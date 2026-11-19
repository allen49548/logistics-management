"""Assembly root: binds adapters to use cases and exposes a neutral handler."""

from __future__ import annotations

from typing import Any

from blueprint.business.contracts import Claim, IntakeRequest
from blueprint.business.workflows import CollectClaims
from blueprint.core.model import Context, Error, Result
from blueprint.core.ports import Handler
from blueprint.core.registry import Registry
from blueprint.core.usecase import Runner, UseCase
from blueprint.operations.adapters import InMemoryReader, InMemoryWriter, RecordingNotifier


class Container(Handler):
    def __init__(
        self,
        reader: InMemoryReader | None = None,
        writer: InMemoryWriter | None = None,
        notifier: RecordingNotifier | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.reader = reader or InMemoryReader()
        self.writer = writer or InMemoryWriter()
        self.notifier = notifier or RecordingNotifier()
        self.runner = runner or Runner()
        self._cases: Registry[UseCase[Any, Any]] = Registry()
        self._cases.register(
            CollectClaims.name,
            CollectClaims(self.reader, self.writer, self.notifier),
        )

    def handle(self, command: str, payload: dict[str, Any], ctx: Context) -> Result[Any]:
        try:
            case = self._cases.get(command)
        except KeyError as exc:
            return Result.failure(Error("unknown_command", str(exc), "technical"))
        try:
            request = self._to_request(command, payload)
        except (KeyError, TypeError, ValueError) as exc:
            return Result.failure(Error("invalid_payload", str(exc), "technical"))
        return self.runner.run(case, request, ctx)

    @staticmethod
    def _to_request(command: str, payload: dict[str, Any]) -> Any:
        if command == CollectClaims.name:
            claims = tuple(Claim(**item) for item in payload["claims"])
            return IntakeRequest(claims=claims, period=str(payload["period"]))
        raise KeyError(command)
