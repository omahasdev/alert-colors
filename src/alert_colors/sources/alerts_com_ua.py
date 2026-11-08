"""Preset: https://alerts.com.ua (oblast level; no key for /api/states).

    [source]
    type = "alerts_com_ua"
    [source.vars]
    state_id = "3"          # the oblast id of this API: 3 = Dnipropetrovsk oblast

Warning (checked 2026-11-08): the public data was frozen. `last_update` was
more than 30 h old, and the Dnipropetrovsk oblast showed no alert while
ubilling showed one. The staleness check then shows the "unknown" scene.
Prefer the official `ukrainealarm` source.

Response shape (from the backend source, github.com/and3rson/raid):
{"states": [{"id": 3, "name": "...", "alert": true, "changed": "<ISO>"}],
 "last_update": "<ISO>"}. A key, when you have one, goes in X-API-Key through
ALERTS_COM_UA_KEY; the default request sends none.
"""

from __future__ import annotations

from .preset import register_preset

TIMEOUT_S = 10
_STATE = "states[?to_string(id)=='${state_id}'] | [0]"

DEFAULTS = {
    "request": {
        "url": "https://alerts.com.ua/api/states",
        "method": "GET",
        "timeout_s": TIMEOUT_S,
    },
    "fields": {
        "full": f"{_STATE}.alert",
        "observed_at": "last_update",
        "changed_at": f"{_STATE}.changed",
    },
    "kinds": {"default": "air_raid"},
    "time": {"format": "iso", "timezone": "Europe/Kyiv"},
}

register_preset("alerts_com_ua", DEFAULTS)
