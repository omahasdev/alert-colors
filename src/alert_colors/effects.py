"""Effects: what a scene does with the LEDs over time.

Every effect is a pure function of the elapsed time and the LED position, so it
is easy to test, and the same effect drives every backend. A single-color
effect is `uniform`: all LEDs show the same color. A spatial effect gives each
LED its own color (gradients, waves, comets); a backend with one color only
shows the first position.

Every effect declares its options as a schema of `Param`s. The registry
validates config values against it, and the tuner UI builds its forms from it.
New effects: decorate a builder with `@effect("name", "Title", [Param, ...])`.

The set follows the common effects of Armoury Crate (Aura), Razer Synapse and
OpenRGB: static, breathing, strobing, color cycle, rainbow, color wave,
gradient, comet, scanner (glowing yoyo), starry night, marquee and fire.
"""

from __future__ import annotations

import colorsys
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .domain import BLACK, FULL_BRIGHTNESS, NO_BRIGHTNESS, Color
from .registry import Registry

# ------------------------------------------------------------------ schema ----
COLOR, COLORS, FLOAT, INT, BOOL, CHOICE = "color", "colors", "float", "int", "bool", "choice"
FORWARD, BACKWARD = 1, -1
DIRECTIONS = ("forward", "backward")


@dataclass(frozen=True)
class Param:
    name: str
    kind: str
    default: Any
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()

    def parse(self, raw: Any) -> Any:
        if self.kind == COLOR:
            return Color.from_hex(str(raw))
        if self.kind == COLORS:
            colors = [Color.from_hex(str(item)) for item in raw]
            if len(colors) < (1 if self.min is None else self.min):  # min = the shortest list
                raise ValueError(f"{self.name}: needs at least {self.min or 1} color(s)")
            return colors
        if self.kind == BOOL:
            return bool(raw)
        if self.kind == CHOICE:
            if raw not in self.choices:
                raise ValueError(f"{self.name}: {raw!r} is not one of {self.choices}")
            return raw
        value = int(raw) if self.kind == INT else float(raw)
        if self.min is not None and value < self.min or self.max is not None and value > self.max:
            raise ValueError(f"{self.name}: {value} is outside {self.min}..{self.max}")
        return value

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, (), "")} | {"default": self.default}


@dataclass(frozen=True)
class EffectSpec:
    name: str
    title: str
    params: tuple[Param, ...]
    build: Callable[[dict[str, Any]], Any]

    def create(self, options: dict[str, Any]) -> Any:
        known = {p.name for p in self.params}
        values = {}
        for param in self.params:
            raw = options.get(param.name, param.default)
            values[param.name] = param.parse(raw)
        unknown = set(options) - known - {"effect"}
        if unknown:
            raise ValueError(f"effect {self.name!r}: unknown options {sorted(unknown)}; known: {sorted(known)}")
        return self.build(values)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "params": [p.to_json() for p in self.params]}


class Effect(Protocol):
    """What every effect gives: one color for each LED, at any time."""

    uniform: bool

    def colors(self, elapsed_s: float, count: int) -> list[Color]: ...

    def color_at(self, elapsed_s: float) -> Color: ...


EFFECTS: Registry[Effect] = Registry("effect")
SPECS: dict[str, EffectSpec] = {}


def effect(name: str, title: str, params: Sequence[Param]) -> Callable:
    def decorator(build: Callable[[dict[str, Any]], Any]) -> Callable:
        spec = EffectSpec(name, title, tuple(params), build)
        SPECS[name] = spec
        EFFECTS.register(name)(spec.create)
        return build

    return decorator


# --------------------------------------------------------------- helpers ----
def _lerp(a: Color, b: Color, t: float) -> Color:
    t = min(max(t, NO_BRIGHTNESS), FULL_BRIGHTNESS)
    return Color(round(a.r + (b.r - a.r) * t), round(a.g + (b.g - a.g) * t), round(a.b + (b.b - a.b) * t))


def _palette(colors: Sequence[Color], x: float, cyclic: bool) -> Color:
    """The color at x in 0..1 along `colors`, blended smoothly."""
    if len(colors) == 1:
        return colors[0]
    x = x % 1.0 if cyclic else min(max(x, 0.0), 1.0)
    segments = len(colors) if cyclic else len(colors) - 1
    scaled = x * segments
    index = min(int(scaled), segments - 1)
    return _lerp(colors[index], colors[(index + 1) % len(colors)], scaled - index)


