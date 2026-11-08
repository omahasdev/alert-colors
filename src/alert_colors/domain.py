"""Core value types shared by every layer. No I/O here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

CHANNEL_MAX = 255
HEX_COLOR_LENGTH = 6


class Coverage(StrEnum):
    """How much of the watched place the active alerts cover."""

    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"


@dataclass(frozen=True)
class AlertStatus:
    """One observation of the watched place, as reported by a source.

    `kinds` are free names (for example "air_raid", "artillery"). A source maps
    the values of its API to these names; rules in config.toml match on them.
    """

    coverage: Coverage
    kinds: frozenset[str] = field(default_factory=frozenset)
    observed_at: datetime | None = None  # when the provider produced the data (aware)
    changed_at: datetime | None = None  # when the alert state last changed (aware)
    source: str = ""

    @property
    def active(self) -> bool:
        return self.coverage is not Coverage.NONE


@dataclass(frozen=True)
class Color:
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for channel in (self.r, self.g, self.b):
            if not 0 <= channel <= CHANNEL_MAX:
                raise ValueError(f"color channel out of range: {channel}")

    @classmethod
    def from_hex(cls, text: str) -> Color:
        value = text.strip().lstrip("#")
        if len(value) != HEX_COLOR_LENGTH:
            raise ValueError(f"expected #RRGGBB, got {text!r}")
        return cls(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))

    def scaled(self, factor: float) -> Color:
        factor = min(max(factor, NO_BRIGHTNESS), FULL_BRIGHTNESS)
        return Color(round(self.r * factor), round(self.g * factor), round(self.b * factor))

    def corrected(self, factors: tuple[float, float, float]) -> Color:
        """Scale each channel: color correction for LEDs whose channels differ in brightness."""
        fr, fg, fb = (min(max(f, NO_BRIGHTNESS), FULL_BRIGHTNESS) for f in factors)
        return Color(round(self.r * fr), round(self.g * fg), round(self.b * fb))

    def to_bgr_int(self) -> int:
        """0x00BBGGRR, the packing used by the ASUS Aura SDK and Razer Chroma."""
        return (self.b << 16) | (self.g << 8) | self.r

    def to_hex(self) -> str:
        return f"#{self.r:02X}{self.g:02X}{self.b:02X}"


BLACK = Color(0, 0, 0)
FULL_BRIGHTNESS = 1.0  # brightness factor: colors as configured
NO_BRIGHTNESS = 0.0  # brightness factor: off
