"""Command registry. Handlers: (dev, data, primary) -> (response code, response data)."""
from __future__ import annotations

from typing import Callable

REGISTRY: dict[int, tuple[Callable, bool]] = {}


class ShortData(Exception):
    """Request data shorter than the command needs (response code 5)."""


def need(data: bytes, n: int) -> None:
    if len(data) < n:
        raise ShortData(f"need {n} data bytes, got {len(data)}")


def register(number: int, *, write: bool = False):
    """Register a command handler; `write=True` handlers are refused in write protect."""
    def deco(fn):
        REGISTRY[number] = (fn, write)
        return fn
    return deco


from . import universal  # noqa: E402,F401  (registers commands on import)