def _hue(h: float, saturation: float, value: float) -> Color:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, saturation, value)
    return Color(round(r * 255), round(g * 255), round(b * 255))


def _positions(count: int) -> list[float]:
    """LED i sits at the middle of its share of 0..1."""
    return [(i + 0.5) / count for i in range(count)]


def _phase(elapsed_s: float, period_s: float) -> float:
    return (elapsed_s % period_s) / period_s if period_s > 0 else 0.0


_HASH_MASK = 0xFFFFFFFF
_HASH_SCALE = float(1 << 32)


def _noise(i: int, k: int, seed: int) -> float:
    """A fixed pseudo-random value in 0..1 for LED i in time step k (no state)."""
    x = (i * 0x9E3779B1 + k * 0x85EBCA77 + seed * 0xC2B2AE3D) & _HASH_MASK
    x ^= x >> 15
    x = (x * 0x2C1B3C6D) & _HASH_MASK
    x ^= x >> 12
    x = (x * 0x297A2D39) & _HASH_MASK
    x ^= x >> 15
    return x / _HASH_SCALE


def _smooth_noise(i: int, t: float, seed: int) -> float:
    k = math.floor(t)
    f = t - k
    f = f * f * (3 - 2 * f)  # smoothstep
    return _noise(i, k, seed) * (1 - f) + _noise(i, k + 1, seed) * f


# ------------------------------------------------------------ base classes ----
@dataclass(frozen=True)
class Uniform:
    """Base for single-color effects: every LED shows color_at(t)."""

    uniform = True

    def color_at(self, elapsed_s: float) -> Color:
        raise NotImplementedError

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        return [self.color_at(elapsed_s)] * count


@dataclass(frozen=True)
class Spatial:
    """Base for effects with one color for each LED position (0..1)."""

    uniform = False

    def color_at(self, elapsed_s: float) -> Color:
        return self.colors(elapsed_s, 1)[0]

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        raise NotImplementedError


# common params
P_COLOR = Param("color", COLOR, "#FF0000", "Main color")
P_BACKGROUND = Param("background", COLOR, "#000000", "Color of the LEDs that are not lit")
P_BRIGHTNESS = Param("brightness", FLOAT, FULL_BRIGHTNESS, "Brightness factor", 0, 1, 0.05)
P_DIRECTION = Param("direction", CHOICE, "forward", "Direction of the movement", choices=DIRECTIONS)


def _period(default: float, help_text: str = "Duration of one cycle, seconds") -> Param:
    return Param("period_s", FLOAT, default, help_text, 0.05, 600, 0.05)


def _sign(direction: str) -> int:
    return FORWARD if direction == "forward" else BACKWARD


# ======================================================== uniform effects ====
@dataclass(frozen=True)
class Solid(Uniform):
    color: Color

    def color_at(self, elapsed_s: float) -> Color:
        return self.color


@effect("solid", "Static", [P_COLOR, P_BRIGHTNESS])
def _solid(v: dict[str, Any]) -> Solid:
    return Solid(v["color"].scaled(v["brightness"]))


@dataclass(frozen=True)
class Pulse(Uniform):
    """Smooth sine breathing between min and max brightness."""

    color: Color
    period_s: float
    min_brightness: float
    max_brightness: float = FULL_BRIGHTNESS

    def __post_init__(self) -> None:
        if not NO_BRIGHTNESS <= self.min_brightness <= self.max_brightness <= FULL_BRIGHTNESS:
            raise ValueError("pulse needs 0 <= min_brightness <= max_brightness <= 1")
        if self.period_s <= 0:
            raise ValueError("pulse needs period_s > 0")

    def color_at(self, elapsed_s: float) -> Color:
        wave = (1 - math.cos(2 * math.pi * _phase(elapsed_s, self.period_s))) / 2  # 0 -> 1 -> 0
        return self.color.scaled(self.min_brightness + (self.max_brightness - self.min_brightness) * wave)


@effect("pulse", "Breathing", [
    P_COLOR, _period(2.0),
    Param("min_brightness", FLOAT, NO_BRIGHTNESS, "Brightness at the low point", 0, 1, 0.05),
    Param("max_brightness", FLOAT, FULL_BRIGHTNESS, "Brightness at the high point", 0, 1, 0.05),
])
def _pulse(v: dict[str, Any]) -> Pulse:
    return Pulse(v["color"], v["period_s"], v["min_brightness"], v["max_brightness"])


