"""Shared preset values for the ukrainealarm.com API (official host and mirrors).

Not an optional module: `ukrainealarm` and `siren_pp_ua` both use it, and a
build can include either one alone.

Variables in [source.vars]:
    region_id    the watched place, for example "332" (м. Дніпро та Дніпровська ТГ)
    district_id  its district, for example "44"
    oblast_id    its oblast, for example "9"
All ids: GET {base}/regions (oblast > district > community).

States (checked live on 2026-11-08 through siren.pp.ua):
- An alert on the place, its district or its oblast covers the place: FULL.
- An alert anywhere else in the oblast: PARTIAL. The lookup `nearby_ids`
  reads the ids of the oblast from /regions at the first poll.
- Alert kinds come from the alert type (AIR, ARTILLERY, ...). An alert whose
  levels are only "Yellow" is a preliminary threat: kind `threat`. The API
  marks each alert with levels in `activeAlertLevels` ("Red" or "Yellow").

Requests: each poll fetches only /alerts/status (`lastActionIndex`, 38 bytes).
The full /alerts (all regions, ~16 KB) runs when that number changes
(~every 7 s, it counts changes in the whole country) or after max_age_s.
"""

from __future__ import annotations

from typing import Any

TIMEOUT_S = 10
MARKER_MAX_AGE_S = 300  # a full request at least every 5 min, even with no change
# The lookup that reads the region tree. The region picker (regions.py) uses it too.
REGION_TREE_LOOKUP = "nearby_ids"

# The region entries whose alerts cover the watched place.
_COVERING = "[?contains(['${region_id}', '${district_id}', '${oblast_id}'], regionId)]"
# The ids of every district and community of the oblast (two levels below it).
_NEARBY_LOOKUP = ("states[?regionId=='${oblast_id}'] | [0] | "
                  "[regionChildIds[].regionId, regionChildIds[].regionChildIds[].regionId][]")
# For one alert: THREAT if its levels are Yellow and none is Red, else its type.
_KIND_OF_ALERT = ("((activeAlertLevels[?alertLevel=='Yellow'] && !(activeAlertLevels[?alertLevel=='Red']))"
                  " && 'THREAT') || type")


def defaults(base_url: str, headers: dict[str, str]) -> dict[str, Any]:
    """The preset for this API at `base_url` (the official host or a mirror)."""
    return {
        "request": {
            "url": f"{base_url}/alerts",
            "method": "GET",
            "timeout_s": TIMEOUT_S,
            "headers": headers,
        },
        "change_marker": {
            "url": f"{base_url}/alerts/status",
            "field": "lastActionIndex",
            "max_age_s": MARKER_MAX_AGE_S,
        },
        "lookups": {
            REGION_TREE_LOOKUP: {"url": f"{base_url}/regions", "expression": _NEARBY_LOOKUP},
        },
        "fields": {
            "full": f"length({_COVERING}.activeAlerts[]) > `0`",
            "partial": "length([?contains(`${nearby_ids}`, regionId)].activeAlerts[]) > `0`",
            "kinds": f"map(&({_KIND_OF_ALERT}), {_COVERING}.activeAlerts[])",
            "changed_at": f"{_COVERING}.lastUpdate",
        },
        "kinds": {
            "default": "air_raid",
            "unmapped": "other",
            "map": {
                "AIR": "air_raid",
                "ARTILLERY": "artillery",
                "URBAN_FIGHTS": "urban_fights",
                "CHEMICAL": "chemical",
                "NUCLEAR": "nuclear",
                "THREAT": "threat",
            },
        },
        "time": {"format": "iso", "timezone": "UTC"},
    }
