import tomllib
from pathlib import Path

import pytest

from alert_colors import regions
from alert_colors.config import App
from alert_colors.http import RateLimitedError

ROOT = Path(__file__).resolve().parents[1]
CONFIG_TEXT = (ROOT / "config.toml").read_text(encoding="utf-8")

TREE = {"states": [
    {"regionId": "31", "regionName": "м. Київ", "regionChildIds": []},
    {"regionId": "9", "regionName": "Дніпропетровська область", "regionChildIds": [
        {"regionId": "44", "regionName": "Дніпровський район", "regionChildIds": [
            {"regionId": "332", "regionName": "м. Дніпро та Дніпровська територіальна громада"},
            {"regionId": "333", "regionName": "Слобожанська територіальна громада"},
        ]},
        {"regionId": "45", "regionName": "Кам'янський район", "regionChildIds": [
            {"regionId": "340", "regionName": "Дніпровська сільська громада"},
        ]},
    ]},
    {"regionId": "22", "regionName": "Харківська область", "regionChildIds": [
        {"regionId": "130", "regionName": "Харківський район", "regionChildIds": []},
    ]},
    {"regionId": "1293", "regionName": "м. Харків та Харківська територіальна громада", "regionChildIds": []},
    {"regionId": "0", "regionName": "Тестовий регіон", "regionChildIds": []},
]}
PLACES = regions.places(TREE)


def test_every_level_is_a_place_with_its_parents():
    kyiv = regions.find(PLACES, "31")
    assert (kyiv.district_id, kyiv.oblast_id) == ("31", "31")
    city = regions.find(PLACES, "332")
    assert city.variables() == {"region_id": "332", "district_id": "44", "oblast_id": "9"}
    assert city.path.endswith("Дніпровський район, Дніпропетровська область")
    assert len(PLACES) == 11


def test_the_test_region_is_a_place_with_its_own_ids():
    assert regions.find(PLACES, "0").variables() == {"region_id": "0", "district_id": "0", "oblast_id": "0"}
    assert regions.search(PLACES, "testovyi")[0].region_id == "0"


def test_a_top_level_city_gets_its_oblast_and_district_from_its_name():
    kharkiv = regions.find(PLACES, "1293")
    assert kharkiv.variables() == {"region_id": "1293", "district_id": "130", "oblast_id": "22"}
    assert regions.search(PLACES, "kharkiv")[0].region_id == "1293"


@pytest.mark.parametrize("text", ["дніпро", "Dnipro", "dnipro ta"])
def test_search_in_ukrainian_or_latin_puts_the_city_first(text):
    assert regions.search(PLACES, text)[0].region_id == "332"


def test_search_kyiv_in_latin_and_apostrophe():
    assert regions.search(PLACES, "kyiv")[0].region_id == "31"
    assert regions.search(PLACES, "kamianskyi")[0].region_id == "45"
    assert regions.search(PLACES, "   ") == []


def test_set_place_changes_only_the_three_ids():
    place = regions.find(PLACES, "31")
    text = regions.set_place(CONFIG_TEXT, place)
    vars_ = tomllib.loads(text)["source"]["vars"]
    assert vars_ == {"region_id": "31", "district_id": "31", "oblast_id": "31"}
    assert "# м. Київ" in text
    changed = [line for line in text.splitlines() if line not in CONFIG_TEXT.splitlines()]
    assert len(changed) == len(regions.VAR_NAMES)
    assert tomllib.loads(text)["scenes"] == tomllib.loads(CONFIG_TEXT)["scenes"]


def test_set_place_adds_a_missing_variable_and_needs_the_table():
    text = regions.set_place('[source]\ntype = "x"\n\n[source.vars]\nregion_id = "1"\n\n[display]\n',
                             regions.find(PLACES, "332"))
    assert tomllib.loads(text)["source"]["vars"]["district_id"] == "44"
    assert tomllib.loads(text)["display"] == {}
    with pytest.raises(regions.RegionError):
        regions.set_place("[source]\n", regions.find(PLACES, "332"))


class FakeSource:
    name = "fake"
    lookups = {regions.REGION_TREE_LOOKUP: None}

    def __init__(self, failures):
        self.failures = failures

    def fetch_lookup_document(self, name):
        if self.failures:
            self.failures -= 1
            raise RateLimitedError(None)
        return TREE


def test_fetch_tree_waits_on_429_and_needs_a_tree_source():
    waits = []
    assert regions.fetch_tree(FakeSource(failures=2), sleep=waits.append) is TREE
    assert waits == [regions.FIRST_RETRY_WAIT_S, regions.FIRST_RETRY_WAIT_S * 2]
    with pytest.raises(RateLimitedError):
        regions.fetch_tree(FakeSource(failures=regions.FETCH_ATTEMPTS), sleep=waits.append)
    with pytest.raises(regions.RegionError):
        regions.fetch_tree(object())


def test_the_shipped_source_has_the_tree_lookup():
    source = App(tomllib.loads(CONFIG_TEXT), ROOT).source()
    assert regions.REGION_TREE_LOOKUP in source.lookups
    assert source.lookups[regions.REGION_TREE_LOOKUP].request["url"].endswith("/regions")