@dataclass(frozen=True)
class Blink(Uniform):
    """On for `duty` part of each period, then `off_color`."""

    color: Color
    period_s: float
    duty: float
    off_color: Color = BLACK

    def __post_init__(self) -> None:
        if self.period_s <= 0:
            raise ValueError("blink needs period_s > 0")

    def color_at(self, elapsed_s: float) -> Color:
        return self.color if _phase(elapsed_s, self.period_s) < self.duty else self.off_color


@effect("blink", "Blink", [
    P_COLOR, _period(1.0), Param("duty", FLOAT, 0.5, "Part of the period that is on", 0, 1, 0.05),
    P_BRIGHTNESS, Param("off_color", COLOR, "#000000", "Color in the off part"),
])
def _blink(v: dict[str, Any]) -> Blink:
    return Blink(v["color"].scaled(v["brightness"]), v["period_s"], v["duty"], v["off_color"])


@dataclass(frozen=True)
class Strobe(Uniform):
    """`flashes` short flashes at the start of each period (Aura "Strobing")."""

    color: Color
    period_s: float
    flashes: int
    flash_s: float
    off_color: Color

    def color_at(self, elapsed_s: float) -> Color:
        t = elapsed_s % self.period_s
        slot = self.flash_s * 2  # one flash and one gap
        on = t < slot * self.flashes and (t % slot) < self.flash_s
        return self.color if on else self.off_color


@effect("strobe", "Strobing", [
    P_COLOR, _period(1.5), Param("flashes", INT, 2, "Flashes in each period", 1, 10, 1),
    Param("flash_s", FLOAT, 0.06, "Duration of one flash, seconds", 0.02, 1, 0.01),
    P_BRIGHTNESS, Param("off_color", COLOR, "#000000", "Color between flashes"),
])
def _strobe(v: dict[str, Any]) -> Strobe:
    return Strobe(v["color"].scaled(v["brightness"]), v["period_s"], v["flashes"], v["flash_s"], v["off_color"])


@dataclass(frozen=True)
class ColorCycle(Uniform):
    """All LEDs move through the colors (or the whole spectrum) together."""

    colors_list: tuple[Color, ...]
    period_s: float
    saturation: float
    brightness: float
    smooth: bool

    def color_at(self, elapsed_s: float) -> Color:
        phase = _phase(elapsed_s, self.period_s)
        if not self.colors_list:
            return _hue(phase, self.saturation, self.brightness)
        if not self.smooth:
            return self.colors_list[int(phase * len(self.colors_list)) % len(self.colors_list)].scaled(self.brightness)
        return _palette(self.colors_list, phase, cyclic=True).scaled(self.brightness)


@effect("color_cycle", "Color cycle / Spectrum cycling", [
    Param("colors", COLORS, [], "Colors to cycle; empty: the whole spectrum", min=0), _period(10.0),
    Param("saturation", FLOAT, FULL_BRIGHTNESS, "Saturation of the spectrum", 0, 1, 0.05),
    P_BRIGHTNESS, Param("smooth", BOOL, True, "Blend between colors (off: jump)"),
])
def _color_cycle(v: dict[str, Any]) -> ColorCycle:
    raw = v["colors"] if v["colors"] else ()
    return ColorCycle(tuple(raw), v["period_s"], v["saturation"], v["brightness"], v["smooth"])


# ======================================================== spatial effects ====
@dataclass(frozen=True)
class Rainbow(Spatial):
    """The spectrum across the LEDs; moving when period_s > 0 (rainbow wave)."""

    period_s: float
    repeats: float
    direction: int
    saturation: float
    brightness: float
    moving: bool

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        shift = _phase(elapsed_s, self.period_s) * self.direction if self.moving else 0.0
        return [_hue(p * self.repeats - shift, self.saturation, self.brightness) for p in _positions(count)]


_REPEATS = Param("repeats", FLOAT, 1.0, "How many times the pattern fits on the LEDs", 0.1, 20, 0.1)
_MOVING = Param("moving", BOOL, True, "Move the pattern (off: static)")


