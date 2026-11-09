"""Load config.toml and build the object graph. This is the only place that
knows how the parts connect."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .daemon import Daemon, DaemonSettings
from .domain import FULL_BRIGHTNESS
from .lighting import AVAILABLE_MODULES, BACKENDS, LightingBackend
from .outputs import NO_CORRECTION, BackendWorker
from .policy import Policy
from .scenes import Scene, build_scenes
from .tuning import TuningWatcher, resolve_path
from .sources import SOURCES, AlertSource

DEFAULT_CONFIG = Path("config.toml")
# Take the LEDs again (see power_events.py): for a config without these keys.
DEFAULT_RECLAIM_INTERVAL_S = 300.0  # a fallback for takeovers without a wake event
DEFAULT_RECLAIM_AFTER_WAKE_S = 60.0  # the ASUS service applied its profile 12 s after the event
DEFAULT_RECLAIM_STEP_S = 5.0
log = logging.getLogger(__name__)


@dataclass
class App:
    raw: dict[str, Any]
    base_dir: Path

    @classmethod
    def load(cls, path: Path) -> App:
        with path.open("rb") as f:
            return cls(tomllib.load(f), path.resolve().parent)

    def source(self) -> AlertSource:
        section = self.raw["source"]
        return SOURCES.create(section["type"], section)

    def scenes(self) -> dict[str, Scene]:
        return build_scenes(self.raw["scenes"])

    def policy(self) -> Policy:
        return Policy.from_config(self.raw["rules"], self.raw["scenes"].keys())

    def lighting_entries(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """The enabled [[lighting]] entries whose module is in this build."""
        if dry_run:
            return [{"type": "console"}]
        entries = []
        for entry in self.raw.get("lighting", []):
            if not entry.get("enabled", True):
                continue
            if entry["type"] not in BACKENDS.names():
                log.warning("lighting %r is not in this build (modules: %s); skipped",
                            entry["type"], ", ".join(AVAILABLE_MODULES) or "none")
                continue
            entries.append(entry)
        return entries

    def backends(self, dry_run: bool = False) -> list[LightingBackend]:
        return [BACKENDS.create(entry["type"], entry) for entry in self.lighting_entries(dry_run)]

    def settings(self) -> DaemonSettings:
        source, display = self.raw["source"], self.raw["display"]
        return DaemonSettings(
            poll_interval_s=float(source["poll_interval_s"]),
            stale_after_s=float(source["stale_after_s"]),
            frame_interval_s=float(display["frame_interval_s"]),
            backoff_max_s=float(source.get("backoff_max_s", source["poll_interval_s"])),
            brightness=float(display.get("brightness", FULL_BRIGHTNESS)),
            reclaim_interval_s=float(display.get("reclaim_interval_s", DEFAULT_RECLAIM_INTERVAL_S)),
            reclaim_after_wake_s=float(display.get("reclaim_after_wake_s", DEFAULT_RECLAIM_AFTER_WAKE_S)),
            reclaim_step_s=float(display.get("reclaim_step_s", DEFAULT_RECLAIM_STEP_S)),
        )

    def outputs(self, dry_run: bool = False) -> list[BackendWorker]:
        display = self.raw["display"]
        return [
            BackendWorker(
                BACKENDS.create(entry["type"], entry),
                retry_s=float(display["backend_retry_s"]),
                close_timeout_s=float(display["backend_close_timeout_s"]),
                brightness=float(entry.get("brightness", FULL_BRIGHTNESS)),
                correction=_correction(entry),
            )
            for entry in self.lighting_entries(dry_run)
        ]

    def tuning_path(self) -> Path | None:
        section = self.raw.get("tuning")
        return resolve_path(section["file"]) if section else None

    def tuning_watcher(self, outputs: list[BackendWorker], monotonic=None) -> TuningWatcher | None:  # noqa: ANN001
        path = self.tuning_path()
        if path is None:
            return None
        import time

        return TuningWatcher(
            path=path,
            base_scenes=self.raw["scenes"],
            backend_types={output.name for output in outputs},
            check_interval_s=float(self.raw["tuning"]["check_interval_s"]),
            monotonic=monotonic or time.monotonic,
        )

    def daemon(self, dry_run: bool = False) -> Daemon:
        outputs = self.outputs(dry_run)
        return Daemon(
            source=self.source(),
            policy=self.policy(),
            scenes=self.scenes(),
            outputs=outputs,
            settings=self.settings(),
            tuning=self.tuning_watcher(outputs),
        )

    def stop_event_name(self) -> str:
        return self.raw["service"]["stop_event"]

    def log_path(self, file_name: str) -> Path:
        """The log file, always inside the config folder.

        The app runs as admin: an absolute path or `..` in the config could
        otherwise append log lines to any file on the PC.
        """
        path = (self.base_dir / file_name).resolve()
        if not path.is_relative_to(self.base_dir.resolve()):
            raise ValueError(f"[logging] file must stay inside {self.base_dir}: {file_name!r}")
        return path

    def configure_logging(self, to_file: bool = True) -> None:
        section = self.raw["logging"]
        handlers: list[logging.Handler] = []
        if sys.stderr is not None:  # None under pythonw.exe
            handlers.append(logging.StreamHandler())
        if to_file and (file_name := section.get("file")):
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    self.log_path(file_name),
                    maxBytes=int(section["max_bytes"]),
                    backupCount=int(section["backups"]),
                    encoding="utf-8",
                )
            )
        logging.basicConfig(
            level=section["level"],
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=handlers,
            force=True,
        )


RGB_CHANNELS = 3


def _correction(entry: dict[str, Any]) -> tuple[float, float, float]:
    """`color_correction = [r, g, b]` of a [[lighting]] entry (factors 0..1)."""
    raw = entry.get("color_correction")
    if raw is None:
        return NO_CORRECTION
    if len(raw) != RGB_CHANNELS:
        raise ValueError(f"lighting {entry['type']!r}: color_correction needs 3 factors [r, g, b]")
    r, g, b = (float(f) for f in raw)
    return (r, g, b)
