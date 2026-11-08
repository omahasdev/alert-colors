"""Live tuning: a small JSON file that changes the look while the app runs.

The installed app runs as admin and reads its config from Program Files, where
only administrators can write. The tuner (configure.cmd) runs with normal
rights, so it writes this separate file instead, and the app reloads it within
`check_interval_s`. No restart and no UAC are necessary.

The app reads this file with admin rights and your account can write it, so
its content is limited and checked strictly. Allowed:

    {
      "scenes":   {"<existing scene>": {"effect": "...", <effect options>}},
      "display":  {"brightness": 0..1},
      "lighting": {"<backend type>": {"brightness": 0..1, "color_correction": [r, g, b]}},
      "preview":  {"scene": "<existing scene>", "until": <unix time>,
                   "definition": {<optional scene definition, used only for the preview>}}
    }

A scene must already exist in config.toml (the rules name the scenes). The
file can never change the source, the URLs, the rules, the log path or any
other setting. On any error the app logs a warning and keeps the last valid
tuning, so the worst a bad file can do is wrong colors.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import FULL_BRIGHTNESS, NO_BRIGHTNESS
from .scenes import Scene, build_scenes

log = logging.getLogger(__name__)

TOP_KEYS = {"scenes", "display", "lighting", "preview"}
DISPLAY_KEYS = {"brightness"}
LIGHTING_KEYS = {"brightness", "color_correction"}
PREVIEW_KEYS = {"scene", "until", "definition"}
RGB_CHANNELS = 3
MAX_FILE_BYTES = 262_144  # a tuning file is small; refuse anything larger


_NEVER_CHECKED = object()  # differs from every file signature, also from None (no file)


class TuningError(ValueError):
    pass


@dataclass(frozen=True)
class LightingTuning:
    brightness: float | None = None
    correction: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class Tuning:
    scenes: dict[str, Scene]  # the effective scenes: config scenes with the tuned ones replaced
    scene_options: dict[str, dict[str, Any]]  # the effective raw options, for the tuner UI
    brightness: float | None = None
    lighting: dict[str, LightingTuning] = field(default_factory=dict)
    preview_scene: str | None = None
    preview_until: float = 0.0
    preview_definition: Scene | None = None  # form values that are not applied yet


def _factor(value: Any, where: str) -> float:
    number = float(value)
    if not NO_BRIGHTNESS <= number <= FULL_BRIGHTNESS:
        raise TuningError(f"{where}: {number} is outside 0..1")
    return number


def _only(table: Any, allowed: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(table, dict):
        raise TuningError(f"{where}: must be an object")
    unknown = set(table) - allowed
    if unknown:
        raise TuningError(f"{where}: keys {sorted(unknown)} are not allowed; allowed: {sorted(allowed)}")
    return table


def parse(raw: Any, base_scenes: Mapping[str, dict[str, Any]], backend_types: set[str]) -> Tuning:
    """Validate a tuning document against the config. Raises TuningError."""
    raw = _only(raw, TOP_KEYS, "tuning")
    scenes_raw = _only(raw.get("scenes", {}), set(base_scenes), "scenes")
    options = {name: dict(base_scenes[name]) for name in base_scenes}
    for name, definition in scenes_raw.items():
        if not isinstance(definition, dict):
            raise TuningError(f"scenes.{name}: must be an object")
        options[name] = dict(definition)
    try:
        scenes = build_scenes(options)
    except (ValueError, KeyError, TypeError) as error:
        raise TuningError(f"scenes: {error}") from error

    display = _only(raw.get("display", {}), DISPLAY_KEYS, "display")
    brightness = _factor(display["brightness"], "display.brightness") if "brightness" in display else None

    lighting = {}
    for backend, table in _only(raw.get("lighting", {}), backend_types, "lighting").items():
        table = _only(table, LIGHTING_KEYS, f"lighting.{backend}")
        correction = table.get("color_correction")
        if correction is not None:
            if not isinstance(correction, list) or len(correction) != RGB_CHANNELS:
                raise TuningError(f"lighting.{backend}.color_correction: needs [r, g, b]")
            r, g, b = (_factor(c, f"lighting.{backend}.color_correction") for c in correction)
            correction = (r, g, b)
        lighting[backend] = LightingTuning(
            brightness=_factor(table["brightness"], f"lighting.{backend}.brightness") if "brightness" in table else None,
            correction=correction,
        )

    preview = _only(raw.get("preview", {}), PREVIEW_KEYS, "preview")
    preview_scene = preview.get("scene")
    if preview_scene is not None and preview_scene not in scenes:
        raise TuningError(f"preview.scene: unknown scene {preview_scene!r}")
    preview_definition = None
    if "definition" in preview:
        if preview_scene is None or not isinstance(preview["definition"], dict):
            raise TuningError("preview.definition: needs preview.scene and an object")
        try:
            preview_definition = build_scenes({preview_scene: preview["definition"]})[preview_scene]
        except (ValueError, KeyError, TypeError) as error:
            raise TuningError(f"preview.definition: {error}") from error
    return Tuning(
        scenes=scenes,
        scene_options=options,
        brightness=brightness,
        lighting=lighting,
        preview_scene=preview_scene,
        preview_until=float(preview.get("until", 0.0)),
        preview_definition=preview_definition,
    )


def read_file(path: Path) -> Any:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise TuningError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    return json.loads(path.read_text(encoding="utf-8"))


def write_file(path: Path, document: dict[str, Any]) -> None:
    """Write atomically: the app never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def resolve_path(raw: str) -> Path:
    return Path(os.path.expandvars(raw))


@dataclass
class TuningWatcher:
    """Reloads the tuning file when it changes; keeps the last valid tuning."""

    path: Path
    base_scenes: Mapping[str, dict[str, Any]]
    backend_types: set[str]
    check_interval_s: float
    monotonic: Callable[[], float]
    _signature: object = field(default=_NEVER_CHECKED, init=False)
    _next_check: float = field(default=0.0, init=False)

    def poll(self) -> Tuning | None:
        """A new Tuning when the file changed (or was removed), else None."""
        now = self.monotonic()
        if now < self._next_check:
            return None
        self._next_check = now + self.check_interval_s
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime, stat.st_size)
        except FileNotFoundError:
            signature = None
        if signature == self._signature:
            return None
        self._signature = signature
        try:
            raw = read_file(self.path) if signature is not None else {}
            tuning = parse(raw, self.base_scenes, self.backend_types)
        except (OSError, ValueError) as error:  # TuningError and JSON errors are ValueErrors
            log.warning("tuning file %s ignored: %s", self.path, error)
            return None
        log.info("tuning applied from %s", self.path if signature is not None else "config (no tuning file)")
        return tuning