@effect("rainbow", "Rainbow / Rainbow wave", [
    _period(5.0), _REPEATS, P_DIRECTION,
    Param("saturation", FLOAT, FULL_BRIGHTNESS, "Saturation", 0, 1, 0.05), P_BRIGHTNESS, _MOVING,
])
def _rainbow(v: dict[str, Any]) -> Rainbow:
    return Rainbow(v["period_s"], v["repeats"], _sign(v["direction"]), v["saturation"], v["brightness"], v["moving"])


@dataclass(frozen=True)
class Gradient(Spatial):
    """The colors blended across the LEDs; moving when `moving` (color wave)."""

    colors_list: tuple[Color, ...]
    period_s: float
    repeats: float
    direction: int
    brightness: float
    moving: bool

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        shift = _phase(elapsed_s, self.period_s) * self.direction if self.moving else 0.0
        cyclic = self.moving or self.repeats != 1
        return [_palette(self.colors_list, p * self.repeats - shift, cyclic).scaled(self.brightness)
                for p in _positions(count)]


@effect("gradient", "Gradient / Color wave", [
    Param("colors", COLORS, ["#FF0000", "#0000FF"], "Colors along the LEDs"), _period(6.0),
    _REPEATS, P_DIRECTION, P_BRIGHTNESS, Param("moving", BOOL, False, "Move the gradient (color wave)"),
])
def _gradient(v: dict[str, Any]) -> Gradient:
    return Gradient(tuple(v["colors"]), v["period_s"], v["repeats"], _sign(v["direction"]),
                    v["brightness"], v["moving"])


@dataclass(frozen=True)
class Wave(Spatial):
    """A brightness wave of one color that moves across the LEDs."""

    color: Color
    background: Color
    period_s: float
    repeats: float
    direction: int
    min_brightness: float
    max_brightness: float

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        shift = _phase(elapsed_s, self.period_s) * self.direction
        out = []
        for p in _positions(count):
            level = (1 + math.sin(2 * math.pi * (p * self.repeats - shift))) / 2
            level = self.min_brightness + (self.max_brightness - self.min_brightness) * level
            out.append(_lerp(self.background, self.color, level))
        return out


@effect("wave", "Wave", [
    P_COLOR, P_BACKGROUND, _period(3.0), _REPEATS, P_DIRECTION,
    Param("min_brightness", FLOAT, NO_BRIGHTNESS, "Level in the wave trough", 0, 1, 0.05),
    Param("max_brightness", FLOAT, FULL_BRIGHTNESS, "Level at the wave crest", 0, 1, 0.05),
])
def _wave(v: dict[str, Any]) -> Wave:
    return Wave(v["color"], v["background"], v["period_s"], v["repeats"], _sign(v["direction"]),
                v["min_brightness"], v["max_brightness"])


@dataclass(frozen=True)
class Comet(Spatial):
    """A bright head with a fading tail runs across the LEDs; `bounce` runs back
    and forth (scanner / glowing yoyo)."""

    color: Color
    background: Color
    period_s: float
    tail: float
    direction: int
    bounce: bool

    def _head(self, elapsed_s: float) -> float:
        phase = _phase(elapsed_s, self.period_s)
        if self.bounce:
            return 1 - abs(1 - 2 * phase)  # 0 -> 1 -> 0
        return phase if self.direction == FORWARD else 1 - phase

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        head = self._head(elapsed_s)
        moving_forward = (self.direction == FORWARD) if not self.bounce else _phase(elapsed_s, self.period_s) < 0.5
        out = []
        for p in _positions(count):
            behind = (head - p) if moving_forward else (p - head)
            if not self.bounce:
                behind %= 1.0  # the tail wraps around for a running comet
            level = 1 - behind / self.tail if 0 <= behind <= self.tail else 0.0
            out.append(_lerp(self.background, self.color, level))
        return out


_TAIL = Param("tail", FLOAT, 0.3, "Tail length, part of the LEDs", 0.01, 1, 0.01)


@effect("comet", "Comet / Flash and dash", [P_COLOR, P_BACKGROUND, _period(2.0), _TAIL, P_DIRECTION])
def _comet(v: dict[str, Any]) -> Comet:
    return Comet(v["color"], v["background"], v["period_s"], v["tail"], _sign(v["direction"]), bounce=False)


