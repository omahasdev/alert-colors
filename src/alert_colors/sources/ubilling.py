"""Preset: https://ubilling.net.ua/aerialalerts/ (no token, oblast level only).

The feed has one `alertnow` flag for each oblast and no alert types. So an
active alert has FULL coverage and the kind `air_raid`.

    [source]
    type = "ubilling"
    [source.vars]
    oblast_name = "Київська область"   # setup.cmd region writes it

`region` (the oblast name as the feed spells it) takes the value of
`oblast_name`. An older config that gives `region` itself keeps working.

Tested against the live feed on 2026-11-08. The feed answers HTTP 429 when
polled too fast; the poller then backs off (see daemon.py).
"""

from __future__ import annotations

from .preset import register_preset

TIMEOUT_S = 10

DEFAULTS = {
    "request": {"url": "https://ubilling.net.ua/aerialalerts/", "method": "GET", "timeout_s": TIMEOUT_S},
    "fields": {
        "full": 'states."${region}".alertnow',
        "observed_at": "cachedat",
        "changed_at": 'states."${region}".changed',
    },
    "kinds": {"default": "air_raid"},
    "time": {"format": "%Y-%m-%d %H:%M:%S", "timezone": "Europe/Kyiv"},
}

register_preset("ubilling", DEFAULTS, aliases={"region": "oblast_name"})
