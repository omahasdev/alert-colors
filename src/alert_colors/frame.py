"""A frame: what the LEDs show at one moment.

An effect fills a frame for any number of LEDs: `colors(count)` gives one
color for each LED, from the first LED to the last. A single-color effect
fills every LED with the same color (`uniform` is then true), so backends
that have one color only (a mouse) or that are slow (COM) can take
`color()` instead.

Brightness and color correction are transforms on the frame. The daemon adds
the global brightness; each backend worker adds its own brightness and color
correction. The effect stays unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .domain import Color

Transform = Callable[[Color], Color]
FIRST_LED = 1  # a one-LED device shows the color of the first position


class SpatialEffect(Protocol):
    uniform: bool

    def colors(self, elapsed_s: float, count: int) -> list[Color]: ...


@dataclass(frozen=True)
class Frame:
    effect: SpatialEffect
    elapsed_s: float
    transforms: tuple[Transform, ...] = field(default=())

    @property
    def uniform(self) -> bool:
        return self.effect.uniform

    def colors(self, count: int) -> list[Color]:
        raw = self.effect.colors(self.elapsed_s, count)
        for transform in self.transforms:
            raw = [transform(color) for color in raw]
        return raw

    def color(self) -> Color:
        return self.colors(FIRST_LED)[0]

    def map(self, transform: Transform) -> Frame:
        return Frame(self.effect, self.elapsed_s, (*self.transforms, transform))
