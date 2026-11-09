"""Razer Chroma backend through the local Chroma SDK REST server.

Protocol: POST app info to the base URL -> session `uri`. PUT static effects
to `{uri}/{device}`. The session closes if no request arrives within the SDK
timeout, so the backend sends a heartbeat. DELETE `{uri}` ends the session and
gives the devices back to Synapse.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..frame import Frame
from ..http import local_request_json
from .base import BACKENDS

log = logging.getLogger(__name__)

STATIC_EFFECT = "CHROMA_STATIC"
CUSTOM_EFFECT = "CHROMA_CUSTOM"
KEYBOARD = "keyboard"
KEYBOARD_ROWS, KEYBOARD_COLUMNS = 6, 22  # the Chroma keyboard grid


@dataclass
class RazerChromaBackend:
    url: str
    devices: list[str]
    app_title: str
    heartbeat_interval_s: float
    init_delay_s: float
    timeout_s: float
    name: str = "razer_chroma"
    _uri: str | None = field(default=None, repr=False)
    _last: dict[str, Any] = field(default_factory=dict, repr=False)
    _last_request: float = 0.0

    def _acquire(self) -> None:
        if self._uri is not None:
            return
        app = {
            "title": self.app_title,
            "description": "Shows the air-raid alert status with device lighting",
            "author": {"name": self.app_title, "contact": "localhost"},
            "device_supported": self.devices,
            "category": "application",
        }
        session = local_request_json(self.url, method="POST", body=app, timeout_s=self.timeout_s)
        self._uri = session["uri"]
        self._last = {}
        time.sleep(self.init_delay_s)  # the SDK ignores effects sent right after init
        log.info("Razer Chroma session opened: %s", self._uri)

    def _put(self, path: str, body: Any = None) -> None:
        local_request_json(f"{self._uri}/{path}", method="PUT", body=body, timeout_s=self.timeout_s)
        self._last_request = time.monotonic()

    @staticmethod
    def _effect(frame: Frame, device: str) -> dict[str, Any]:
        if device == KEYBOARD and not frame.uniform:
            # One color for each column; the same row repeats down the keyboard.
            row = [c.to_bgr_int() for c in frame.colors(KEYBOARD_COLUMNS)]
            return {"effect": CUSTOM_EFFECT, "param": [row] * KEYBOARD_ROWS}
        return {"effect": STATIC_EFFECT, "param": {"color": frame.color().to_bgr_int()}}

    def show(self, frame: Frame) -> None:
        self._acquire()
        sent = False
        for device in self.devices:
            effect = self._effect(frame, device)
            if effect != self._last.get(device):
                self._put(device, effect)
                self._last[device] = effect
                sent = True
        if not sent and time.monotonic() - self._last_request >= self.heartbeat_interval_s:
            self._put("heartbeat")

    def reclaim(self) -> None:
        self._last = {}  # send the effects again with the next frame

    def release(self) -> None:
        if self._uri is None:
            return
        try:
            local_request_json(self._uri, method="DELETE", timeout_s=self.timeout_s)
            log.info("Razer Chroma session closed")
        finally:
            self._uri = None
            self._last = {}

    def describe(self) -> list[str]:
        info = local_request_json(self.url, timeout_s=self.timeout_s)
        return [f"Chroma SDK {info.get('version')} at {self.url}", *self.devices]


@BACKENDS.register("razer_chroma")
def _create(options: dict[str, Any]) -> RazerChromaBackend:
    return RazerChromaBackend(
        url=options["url"],
        devices=list(options["devices"]),
        app_title=options["app_title"],
        heartbeat_interval_s=float(options["heartbeat_interval_s"]),
        init_delay_s=float(options["init_delay_s"]),
        timeout_s=float(options["timeout_s"]),
    )
