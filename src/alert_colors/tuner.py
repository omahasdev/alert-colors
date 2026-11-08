"""The configurator: a local web page that tunes the running app live.

`configure.cmd` starts it with normal rights. It reads the installed config,
shows one form for each scene (built from the effect schemas), and on Apply
writes the tuning file, which the running app reloads within about a second.

Security: the server binds 127.0.0.1 only, on a random port. Every API call
needs a random token from the URL that the tuner opens, and the Host header
must be the server's own address (no DNS rebinding). Request bodies are
limited in size. The server writes one file only: the tuning file, and every
document is validated with tuning.parse() before it is written.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from .config import App
from .domain import FULL_BRIGHTNESS
from .outputs import NO_CORRECTION
from .effects import schema
from .tuning import TuningError, parse, read_file, write_file

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
ANY_FREE_PORT = 0
TOKEN_BYTES = 24
MAX_BODY_BYTES = 262_144
IDLE_EXIT_S = 1800  # the tuner exits after 30 min without requests
PREVIEW_MIN_S, PREVIEW_DEFAULT_S, PREVIEW_MAX_S = 1.0, 10.0, 120.0
SCENE_LINE = re.compile(r"scene (\S+) -> (\S+)")
LOG_TAIL_BYTES = 65_536


def _rule_label(rule: dict[str, Any]) -> str:
    parts = [f"{key}={rule[key]}" for key in ("state", "coverage", "active", "kinds") if key in rule]
    return ", ".join(parts) or "everything else"


class Tuner:
    def __init__(self, app: App) -> None:
        self.app = app
        self.path = app.tuning_path()
        if self.path is None:
            raise SystemExit("config.toml has no [tuning] section: live tuning is off")
        self.base_scenes = app.raw["scenes"]
        self.backend_types = {e["type"] for e in app.raw.get("lighting", []) if e.get("enabled", True)}
        self.token = secrets.token_urlsafe(TOKEN_BYTES)
        self.last_request = time.monotonic()

    # ---- state --------------------------------------------------------------
    def _document(self) -> dict[str, Any]:
        try:
            return read_file(self.path) if self.path.exists() else {}
        except (OSError, ValueError):
            return {}

    def _current_scene(self) -> str | None:
        file_name = self.app.raw.get("logging", {}).get("file")
        if not file_name:
            return None
        try:
            with self.app.log_path(file_name).open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        matches = SCENE_LINE.findall(tail)
        return matches[-1][1] if matches else None

    def state(self) -> dict[str, Any]:
        document = self._document()
        try:
            tuning = parse(document, self.base_scenes, self.backend_types)
            options = tuning.scene_options
        except TuningError:
            options = {name: dict(value) for name, value in self.base_scenes.items()}
        lighting = {}
        for entry in self.app.raw.get("lighting", []):
            if entry.get("enabled", True):
                tuned = document.get("lighting", {}).get(entry["type"], {})
                lighting[entry["type"]] = {
                    "brightness": tuned.get("brightness", entry.get("brightness", FULL_BRIGHTNESS)),
                    "color_correction": tuned.get("color_correction",
                                                  entry.get("color_correction", list(NO_CORRECTION))),
                }
        used_by = {}
        for rule in self.app.raw.get("rules", []):
            used_by.setdefault(rule["scene"], []).append(_rule_label(rule))
        return {
            "schema": schema(),
            "scenes": options,
            "base_scenes": self.base_scenes,
            "used_by": used_by,
            "brightness": document.get("display", {}).get(
                "brightness", self.app.raw.get("display", {}).get("brightness", FULL_BRIGHTNESS)),
            "lighting": lighting,
            "tuning_file": str(self.path),
            "current_scene": self._current_scene(),
        }

    # ---- actions ------------------------------------------------------------
    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        scenes = {name: value for name, value in body.get("scenes", {}).items()
                  if value != self.base_scenes.get(name)}  # store only what differs from config
        document = {"scenes": scenes, "display": {"brightness": body.get("brightness", FULL_BRIGHTNESS)},
                    "lighting": body.get("lighting", {})}
        parse(document, self.base_scenes, self.backend_types)  # raises TuningError
        write_file(self.path, document)
        return {"ok": True, "message": f"applied: {len(scenes)} scene(s) differ from config.toml"}

    def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        seconds = min(max(float(body.get("seconds", PREVIEW_DEFAULT_S)), PREVIEW_MIN_S), PREVIEW_MAX_S)
        document = self._document()
        document["preview"] = {"scene": body["scene"], "until": time.time() + seconds}
        if "scene_definition" in body:  # the form values; used for the preview only, not saved
            document["preview"]["definition"] = body["scene_definition"]
        parse(document, self.base_scenes, self.backend_types)
        write_file(self.path, document)
        return {"ok": True, "message": f"showing {body['scene']} for {seconds:.0f} s"}

    def reset(self) -> dict[str, Any]:
        if self.path.exists():
            self.path.unlink()
        return {"ok": True, "message": "tuning removed: config.toml values apply"}

    # ---- server -------------------------------------------------------------
    def serve(self, open_browser: bool = True, stop: threading.Event | None = None) -> None:
        """Serve until Close, idle timeout, or `stop` (the app's stop event: an
        update or restart then closes the configurator too)."""
        tuner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # quiet console
                log.debug(fmt, *args)

            def _allowed(self) -> bool:
                host_ok = self.headers.get("Host") == f"{HOST}:{server.server_port}"
                token = self.headers.get("X-Token") or ""
                return host_ok and hmac.compare_digest(token, tuner.token)

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data: Any) -> None:
                self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")

            def do_GET(self) -> None:  # noqa: N802
                tuner.last_request = time.monotonic()
                if self.path.split("?")[0] == "/":
                    if self.headers.get("Host") != f"{HOST}:{server.server_port}":
                        self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                        return
                    page = resources.files("alert_colors").joinpath("tuner.html").read_bytes()
                    self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
                elif self.path == "/api/state" and self._allowed():
                    self._json(HTTPStatus.OK, tuner.state())
                else:
                    self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "forbidden"})

            def do_POST(self) -> None:  # noqa: N802
                tuner.last_request = time.monotonic()
                if not self._allowed():
                    self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "forbidden"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "message": "too large"})
                    return
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    action = {"/api/apply": tuner.apply, "/api/preview": tuner.preview}.get(self.path)
                    if self.path == "/api/reset":
                        result = tuner.reset()
                    elif self.path == "/api/quit":
                        result = {"ok": True, "message": "configurator closed"}
                        threading.Thread(target=server.shutdown, daemon=True).start()
                    elif action is not None:
                        result = action(body)
                    else:
                        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "unknown action"})
                        return
                    self._json(HTTPStatus.OK, result)
                except (TuningError, ValueError, KeyError, TypeError, OSError) as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(error)})

        server = ThreadingHTTPServer((HOST, ANY_FREE_PORT), Handler)
        url = f"http://{HOST}:{server.server_port}/#{self.token}"

        def idle_exit() -> None:
            while True:
                time.sleep(IDLE_EXIT_S / 10)
                if time.monotonic() - tuner.last_request > IDLE_EXIT_S:
                    server.shutdown()
                    return

        threading.Thread(target=idle_exit, daemon=True).start()
        if stop is not None:
            threading.Thread(target=lambda: (stop.wait(), server.shutdown()), daemon=True).start()
        log.info("configurator at %s", url.split("#")[0])
        print(f"Configurator: {url}", flush=True)  # a parent reads the token from this line
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
