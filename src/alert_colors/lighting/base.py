from __future__ import annotations

from typing import Protocol

from ..frame import Frame
from ..registry import Registry


class LightingBackend(Protocol):
    """One lighting system (a vendor SDK, a device, a debug sink).

    Lifecycle: `show()` takes control on first use. `release()` gives the LEDs
    back to the vendor app (for example, Armoury Crate) and the next `show()`
    takes control again. The daemon calls `show()` on every frame, so a backend
    must skip work when the colors did not change and send keep-alives itself.
    A frame gives one color for each LED (frame.colors(n)), or one color for
    a device with one color only (frame.color()).
    """

    name: str

    def show(self, frame: Frame) -> None: ...

    def release(self) -> None: ...

    def reclaim(self) -> None:
        """Take the LEDs again, and send the next frame even if it did not change.

        A vendor app can take the LEDs back without a message to the app (the
        ASUS service does it each time the display turns on). Only call it while
        the backend controls the LEDs; without control, do nothing.
        """
        ...

    def describe(self) -> list[str]:
        """Human-readable list of the devices this backend controls."""
        ...


BACKENDS: Registry[LightingBackend] = Registry("lighting backend")
