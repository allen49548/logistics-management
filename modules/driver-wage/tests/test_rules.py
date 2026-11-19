#!/usr/bin/env python3
"""规则 DSL 与受限求值器单元测试（零第三方依赖）。"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import rules  # noqa: E402

D = Decimal


class TestTranslate(unittest.TestCase):
    def test_if_tier_with_cell_map(self):
        expr = "=IF(G2<=60,130,IF(G2<=150,150,IF(G2<=250,180,200)))"
        self.assertEqual(rules.safe_eval(expr, {"km": D(40)}, {}, {"G2": "km"}), D(130))
        self.assertEqual(rules.safe_eval(expr, {"km": D(140)}, {}, {"G2": "km"}), D(150))
        self.assertEqual(rules.safe_eval(expr, {"km": D(300)}, {}, {"G2": "km"}), D(200))

    def test_rounddown_formula(self):
        expr = ("=ROUNDDOWN(IF(H2<=50,300,IF(H2<=110,510,510+(H2-110)*2))*0.95,0)")
        self.assertEqual(rules.safe_eval(expr, {"km": 40}, {}, {"H2": "km"}), D(285))
        self.assertEqual(rules.safe_eval(expr, {"km": 120}, {}, {"H2": "km"}),
                         D(int((510 + 10 * 2) * 0.95)))

    def test_unmapped_cell_rejected(self):
        with self.assertRaises(rules.RuleError):
            rules.safe_eval("=IF(G2<=60,130,150)", {"km": 40}, {})

    def test_range_reference_rejected(self):
        with self.assertRaises(rules.RuleError):
            rules.safe_eval("=SUM(G2:G12)", {"km": 40}, {})

    def test_code_injection_rejected(self):
        for evil in ("__import__('os').system('echo hi')",
                     "().__class__.__bases__[0]", "(lambda: 1)()",
                     "[x for x in (1,2)]", "2**999999"):
            with self.assertRaises(rules.RuleError, msg=evil):
                rules.safe_eval(evil, {"km": 40}, {})

    def test_if_is_lazy(self):
        # 未命中分支里的除零不应炸掉求值
        self.assertEqual(rules.safe_eval("IF(km<=60,130,1/0)", {"km": 40}, {}), D(130))
        with self.assertRaises(rules.RuleError):
            rules.safe_eval("IF(km<=60,1/0,130)", {"km": 40}, {})

    def test_unknown_variable_rejected(self):
        with self.assertRaises(rules.RuleError):
            rules.safe_eval("missing*2", {"km": 40}, {})


class TestStructuredRules(unittest.TestCase):
    def setUp(self):
        self.trip = {"km": "120", "route": "路线B", "trip_no": "1",
                     "driver_resolved": True}

    def test_tier(self):
        rule = {"op": "tier", "field": "km",
                "brackets": [{"lte": 60, "value": 130}, {"lte": 150, "value": 150}],
                "default": 200}
        self.assertEqual(rules.evaluate(rule, {"km": "50"}), D(130))
        self.assertEqual(rules.evaluate(rule, self.trip), D(150))
        self.assertEqual(rules.evaluate(rule, {"km": "999"}), D(200))

    def test_lookup(self):
        rule = {"op": "lookup", "key": "route", "table": "t"}
        self.assertEqual(rules.evaluate(rule, self.trip, {"t": {"路线B": 200}}), D(200))

    def test_const_and_sum(self):
        self.assertEqual(rules.evaluate({"op": "const", "value": 150}, self.trip), D(150))
        self.assertEqual(rules.evaluate({"op": "sum", "items": [
            {"op": "const", "value": 100}, {"op": "const", "value": 50}]}, self.trip), D(150))

    def test_validate_rules_flags_unconfirmed(self):
        doc = {"drivers": {"X": {"wage": {"op": "const", "value": 1},
                                 "confirmed": False}}}
        self.assertTrue(any("未经发起人确认" in p for p in rules.validate_rules(doc)))
        doc["drivers"]["X"]["confirmed"] = True
        self.assertEqual(rules.validate_rules(doc), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
