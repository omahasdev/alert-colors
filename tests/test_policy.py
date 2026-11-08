import pytest

from alert_colors.domain import AlertStatus, Coverage
from alert_colors.policy import Policy

SCENES = {"clear", "unknown", "alert", "partial", "artillery"}
RULES = [
    {"state": "unknown", "scene": "unknown"},
    {"kinds": ["artillery"], "scene": "artillery"},
    {"coverage": "partial", "scene": "partial"},
    {"active": True, "scene": "alert"},
    {"scene": "clear"},
]


def policy(rules=RULES):
    return Policy.from_config(rules, SCENES)


def status(coverage, *kinds):
    return AlertStatus(coverage=coverage, kinds=frozenset(kinds))


def test_no_data_is_unknown_never_clear():
    assert policy().select(None) == "unknown"


def test_clear():
    assert policy().select(status(Coverage.NONE)) == "clear"


def test_rule_order_is_priority():
    assert policy().select(status(Coverage.FULL, "air_raid", "artillery")) == "artillery"
    assert policy().select(status(Coverage.PARTIAL, "artillery")) == "artillery"
    assert policy().select(status(Coverage.PARTIAL, "air_raid")) == "partial"
    assert policy().select(status(Coverage.FULL, "anything")) == "alert"


def test_unknown_status_skips_ok_rules():
    rules = [{"scene": "clear"}, {"state": "unknown", "scene": "unknown"}]
    assert policy(rules).select(None) == "unknown"


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ([{"scene": "clear"}], "unknown"),
        ([{"state": "unknown", "scene": "unknown"}], "no conditions"),
        ([{"state": "unknown", "scene": "unknown"}, {"scene": "missing"}], "undefined"),
        ([{"state": "unknown", "scene": "unknown"}, {"scene": "clear", "colour": 1}], "unknown keys"),
    ],
)
def test_invalid_rules(rules, message):
    with pytest.raises(ValueError, match=message):
        policy(rules)
