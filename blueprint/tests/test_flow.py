"""End-to-end flow tests using in-memory adapters."""

from __future__ import annotations

import unittest

from blueprint.composition.container import Container
from blueprint.core.model import Context
from blueprint.operations.adapters import InMemoryReader, InMemoryWriter, RecordingNotifier

CTX = Context("test-1", "tester", "2026-11-19T00:00:00+00:00")


def _claim(reference: str, has_proof: bool, amount: float = 100.0) -> dict:
    return {
        "reference": reference,
        "amount": amount,
        "has_proof": has_proof,
        "paid_at": "2026-08-01",
    }


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.reader = InMemoryReader({"references": []})
        self.writer = InMemoryWriter()
        self.notifier = RecordingNotifier()
        self.container = Container(
            reader=self.reader,
            writer=self.writer,
            notifier=self.notifier,
        )

    def test_rules_reject_missing_proof_and_duplicate(self):
        payload = {
            "period": "2026-08",
            "claims": [
                _claim("A1", True),
                _claim("A1", True),
                _claim("B2", False, 50.0),
            ],
        }
        result = self.container.handle("collect_claims", payload, CTX)
        self.assertTrue(result.ok)
        self.assertEqual(result.value.accepted, 1)
        self.assertEqual(result.value.rejected, 2)
        self.assertEqual(result.value.total_amount, 100.0)
        self.assertEqual(len(self.writer.data["accepted"]), 1)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_seen_reference_is_rejected_without_notification(self):
        reader = InMemoryReader({"references": [{"reference": "A1"}]})
        container = Container(
            reader=reader,
            writer=self.writer,
            notifier=self.notifier,
        )
        payload = {"period": "2026-08", "claims": [_claim("A1", True)]}
        result = container.handle("collect_claims", payload, CTX)
        self.assertTrue(result.ok)
        self.assertEqual(result.value.accepted, 0)
        self.assertEqual(result.value.rejected, 1)
        self.assertEqual(self.notifier.sent, [])

    def test_unknown_command_is_technical_error(self):
        result = self.container.handle("missing", {}, CTX)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "technical")
        self.assertEqual(result.error.code, "unknown_command")

    def test_invalid_payload_does_not_leak_business_errors(self):
        result = self.container.handle("collect_claims", {"claims": []}, CTX)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "technical")
        self.assertEqual(result.error.code, "invalid_payload")


if __name__ == "__main__":
    unittest.main()
