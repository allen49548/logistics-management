"""Pure scenario rules. No adapters, no SDK, no filesystem, no network."""

from __future__ import annotations

from blueprint.core.model import Context
from blueprint.core.policy import Decision

from .contracts import Claim

RULE_PROOF_REQUIRED = "proof_required"
RULE_DUPLICATE_GUARD = "duplicate_guard"


class ProofRequired:
    name = RULE_PROOF_REQUIRED

    def evaluate(self, candidate: Claim, ctx: Context) -> Decision[Claim]:
        if not candidate.has_proof:
            return Decision(False, reason="missing_proof")
        return Decision(True, value=candidate)


class DuplicateGuard:
    name = RULE_DUPLICATE_GUARD

    def __init__(self, seen_references: set[str]) -> None:
        self._seen = seen_references

    def evaluate(self, candidate: Claim, ctx: Context) -> Decision[Claim]:
        if candidate.reference in self._seen:
            return Decision(False, reason="duplicate_reference")
        return Decision(True, value=candidate)
