import json

import pytest

from alert_colors.domain import Color
from alert_colors.effects import EFFECTS, SPECS, schema
from alert_colors.frame import Frame

RED, BLUE, BLACK = Color(255, 0, 0), Color(0, 0, 255), Color(0, 0, 0)
LEDS = 10


@pytest.mark.parametrize("name", sorted(SPECS))
def test_every_effect_builds_from_its_defaults_and_fills_any_led_count(name):
    effect = EFFECTS.create(name, {})
    for count in (1, 4, LEDS, 120):
        colors = Frame(effect, 1.234).colors(count)
        assert len(colors) == count and all(isinstance(c, Color) for c in colors)


def test_schema_is_json_for_the_ui():
    data = json.loads(json.dumps(schema()))
    names = {e["name"] for e in data}
    assert {"solid", "pulse", "blink", "strobe", "color_cycle", "rainbow", "gradient", "wave",
            "comet", "scanner", "twinkle", "marquee", "fire"} <= names
    pulse = next(e for e in data if e["name"] == "pulse")
    assert {p["name"] for p in pulse["params"]} == {"color", "period_s", "min_brightness", "max_brightness"}


def test_unknown_option_and_out_of_range_are_errors():
    with pytest.raises(ValueError, match="unknown options"):
        EFFECTS.create("solid", {"colour": "#FF0000"})
    with pytest.raises(ValueError, match="outside"):
        EFFECTS.create("pulse", {"min_brightness": 2})


def test_static_gradient_goes_from_first_to_last_color():
    g = EFFECTS.create("gradient", {"colors": ["#FF0000", "#0000FF"]})
    colors = g.colors(0.0, LEDS)
    assert colors[0].r > colors[-1].r and colors[0].b < colors[-1].b
    assert not g.uniform and EFFECTS.create("solid", {}).uniform


def test_moving_effects_change_over_time_static_ones_do_not():
    moving = EFFECTS.create("rainbow", {"period_s": 4})
    static = EFFECTS.create("rainbow", {"moving": False})
    assert moving.colors(0.0, LEDS) != moving.colors(1.0, LEDS)
    assert static.colors(0.0, LEDS) == static.colors(1.0, LEDS)


def test_comet_head_is_brightest_and_moves():
    comet = EFFECTS.create("comet", {"color": "#FF0000", "period_s": 1.0, "tail": 0.3})
    early, late = comet.colors(0.25, LEDS), comet.colors(0.75, LEDS)
    assert max(range(LEDS), key=lambda i: early[i].r) < max(range(LEDS), key=lambda i: late[i].r)


def test_marquee_pattern():
    m = EFFECTS.create("marquee", {"color": "#FF0000", "spacing": 3, "lit": 1, "period_s": 3})
    assert m.colors(0.0, 6) == [RED, BLACK, BLACK, RED, BLACK, BLACK]
    with pytest.raises(ValueError):
        EFFECTS.create("marquee", {"spacing": 2, "lit": 2})


def test_random_effects_are_deterministic():
    for name in ("twinkle", "fire"):
        e = EFFECTS.create(name, {})
        assert e.colors(3.3, LEDS) == e.colors(3.3, LEDS)


def test_strobe_flashes():
    s = EFFECTS.create("strobe", {"color": "#FF0000", "period_s": 1.0, "flashes": 2, "flash_s": 0.1})
    assert [s.color_at(t) for t in (0.05, 0.15, 0.25, 0.5)] == [RED, BLACK, RED, BLACK]


def test_color_cycle_through_given_colors():
    c = EFFECTS.create("color_cycle", {"colors": ["#FF0000", "#0000FF"], "period_s": 2, "smooth": False})
    assert c.color_at(0.1) == RED and c.color_at(1.1) == BLUE


def test_shipped_scenes_are_unchanged():
    from pathlib import Path

    from alert_colors.config import App

    scenes = App.load(Path(__file__).resolve().parents[1] / "config.toml").scenes()
    alert = scenes["alert"].effect
    assert type(alert).__name__ == "Pulse" and alert.min_brightness == 0.5 and alert.period_s == 1.5
    assert type(scenes["clear"].effect).__name__ == "Solid"
