"""Values in JMESPath expressions: the forms that old configs use still work, a value never
changes an expression, and a source recovers from a lookup value that it cannot use."""
import jmespath
import pytest

from alert_colors.domain import Coverage
from alert_colors.sources import json_http
from alert_colors.sources.json_http import FieldError, create_json_http
from alert_colors.template import expand_jmespath

URL = "https://alerts.example/api"


# ---- forms of old configs -----------------------------------------------------------------
def test_a_plain_token_inside_a_json_literal_still_works():
    expression = jmespath.compile(expand_jmespath("[?contains(`[${a}, 5]`, id)].x", {"a": "31"}))
    assert expression.search([{"id": 31, "x": 1}, {"id": 5, "x": 2}, {"id": 7, "x": 3}]) == [1, 2]


def test_a_word_as_a_whole_literal_still_works():
    # A deprecated JMESPath string literal: `Alpha` reads as the string "Alpha".
    with pytest.warns(PendingDeprecationWarning):
        expression = jmespath.compile(expand_jmespath("[?name==`${n}`].x", {"n": "Alpha"}))
    assert expression.search([{"name": "Alpha", "x": 1}, {"name": "Beta", "x": 2}]) == [1]


def test_a_whole_literal_lookup_value_may_hold_an_escaped_quote():
    """Valid JSON never ends in a backslash: it cannot escape the closing backtick."""
    value = '["a\\"b"]'
    expression = jmespath.compile(expand_jmespath("`${ids}`", {"ids": value}))
    assert expression.search({}) == ['a"b']


# ---- a value never changes the expression ---------------------------------------------------
def test_a_placeholder_right_after_an_escape_is_refused():
    with pytest.raises(ValueError, match="after an escape"):
        expand_jmespath("[?name=='a\\${x}']", {"x": "y"})


def test_an_escaped_quote_before_a_placeholder_keeps_the_quoted_name_closed():
    crafted = '" || `true` || "'
    expression = jmespath.compile(expand_jmespath('states."a\\"b${x}".v', {"x": crafted}))
    key = 'a"b' + crafted
    assert expression.search({"states": {key: {"v": 1}}}) == 1  # one key, with the value in it
    assert expression.search({"states": {}}) is None  # never `true`


def test_a_control_character_in_a_raw_string_is_refused():
    with pytest.raises(ValueError):
        expand_jmespath("[?name=='${x}']", {"x": "a\x01b"})


def test_a_placeholder_with_text_after_it_is_not_a_whole_literal():
    with pytest.raises(ValueError, match="whole JSON value"):
        expand_jmespath("`${x} `", {"x": '["a"]'})


# ---- a lookup value that cannot be used ---------------------------------------------------------
def test_a_source_recovers_from_a_lookup_value_that_it_cannot_use(monkeypatch):
    """The value is forgotten: the next fetch asks for the lookup again."""
    answers = iter([{"name": "x`y"}, {"name": "here"}])  # first a value with a backtick, then a good one
    documents = {URL: {"alerts": {"here": True}}}

    def fake_get(request, network):
        if request["url"] == URL + "/place":
            return next(answers)
        return documents[request["url"]]

    monkeypatch.setattr(json_http, "_get", fake_get)
    source = create_json_http({
        "request": {"url": URL, "timeout_s": 1},
        "fields": {"full": "contains(`${place}`, 'here')"},  # the lookup value fills a JSON literal
        "lookups": {"place": {"url": URL + "/place", "expression": "name"}},
    })
    with pytest.raises(FieldError, match="lookup 'place'"):
        source.fetch()
    assert source.fetch().coverage is Coverage.FULL


def test_a_json_value_that_is_not_a_string_is_refused_outside_a_literal():
    """Outside a literal, only a plain token or one JSON string fits: `[0]` would add structure."""
    with pytest.raises(ValueError, match="outside a literal"):
        expand_jmespath("alerts.${x}", {"x": "[0]"})
    assert expand_jmespath("alerts.${x}", {"x": '"a b"'}) == 'alerts."a b"'


def test_a_too_large_integer_in_the_tuning_file_is_a_tuning_error():
    from alert_colors.tuning import TuningError, _number

    with pytest.raises(TuningError, match="not a number"):
        _number(10 ** 400, "display.brightness")


def test_a_status_from_the_change_marker_keeps_the_age_of_the_full_answer(monkeypatch):
    """An unchanged marker confirms the data: its age stays what it was at the full request.
    Old data stays old, and fresh data stays fresh between two full requests."""
    from datetime import timedelta

    documents = {URL: {"on": False, "time": "2026-01-01T00:00:00Z"}, URL + "/marker": {"index": 7}}
    requests = []
    monkeypatch.setattr(json_http, "_get",
                        lambda request, network: requests.append(request["url"]) or documents[request["url"]])
    source = create_json_http({
        "request": {"url": URL, "timeout_s": 1},
        "fields": {"full": "on", "observed_at": "time"},
        "time": {"format": "iso"},
        "change_marker": {"url": URL + "/marker", "field": "index", "max_age_s": 600},
    })
    clock = {"t": 0.0}
    source.monotonic = lambda: clock["t"]
    full = source.fetch()
    clock["t"] = 100.0
    from_marker = source.fetch()
    assert requests[-1] == URL + "/marker"  # no full request
    assert from_marker.observed_at == full.observed_at + timedelta(seconds=100)
