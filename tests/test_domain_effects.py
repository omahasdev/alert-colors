import pytest

from alert_colors.domain import Color
from alert_colors.effects import EFFECTS, Blink, Pulse

RED = Color(255, 0, 0)


def test_hex_and_bgr_packing():
    color = Color.from_hex("#102030")
    assert (color.r, color.g, color.b) == (0x10, 0x20, 0x30)
    assert color.to_bgr_int() == 0x302010


def test_invalid_hex():
    with pytest.raises(ValueError):
        Color.from_hex("#123")


def test_pulse_goes_from_min_to_full_and_back():
    pulse = Pulse(RED, period_s=2.0, min_brightness=0.2)
    assert pulse.color_at(0.0) == RED.scaled(0.2)
    assert pulse.color_at(1.0) == RED
    assert pulse.color_at(2.0) == RED.scaled(0.2)


def test_blink_duty():
    blink = Blink(RED, period_s=1.0, duty=0.5)
    assert blink.color_at(0.25) == RED
    assert blink.color_at(0.75) == Color(0, 0, 0)


def test_registry_builds_effect_with_brightness():
    effect = EFFECTS.create("solid", {"color": "#FF0000", "brightness": 0.5})
    assert effect.color_at(0) == RED.scaled(0.5)


def test_registry_unknown_name():
    with pytest.raises(ValueError, match="unknown effect"):
        EFFECTS.create("no_such_effect", {})


def test_pulse_max_brightness():
    pulse = Pulse(RED, period_s=2.0, min_brightness=0.5, max_brightness=0.8)
    assert pulse.color_at(0.0) == RED.scaled(0.5)
    assert pulse.color_at(1.0) == RED.scaled(0.8)


def test_pulse_rejects_min_above_max():
    with pytest.raises(ValueError):
        Pulse(RED, period_s=1.0, min_brightness=0.9, max_brightness=0.5)


def test_scene_needs_effect_or_release():
    from alert_colors.scenes import build_scenes

    assert build_scenes({"clear": {"release": True}})["clear"].release
    with pytest.raises(ValueError):
        build_scenes({"bad": {}})
