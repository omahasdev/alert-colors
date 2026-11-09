"""Choose the watched place: the region tree of the ukrainealarm.com API.

The tree (GET {base}/regions) is oblast > district > community. Kyiv city is
an oblast-level entry with no children. A place gives the three variables of
[source.vars]: `region_id`, `district_id` and `oblast_id`.

The source module must read this tree for its `partial` state already (the
lookup `REGION_TREE_LOOKUP`), so the picker uses the same URL, headers and
network limits as the running app. Sources without the tree (ubilling,
alerts_in_ua, alerts_com_ua) have their own ids: edit config.toml for them.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .http import RateLimitedError
from .sources.ukrainealarm_api import REGION_TREE_LOOKUP

FETCH_ATTEMPTS = 4
FIRST_RETRY_WAIT_S = 2.0
VAR_NAMES = ("region_id", "district_id", "oblast_id", "oblast_name")
TEST_REGION_ID = "0"  # "Тестовий регіон" of the API: no real place
_FEMININE_ENDING = "а"  # "Харківська" -> stem "Харківськ" -> "Харківський район"

# Ukrainian to Latin (the official transliteration of 2010, without the special
# forms at the start of a word), so "kyiv" finds "Київ".
_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu",
    "я": "ia", "'": "", "’": "", "ʼ": "",
}


class RegionError(ValueError):
    """The source has no region tree, or the tree or the config is not usable."""


@dataclass(frozen=True)
class Place:
    region_id: str
    district_id: str
    oblast_id: str
    name: str
    path: str  # "name, district, oblast": for the list
    oblast_name: str = ""  # for sources that know only oblast names (ubilling)

    def variables(self) -> dict[str, str]:
        return {"region_id": self.region_id, "district_id": self.district_id, "oblast_id": self.oblast_id,
                "oblast_name": self.oblast_name}


def latin(text: str) -> str:
    return "".join(_LATIN.get(char, char) for char in text.lower())


def places(tree: dict[str, Any]) -> list[Place]:
    """Every place of the tree: oblasts, districts and communities."""
    states = tree.get("states", [])
    result = []
    for oblast in states:
        oblast_id, oblast_name = oblast["regionId"], oblast["regionName"]
        if not oblast.get("regionChildIds"):
            result.append(_top_level_city(oblast, states))
            continue
        result.append(Place(oblast_id, oblast_id, oblast_id, oblast_name, oblast_name, oblast_name))
        for district in oblast["regionChildIds"]:
            district_id, district_name = district["regionId"], district["regionName"]
            result.append(Place(district_id, district_id, oblast_id, district_name,
                                f"{district_name}, {oblast_name}", oblast_name))
            for community in district.get("regionChildIds", []):
                result.append(Place(community["regionId"], district_id, oblast_id, community["regionName"],
                                    f"{community['regionName']}, {district_name}, {oblast_name}",
                                    oblast_name))
    if not result:
        raise RegionError("the region tree is empty")
    return result


def _top_level_city(city: dict[str, Any], states: list[dict[str, Any]]) -> Place:
    """A place at the top of the tree, with no children (Kharkiv, Zaporizhzhia, Kyiv, the test region).

    "м. Харків та Харківська територіальна громада" lies in "Харківська
    область" and in "Харківський район": alerts there cover the city too. The
    API does not list this link, so the adjective in the name gives it. Kyiv
    city ("м. Київ") and "Тестовий регіон" belong to no oblast: they keep
    their own id for each level, and their own name as the oblast name.
    """
    city_id, name = city["regionId"], city["regionName"]
    for oblast in states:
        adjective = oblast["regionName"].split()[0]  # "Харківська"
        if not oblast.get("regionChildIds") or adjective not in name.split():
            continue
        stem = adjective[:-len(_FEMININE_ENDING)] if adjective.endswith(_FEMININE_ENDING) else adjective
        district = next((d for d in oblast["regionChildIds"] if d["regionName"].startswith(stem + "ий ")), None)
        district_id = district["regionId"] if district else city_id
        district_part = f"{district['regionName']}, " if district else ""
        return Place(city_id, district_id, oblast["regionId"], name,
                     f"{name}, {district_part}{oblast['regionName']}", oblast["regionName"])
    # "м. Київ" is an oblast-level name too. The test region has no oblast.
    return Place(city_id, city_id, city_id, name, name, "" if city_id == TEST_REGION_ID else name)


def search(all_places: Iterable[Place], text: str) -> list[Place]:
    """Places whose name contains every word of `text`, in Ukrainian or Latin.

    A match in the place name comes before a match only in its district or
    oblast, and a name that starts with the text comes first.
    """
    words = [w for w in text.lower().split() if w]
    if not words:
        return []

    def contains(haystack: str) -> bool:
        return all(w in haystack or w in latin(haystack) for w in words)

    found = [p for p in all_places if contains(p.path.lower())]
    first = words[0]

    def rank(place: Place) -> tuple[int, int, int, int]:
        # Words like "м." or "смт" stand before the city name: look at each word.
        parts = place.name.lower().split()
        whole_word = any(first in (part, latin(part)) for part in parts)
        word_starts = any(part.startswith(first) or latin(part).startswith(first) for part in parts)
        return (0 if contains(place.name.lower()) else 1, 0 if whole_word else 1,
                0 if word_starts else 1, len(place.path))

    return sorted(found, key=rank)


def find(all_places: Iterable[Place], region_id: str) -> Place | None:
    return next((p for p in all_places if p.region_id == region_id), None)


def fetch_tree(source: Any, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """The region tree, through the source's own request and network limits.

    HTTP 429 means the server limits the rate (the running app also polls it):
    wait and try again.
    """
    fetch_document = getattr(source, "fetch_lookup_document", None)
    if fetch_document is None or REGION_TREE_LOOKUP not in getattr(source, "lookups", {}):
        raise RegionError(f"the source {getattr(source, 'name', '?')!r} has no region tree; "
                          "choose the place in config.toml")
    wait_s = FIRST_RETRY_WAIT_S
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return fetch_document(REGION_TREE_LOOKUP)
        except RateLimitedError as error:
            if attempt == FETCH_ATTEMPTS:
                raise
            sleep(error.retry_after_s or wait_s)
            wait_s *= 2
    raise AssertionError("unreachable")


_VARS_HEADER = re.compile(r"^\[source\.vars\][^\n]*\n", re.MULTILINE)
_NEXT_TABLE = re.compile(r"^\s*\[", re.MULTILINE)


def set_place(config_text: str, place: Place) -> str:
    """config.toml with the ids of `place` in [source.vars]; all else unchanged.

    Each `<name> = "<value>"` line keeps its comment. The comment of
    `region_id` gets the place name. A missing variable gets a new line.
    """
    header = _VARS_HEADER.search(config_text)
    if header is None:
        raise RegionError("config.toml has no [source.vars] table")
    start = header.end()
    end_match = _NEXT_TABLE.search(config_text, start)
    end = end_match.start() if end_match else len(config_text)
    table = config_text[start:end]
    for name, value in place.variables().items():
        line = re.compile(rf'^({name}\s*=\s*)"[^"\n]*"([ \t]*#[^\n]*)?$', re.MULTILINE)
        match = line.search(table)
        if name == "region_id":
            comment = f"  # {place.path}"
        else:
            comment = (match.group(2) or "") if match else ""
        new_line = f'{match.group(1) if match else name + " = "}"{value}"{comment}'
        if match:
            table = table[:match.start()] + new_line + table[match.end():]
        else:
            table = new_line + "\n" + table
    return config_text[:start] + table + config_text[end:]


def current(variables: dict[str, Any], all_places: Iterable[Place]) -> Place | None:
    region_id = str(variables.get("region_id", ""))
    return find(all_places, region_id) if region_id else None
