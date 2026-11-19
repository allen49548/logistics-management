"""Entry drivers turn external protocols into neutral calls to an injected handler."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from typing import Any, Sequence

from blueprint.core.model import Context, Result
from blueprint.core.ports import Clock, Handler


class CliDriver:
    def __init__(self, handler: Handler, clock: Clock) -> None:
        self._handler = handler
        self._clock = clock

    def run(self, argv: Sequence[str]) -> int:
        if not argv:
            sys.stdout.write(json.dumps({"ok": False, "error": "empty_input"}, ensure_ascii=False))
            return 2
        envelope = json.loads(argv[0])
        ctx = Context(
            request_id=str(envelope.get("request_id", "cli")),
            actor="cli",
            occurred_at=self._clock.now(),
        )
        result: Result[Any] = self._handler.handle(envelope["command"], envelope["payload"], ctx)
        sys.stdout.write(json.dumps(self._render(result), ensure_ascii=False))
        return 0 if result.ok else 1

    @staticmethod
    def _render(result: Result[Any]) -> dict[str, Any]:
        if result.ok:
            value = result.value
            return {"ok": True, "data": asdict(value) if is_dataclass(value) else value}
        err = result.error
        payload = {"code": err.code, "message": err.message, "kind": err.kind} if err else None
        return {"ok": False, "error": payload}
