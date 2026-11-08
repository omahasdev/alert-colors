"""Preset: https://api.alerts.in.ua (free token; city level, with alert types).

    [source]
    type = "alerts_in_ua"
    poll_interval_s = 15                 # the API allows few requests
    [source.vars]                        # location_uid values from the API docs
    city_uid = "..."
    raion_uid = "..."
    oblast_uid = "..."

Put the token in the environment variable ALERTS_IN_UA_TOKEN (get it at
https://devs.alerts.in.ua). The config holds only the placeholder.

An alert on the city, its raion or its oblast gives FULL coverage. Any other
alert in the oblast gives PARTIAL coverage. Not tested live (no token). The tests
use the documented response shape.
"""

from __future__ import annotations

from .preset import register_preset

TIMEOUT_S = 10
_IN_PLACE = ("to_string(location_uid)=='${city_uid}' || to_string(location_uid)=='${raion_uid}'"
             " || to_string(location_uid)=='${oblast_uid}'")
_IN_OBLAST = "to_string(location_oblast_uid)=='${oblast_uid}' || to_string(location_uid)=='${oblast_uid}'"

DEFAULTS = {
    "request": {
        "url": "https://api.alerts.in.ua/v1/alerts/active.json",
        "method": "GET",
        "timeout_s": TIMEOUT_S,
        "headers": {"Authorization": "Bearer ${env:ALERTS_IN_UA_TOKEN}"},
    },
    "fields": {
        "full": f"length(alerts[?{_IN_PLACE}]) > `0`",
        "partial": f"length(alerts[?{_IN_OBLAST}]) > `0`",
        "kinds": f"alerts[?{_IN_OBLAST}].alert_type",
        "observed_at": "meta.last_updated_at",
        "changed_at": f"alerts[?{_IN_OBLAST}].started_at",
    },
    "kinds": {
        "default": "air_raid",
        "unmapped": "other",
        "map": {
            "air_raid": "air_raid",
            "artillery_shelling": "artillery",
            "urban_fights": "urban_fights",
            "chemical": "chemical",
            "nuclear": "nuclear",
        },
    },
    "time": {"format": "iso", "timezone": "UTC"},
}

register_preset("alerts_in_ua", DEFAULTS)
