"""Abstract interaction ports. Concrete implementations live outside this package."""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

from .model import Context, Result


@runtime_checkable
class Handler(Protocol):
    def handle(self, command: str, payload: dict[str, Any], ctx: Context) -> Result[Any]: ...


class Reader(Protocol):
    def read(self, key: str, ctx: Context) -> Iterable[dict[str, Any]]: ...


class Writer(Protocol):
    def write(self, key: str, records: Iterable[dict[str, Any]], ctx: Context) -> int: ...


class Notifier(Protocol):
    def notify(self, channel: str, payload: dict[str, Any], ctx: Context) -> None: ...


class Clock(Protocol):
    def now(self) -> str: ...


class UnitOfWork(Protocol):
    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...
