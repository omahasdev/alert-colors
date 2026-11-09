import tomllib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alert_colors.domain import Coverage
from alert_colors.sources import SOURCES
from alert_colors.sources.json_http import FieldError

ROOT = Path(__file__).resolve().parents[1]
UBILLING = ROOT / "examples" / "sources" / "ubilling.toml"
KYIV = ZoneInfo("Europe/Kyiv")
REGION = "Київська область"  # the oblast of examples/sources/ubilling.toml
CITY, RAION, OBLAST, OTHER_RAION, OTHER_OBLAST = "1001", "101", "9", "102", "5"


def source_from(path: Path, **vars_override):
    section = tomllib.loads(path.read_text(encoding="utf-8"))["source"]
    section.setdefault("vars", {}).update(vars_override)
    return SOURCES.create(section["type"], section)


# ---- the shipped config: ubilling ------------------------------------------
def ubilling_payload(active):
    return {
        "source": "test",
        "cachedat": "2026-11-08 13:52:10",
        "states": {REGION: {"alertnow": active, "changed": "2026-11-08 13:19:21"}},
    }


def test_ubilling_active():
    s = source_from(UBILLING).parse(ubilling_payload(True))
    assert s.coverage is Coverage.FULL and s.kinds == {"air_raid"}
    assert s.observed_at == datetime(2026, 11, 8, 13, 52, 10, tzinfo=KYIV)
    assert s.changed_at == datetime(2026, 11, 8, 13, 19, 21, tzinfo=KYIV)


def test_ubilling_clear_has_no_kinds():
    s = source_from(UBILLING).parse(ubilling_payload(False))
    assert s.coverage is Coverage.NONE and s.kinds == frozenset()


def test_wrong_region_is_an_error_not_clear():
    source = source_from(UBILLING, region="Unknown oblast")
    with pytest.raises(FieldError):
        source.parse(ubilling_payload(False))


