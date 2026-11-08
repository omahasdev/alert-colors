"""Preset: https://siren.pp.ua, a public mirror of the official ukrainealarm.com API.

No key. The Home Assistant "Ukraine Alarm" integration uses the same mirror.
Region level down to hromada (city), with alert types.

    [source]
    type = "siren_pp_ua"
    poll_interval_s = 1          # the mirror caches for 1 s (Cache-Control: max-age=1)
    [source.vars]
    region_id = "332"            # м. Дніпро та Дніпровська територіальна громада

Region ids: GET https://siren.pp.ua/api/v3/regions (oblast > district > community).
Everything else is the same as the `ukrainealarm` module: change detection with
`lastActionIndex`, and every listed alert covers the region.

Checked live on 2026-11-08: /alerts/status 38 bytes, ETag, max-age=1.
"""

from __future__ import annotations

from .preset import register_preset
from .ukrainealarm_api import defaults

BASE_URL = "https://siren.pp.ua/api/v3"

register_preset("siren_pp_ua", defaults(BASE_URL, {}))
