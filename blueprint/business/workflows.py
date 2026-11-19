"""Scenario workflow: composes rules and core ports into one use case."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from blueprint.core.model import Context, Result
from blueprint.core.ports import Notifier, Reader, Writer
from blueprint.core.usecase import UseCase

from .contracts import IntakeReport, IntakeRequest
from .rules import DuplicateGuard, ProofRequired


class CollectClaims(UseCase[IntakeRequest, IntakeReport]):
    name = "collect_claims"

    def __init__(self, reader: Reader, writer: Writer, notifier: Notifier) -> None:
        self._reader = reader
        self._writer = writer
        self._notifier = notifier

    def execute(self, request: IntakeRequest, ctx: Context) -> Result[IntakeReport]:
        seen = {str(row.get("reference", "")) for row in self._reader.read("references", ctx)}
        accepted: list[dict[str, Any]] = []
        rejected = 0
        total = 0.0

        for claim in request.claims:
            proof = ProofRequired().evaluate(claim, ctx)
            decision = DuplicateGuard(seen).evaluate(claim, ctx) if proof.allowed else proof
            if not decision.allowed or decision.value is None:
                rejected += 1
                continue
            item = decision.value
            seen.add(item.reference)
            accepted.append(asdict(item))
            total += item.amount

        self._writer.write("accepted", accepted, ctx)
        if accepted:
            # The rule about whether a side effect is warranted belongs here.
            # The adapter only executes the call.
            self._notifier.notify(
                "operations",
                {"accepted": len(accepted), "period": request.period},
                ctx,
            )

        return Result.success(IntakeReport(len(accepted), rejected, total))
