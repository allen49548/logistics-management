"""Adapters implement core ports with technical I/O only."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from blueprint.core.model import Context
from blueprint.core.ports import Clock, Notifier, Reader, Writer


class InMemoryReader:
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._rows = rows or {}

    def read(self, key: str, ctx: Context) -> Iterable[dict[str, Any]]:
        return tuple(self._rows.get(key, ()))


class InMemoryWriter:
    def __init__(self) -> None:
        self.data: dict[str, list[dict[str, Any]]] = {}

    def write(self, key: str, records: Iterable[dict[str, Any]], ctx: Context) -> int:
        self.data[key] = list(records)
        return len(self.data[key])


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def notify(self, channel: str, payload: dict[str, Any], ctx: Context) -> None:
        self.sent.append((channel, dict(payload)))


class SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