@effect("scanner", "Scanner / Glowing yoyo", [P_COLOR, P_BACKGROUND, _period(2.0), _TAIL])
def _scanner(v: dict[str, Any]) -> Comet:
    return Comet(v["color"], v["background"], v["period_s"], v["tail"], FORWARD, bounce=True)


@dataclass(frozen=True)
class Twinkle(Spatial):
    """Random LEDs light up and fade out (starry night / starlight)."""

    colors_list: tuple[Color, ...]
    background: Color
    density: float
    period_s: float
    brightness: float
    seed: int

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        out = []
        for i in range(count):
            offset = _noise(i, 0, self.seed)  # each LED has its own rhythm
            t = elapsed_s / self.period_s + offset
            cycle = math.floor(t)
            if _noise(i, cycle, self.seed + 1) >= self.density:
                out.append(self.background)
                continue
            level = math.sin(math.pi * (t - cycle))  # fade in and out once
            star = self.colors_list[int(_noise(i, cycle, self.seed + 2) * len(self.colors_list))]
            out.append(_lerp(self.background, star.scaled(self.brightness), level))
        return out


@effect("twinkle", "Starry night / Twinkle", [
    Param("colors", COLORS, ["#FFFFFF"], "Colors of the stars"), P_BACKGROUND,
    Param("density", FLOAT, 0.3, "Part of the LEDs that light up in each cycle", 0, 1, 0.05),
    _period(2.0, "Duration of one twinkle, seconds"), P_BRIGHTNESS,
    Param("seed", INT, 1, "Random pattern number", 0, 1000, 1),
])
def _twinkle(v: dict[str, Any]) -> Twinkle:
    return Twinkle(tuple(v["colors"]), v["background"], v["density"], v["period_s"], v["brightness"], v["seed"])


@dataclass(frozen=True)
class Marquee(Spatial):
    """Lit groups with gaps that run along the LEDs (theater chase)."""

    color: Color
    background: Color
    spacing: int
    lit: int
    period_s: float
    direction: int

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        step = int(_phase(elapsed_s, self.period_s) * self.spacing) * self.direction
        return [self.color if (i - step) % self.spacing < self.lit else self.background for i in range(count)]


@effect("marquee", "Marquee / Theater chase", [
    P_COLOR, P_BACKGROUND, Param("spacing", INT, 4, "LEDs in one group with its gap", 2, 50, 1),
    Param("lit", INT, 1, "Lit LEDs in each group", 1, 49, 1),
    _period(1.0, "Time for one group to move one spacing, seconds"), P_DIRECTION,
])
def _marquee(v: dict[str, Any]) -> Marquee:
    if v["lit"] >= v["spacing"]:
        raise ValueError("marquee needs lit < spacing")
    return Marquee(v["color"], v["background"], v["spacing"], v["lit"], v["period_s"], _sign(v["direction"]))


@dataclass(frozen=True)
class Fire(Spatial):
    """Flickering flames: each LED moves through the palette with smooth noise."""

    palette: tuple[Color, ...]
    speed: float
    flicker: float
    brightness: float
    seed: int

    def colors(self, elapsed_s: float, count: int) -> list[Color]:
        out = []
        for i in range(count):
            heat = 1 - self.flicker * _smooth_noise(i, elapsed_s * self.speed, self.seed)
            out.append(_palette(self.palette, heat, cyclic=False).scaled(self.brightness))
        return out


@effect("fire", "Fire", [
    Param("colors", COLORS, ["#200000", "#FF2000", "#FF8000"], "Palette from cold to hot"),
    Param("speed", FLOAT, 3.0, "Flicker steps per second", 0.1, 30, 0.1),
    Param("flicker", FLOAT, 0.6, "How far the flames cool down", 0, 1, 0.05), P_BRIGHTNESS,
    Param("seed", INT, 1, "Random pattern number", 0, 1000, 1),
])
def _fire(v: dict[str, Any]) -> Fire:
    return Fire(tuple(v["colors"]), v["speed"], v["flicker"], v["brightness"], v["seed"])


def schema() -> list[dict[str, Any]]:
    """Every effect with its options, for the tuner UI."""
    return [spec.to_json() for spec in SPECS.values()]


__all__ = ["EFFECTS", "SPECS", "Blink", "Effect", "EffectSpec", "Param", "Pulse", "Solid", "effect", "schema"]
