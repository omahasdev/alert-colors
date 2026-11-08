"""Preset: the official API of the "Повітряна тривога" app, https://api.ukrainealarm.com.

    [source]
    type = "ukrainealarm"
    poll_interval_s = 1          # each poll fetches only the change marker
    [source.vars]
    region_id = "332"            # GET /api/v3/regions; 332 = м. Дніпро та Дніпровська ТГ

Put the API key (request form on https://api.ukrainealarm.com) in the
environment variable UKRAINEALARM_API_KEY. The config holds only the
placeholder. Without a key, use the `siren_pp_ua` module: the same API.

Change detection: each poll asks GET /alerts/status for `lastActionIndex`, a
number that changes when any alert starts or ends. The full request
GET /alerts/{region_id} runs only when that number changes, or after
`max_age_s`. So a 1 s poll costs ~40 bytes per second.

Coverage: the response for a region lists the alerts that cover it, its own
and those of its parent district and oblast (checked 2026-11-08: the city
332 listed the AIR alert of district 44; the oblast 9 listed none). So every
listed alert means FULL coverage of the watched region. The response has no
data time stamp: the staleness check uses the time of the last fetch.

Response shapes (checked live through siren.pp.ua on 2026-11-08):
    /alerts/status   {"lastActionIndex": 639297421613722113}
    /alerts/{id}     [{"regionId": "332", "regionType": "Community", "lastUpdate": "<ISO>",
                       "activeAlerts": [{"regionId": "44", "type": "AIR", ...}]}]
"""

from __future__ import annotations

from .preset import register_preset
from .ukrainealarm_api import defaults

OFFICIAL_BASE_URL = "https://api.ukrainealarm.com/api/v3"

register_preset("ukrainealarm", defaults(OFFICIAL_BASE_URL, {"Authorization": "${env:UKRAINEALARM_API_KEY}"}))
