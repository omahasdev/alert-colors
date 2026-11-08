"""A scene is what the LEDs do: run an effect, or give the LEDs back.

    [scenes.<name>]
    effect = "pulse"          # any registered effect, with its options

    [scenes.<name>]
    release = true            # each backend gives its LEDs back to its vendor app (Armoury Crate, Synapse)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .effects import EFFECTS, Effect


@dataclass(frozen=True)
class Scene:
    name: str
    effect: Effect | None = None
    release: bool = False


def build_scenes(raw: dict[str, dict[str, Any]]) -> dict[str, Scene]:
    scenes = {}
    for name, options in raw.items():
        release = bool(options.get("release", False))
        if release == ("effect" in options):
            raise ValueError(f"scene {name!r}: set either `effect` or `release = true`")
        effect = None if release else EFFECTS.create(options["effect"], options)
        scenes[name] = Scene(name=name, effect=effect, release=release)
    return scenes
