"""Preset: https://api.alerts.in.ua (token; city level, with alert types).

    [source]
    type = "alerts_in_ua"
    poll_interval_s = 15                 # the API allows few requests
    [source.vars]                        # setup.cmd region writes these
    region_id = "0"                      # Тестовий регіон (the test region)
    district_id = "0"
    oblast_id = "0"
    oblast_name = ""                     # the oblast name, for example "Київська область"

The location UIDs of alerts.in.ua are the region IDs of ukrainealarm.com
(checked: 1604 IDs in the published UID table, all with the same place). So
`city_uid`, `raion_uid` and `oblast_uid` take the values of `region_id`,
`district_id` and `oblast_id`. A config that gives the UIDs itself keeps them.

Put the token in the environment variable ALERTS_IN_UA_TOKEN: `setup.cmd token`
does it (request the token with the form at https://alerts.in.ua/api-request).
The config holds only the placeholder.

An alert on the city, its raion or its oblast gives FULL coverage. Any other
alert in the oblast gives PARTIAL coverage. The alert kinds and the change time
come only from the alerts that cover the place, as in the ukrainealarm preset:
an artillery alert in another hromada does not change the scene. The oblast of an alert is its
`location_oblast` (the name), compared with `oblast_name`. The field
`location_oblast_uid` is not the UID of the oblast: for a hromada or a raion it
is the UID of that place. If `oblast_name` is empty (an older config), the app
does not see an alert elsewhere in the oblast: run `setup.cmd region` again. Tested live: `location_uid` is a
string, `location_oblast_uid` is a number, and `meta.last_updated_at` has the
form "2026/11/08 11:05:00 +0000".
"""

from __future__ import annotations

from .preset import Token, register_preset

TIMEOUT_S = 10
_IN_PLACE = ("to_string(location_uid)=='${city_uid}' || to_string(location_uid)=='${raion_uid}'"
             " || to_string(location_uid)=='${oblast_uid}'")
_IN_OBLAST = "location_oblast=='${oblast_name}' || to_string(location_uid)=='${oblast_uid}'"

DEFAULTS = {
    "vars": {"oblast_name": ""},  # setup.cmd region writes it; an older config has none
    "request": {
        "url": "https://api.alerts.in.ua/v1/alerts/active.json",
        "method": "GET",
        "timeout_s": TIMEOUT_S,
        "headers": {"Authorization": "Bearer ${env:ALERTS_IN_UA_TOKEN}"},
    },
    "fields": {
        "full": f"length(alerts[?{_IN_PLACE}]) > `0`",
        "partial": f"length(alerts[?{_IN_OBLAST}]) > `0`",
        "kinds": f"alerts[?{_IN_PLACE}].alert_type",
        "observed_at": "meta.last_updated_at",
        "changed_at": f"alerts[?{_IN_PLACE}].started_at",
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

register_preset("alerts_in_ua", DEFAULTS,
                aliases={"city_uid": "region_id", "raion_uid": "district_id", "oblast_uid": "oblast_id"},
                token=Token("ALERTS_IN_UA_TOKEN", "https://alerts.in.ua/api-request"))