# ---- the example: alerts.in.ua ---------------------------------------------
def alert(uid, kind="air_raid", oblast=OBLAST):
    return {"location_uid": uid, "location_oblast_uid": int(oblast), "alert_type": kind,
            "started_at": "2026-11-08T11:00:00.000Z"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("ALERTS_IN_UA_TOKEN", "test-token")
    monkeypatch.setenv("UKRAINEALARM_API_KEY", "test-key")


def alerts_in_ua(*alerts):
    source = source_from(ROOT / "examples" / "sources" / "alerts_in_ua.toml",
                         city_uid=CITY, raion_uid=RAION, oblast_uid=OBLAST)
    return source.parse({"alerts": list(alerts), "meta": {"last_updated_at": "2026-11-08T11:05:00Z"}})


def test_alerts_in_ua_coverage():
    assert alerts_in_ua(alert(RAION)).coverage is Coverage.FULL
    assert alerts_in_ua(alert(OTHER_RAION)).coverage is Coverage.PARTIAL
    assert alerts_in_ua(alert(OTHER_OBLAST, oblast=OTHER_OBLAST)).coverage is Coverage.NONE
    assert alerts_in_ua().coverage is Coverage.NONE


def test_alerts_in_ua_kind_map_and_times():
    s = alerts_in_ua(alert(CITY, "artillery_shelling"), alert(OTHER_RAION, "new_type"))
    assert s.kinds == {"artillery", "other"}
    assert s.observed_at == datetime(2026, 11, 8, 11, 5, tzinfo=UTC)
    assert s.changed_at == datetime(2026, 11, 8, 11, 0, tzinfo=UTC)


def test_missing_env_placeholder_fails_at_start(monkeypatch):
    monkeypatch.delenv("ALERTS_IN_UA_TOKEN", raising=False)
    with pytest.raises(KeyError, match="ALERTS_IN_UA_TOKEN"):
        source_from(ROOT / "examples" / "sources" / "alerts_in_ua.toml")


def test_env_placeholder_goes_into_headers(monkeypatch):
    monkeypatch.setenv("ALERTS_IN_UA_TOKEN", "secret")
    source = source_from(ROOT / "examples" / "sources" / "alerts_in_ua.toml")
    assert source.request["headers"]["Authorization"] == "Bearer secret"


# ---- alerts.com.ua preset ---------------------------------------------------
def test_alerts_com_ua():
    source = SOURCES.create("alerts_com_ua", {"vars": {"state_id": "9"}})
    payload = {"states": [{"id": 9, "alert": True, "changed": "2026-11-08T13:00:00+02:00"},
                          {"id": 5, "alert": False, "changed": "2026-11-08T12:00:00+02:00"}],
               "last_update": "2026-11-08T13:05:00+02:00"}
    s = source.parse(payload)
    assert s.coverage is Coverage.FULL and s.kinds == {"air_raid"}
    assert "headers" not in source.request  # the public endpoint needs no key
    assert source.name == "alerts_com_ua"


def test_alerts_com_ua_unknown_state_is_an_error():
    source = SOURCES.create("alerts_com_ua", {"vars": {"state_id": "999"}})
    with pytest.raises(FieldError):
        source.parse({"states": [{"id": 9, "alert": True}], "last_update": None})


# ---- presets are data: config overrides any part ---------------------------
def test_config_overrides_preset_values():
    source = SOURCES.create("ubilling", {
        "vars": {"region": REGION},
        "request": {"url": "https://mirror.example.org/alerts/"},
        "kinds": {"default": "siren"},
    })
    assert source.request["url"] == "https://mirror.example.org/alerts/"
    assert source.request["method"] == "GET"  # the rest of the preset stays
    assert source.parse(ubilling_payload(True)).kinds == {"siren"}


def test_preset_without_its_variable_fails_at_start():
    from alert_colors.sources.preset import NotConfiguredError

    with pytest.raises(NotConfiguredError, match="region"):
        SOURCES.create("ubilling", {})


def test_ubilling_takes_the_oblast_name_of_the_place():
    source = SOURCES.create("ubilling", {"vars": {"oblast_name": "Київська область"}})
    assert source.variables["region"] == "Київська область"


def test_an_old_ubilling_region_wins_over_the_oblast_name():
    source = SOURCES.create("ubilling", {"vars": {"region": "Львівська область", "oblast_name": "x"}})
    assert source.variables["region"] == "Львівська область"


def test_alerts_in_ua_takes_the_region_ids(monkeypatch):
    monkeypatch.setenv("ALERTS_IN_UA_TOKEN", "t")
    source = SOURCES.create("alerts_in_ua", {"vars": {"region_id": "155", "district_id": "38", "oblast_id": "3"}})
    assert (source.variables["city_uid"], source.variables["raion_uid"], source.variables["oblast_uid"])         == ("155", "38", "3")


# ---- ukrainealarm (official) and its keyless mirror siren_pp_ua -----------
CITY_ID, DISTRICT_ID, OBLAST_ID = "155", "38", "3"
OTHER_COMMUNITY, OTHER_OBLAST_REGION = "353", "1000"
NEARBY = [DISTRICT_ID, CITY_ID, "48", OTHER_COMMUNITY]   # what the lookup reads from /regions
UA_VARS = {"region_id": CITY_ID, "district_id": DISTRICT_ID, "oblast_id": OBLAST_ID}


def ua_alert(region_id, kind="AIR", *levels):
    alert = {"regionId": region_id, "type": kind}
    if levels:
        alert["activeAlertLevels"] = [{"alertLevel": level} for level in levels]
    return alert


def ua_payload(*alerts):
    """GET /alerts: one entry for each region with active alerts."""
    by_region = {}
    for alert in alerts:
        by_region.setdefault(alert["regionId"], []).append(alert)
    return [{"regionId": rid, "lastUpdate": "2026-11-08T12:00:00Z", "activeAlerts": items}
            for rid, items in by_region.items()]


def ua_source(path="ukrainealarm.toml"):
    source = source_from(ROOT / "examples" / "sources" / path, **UA_VARS)
    source.provide_lookup("nearby_ids", NEARBY)
    return source


def test_city_states():
    source = ua_source()
    assert source.parse(ua_payload()).coverage is Coverage.NONE
    district = source.parse(ua_payload(ua_alert(DISTRICT_ID, "AIR", "Red")))
    assert district.coverage is Coverage.FULL and district.kinds == {"air_raid"}
    nearby = source.parse(ua_payload(ua_alert(OTHER_COMMUNITY, "AIR", "Red")))
    assert nearby.coverage is Coverage.PARTIAL
    elsewhere = source.parse(ua_payload(ua_alert(OTHER_OBLAST_REGION, "AIR", "Red")))
    assert elsewhere.coverage is Coverage.NONE
    assert source.request["headers"]["Authorization"] == "test-key"


@pytest.mark.parametrize(("levels", "kind"), [
    (("Yellow",), "threat"),
    (("Red",), "air_raid"),
    (("Yellow", "Red"), "air_raid"),
    ((), "air_raid"),
])
def test_alert_levels(levels, kind):
    assert ua_source().parse(ua_payload(ua_alert(CITY_ID, "AIR", *levels))).kinds == {kind}


def test_types_and_unknown_type():
    s = ua_source().parse(ua_payload(ua_alert(CITY_ID, "ARTILLERY", "Red"), ua_alert(OBLAST_ID, "NEW")))
    assert s.kinds == {"artillery", "other"}


def test_siren_mirror_needs_no_key(monkeypatch):
    monkeypatch.delenv("UKRAINEALARM_API_KEY", raising=False)
    source = ua_source("siren_pp_ua.toml")
    assert source.request["url"] == "https://siren.pp.ua/api/v3/alerts"
    assert source.marker.request["url"] == "https://siren.pp.ua/api/v3/alerts/status"
    assert source.lookups["nearby_ids"].request["url"] == "https://siren.pp.ua/api/v3/regions"
    assert source.request["headers"] == {}


def test_lookup_expression_reads_the_oblast_tree():
    source = source_from(ROOT / "examples" / "sources" / "siren_pp_ua.toml", **UA_VARS)
    tree = {"states": [{"regionId": OBLAST_ID, "regionChildIds": [
        {"regionId": DISTRICT_ID, "regionChildIds": [{"regionId": CITY_ID}, {"regionId": OTHER_COMMUNITY}]},
        {"regionId": "48", "regionChildIds": []}]}, {"regionId": "5", "regionChildIds": []}]}
    assert sorted(source.lookups["nearby_ids"].expression.search(tree)) == sorted(NEARBY)


@pytest.mark.parametrize(("alerts", "scene"), [
    ((), "clear"),                                                   # N
    ((ua_alert(DISTRICT_ID, "AIR", "Red"),), "alert"),               # R
    ((ua_alert(CITY_ID, "AIR", "Yellow"),), "threat"),               # Y
    ((ua_alert(OTHER_COMMUNITY, "AIR", "Red"),), "partial"),         # P
    ((ua_alert(CITY_ID, "AIR", "Yellow"), ua_alert(OBLAST_ID, "AIR", "Red")), "alert"),  # R wins over Y
])
def test_shipped_rules_give_the_four_states(alerts, scene):
    from alert_colors.config import App

    app = App.load(ROOT / "config.toml")
    status = ua_source("siren_pp_ua.toml").parse(ua_payload(*alerts))
    assert app.policy().select(status) == scene


def test_change_marker_skips_full_request_until_it_changes(monkeypatch):
    from alert_colors.sources import json_http

    marker = {"value": 1}
    calls = []

    def fake_get(request, network):
        calls.append(request["url"].rsplit("/", 1)[-1])
        if request["url"].endswith("/status"):
            return {"lastActionIndex": marker["value"]}
        return ua_payload(ua_alert(CITY_ID, "AIR", "Red"))

    monkeypatch.setattr(json_http, "_get", fake_get)
    clock = {"t": 0.0}
    source = ua_source()
    source.monotonic = lambda: clock["t"]
    assert source.fetch().coverage is Coverage.FULL       # first poll: marker + full
    assert source.fetch().coverage is Coverage.FULL       # same marker: marker only
    marker["value"] = 2
    source.fetch()                                        # new marker: marker + full
    clock["t"] = source.marker.max_age_s + 1
    source.fetch()                                        # too old: marker + full
    assert calls == ["status", "alerts", "status", "status", "alerts", "status", "alerts"]


def test_old_alerts_in_ua_uids_win_over_the_region_ids(monkeypatch):
    """An older config that gives the UIDs itself keeps them."""
    monkeypatch.setenv("ALERTS_IN_UA_TOKEN", "t")
    source = SOURCES.create("alerts_in_ua", {"vars": {"city_uid": "1", "raion_uid": "2", "oblast_uid": "3",
                                                      "region_id": "155", "district_id": "38", "oblast_id": "3"}})
    assert (source.variables["city_uid"], source.variables["raion_uid"], source.variables["oblast_uid"]) \
        == ("1", "2", "3")
