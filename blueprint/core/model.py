"""Neutral data primitives shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Error:
    code: str
    message: str
    kind: str = "technical"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Result(Generic[T]):
    ok: bool
    value: T | None = None
    error: Error | None = None

    @classmethod
    def success(cls, value: T) -> "Result[T]":
        return cls(True, value=value)

    @classmethod
    def failure(cls, error: Error) -> "Result[T]":
        return cls(False, error=error)


@dataclass(frozen=True)
class Context:
    request_id: str
    actor: str
    occurred_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Envelope:
    command: str
    payload: dict[str, Any]
