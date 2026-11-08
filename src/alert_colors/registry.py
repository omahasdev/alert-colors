"""A tiny named-factory registry. Sources, lighting backends and effects use it
so that new implementations plug in by name from the config file."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

T = TypeVar("T")
Factory = Callable[[dict[str, Any]], T]


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._factories: dict[str, Factory[T]] = {}

    def register(self, name: str) -> Callable[[Factory[T]], Factory[T]]:
        def decorator(factory: Factory[T]) -> Factory[T]:
            if name in self._factories:
                raise ValueError(f"{self._kind} {name!r} is already registered")
            self._factories[name] = factory
            return factory

        return decorator

    def create(self, name: str, options: dict[str, Any]) -> T:
        try:
            factory = self._factories[name]
        except KeyError:
            known = ", ".join(sorted(self._factories)) or "none"
            raise ValueError(f"unknown {self._kind} {name!r}; known: {known}") from None
        return factory(options)

    def names(self) -> list[str]:
        return sorted(self._factories)
