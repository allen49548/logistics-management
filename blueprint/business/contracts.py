"""Scenario DTOs. Business vocabulary is intentionally confined to this package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Claim:
    reference: str
    amount: float
    has_proof: bool
    paid_at: str


@dataclass(frozen=True)
class IntakeRequest:
    claims: tuple[Claim, ...]
    period: str


@dataclass(frozen=True)
class IntakeReport:
    accepted: int
    rejected: int
    total_amount: float
