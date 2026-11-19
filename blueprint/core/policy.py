"""Rule shape without rule content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .model import Context

C = TypeVar("C")
V = TypeVar("V")


@dataclass(frozen=True)
class Decision(Generic[V]):
    allowed: bool
    value: V | None = None
    reason: str = ""


class Policy(Protocol[C, V]):
    name: str

    def evaluate(self, candidate: C, ctx: Context) -> Decision[V]: ...
