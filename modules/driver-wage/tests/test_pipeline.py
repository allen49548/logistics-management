#!/usr/bin/env python3
"""计算 / 核对 / 确认闸门 / 私发安全性的端到端测试。全部在临时目录，不外发。"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import audit  # noqa: E402
import adapters  # noqa: E402
import compute  # noqa: E402
import config as cfgmod  # noqa: E402
import gate  # noqa: E402
import ingest  # noqa: E402
import notify  # noqa: E402
import verify  # noqa: E402

MONTH = "2026-09"


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="payroll-test-")
        self.cfg = copy.deepcopy(cfgmod.load_all("example"))
        self.cfg["paths"]["out_dir"] = os.path.join(self.tmp, "out")
        self.cfg["paths"]["state_dir"] = os.path.join(self.tmp, "state")
        os.makedirs(self.cfg["paths"]["out_dir"], exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _compute(self, cfg=None, write=False):
        return compute.compute(cfg or self.cfg, MONTH, write=write)

    def _verify(self, result, cfg=None, write=False):
        return verify.verify(cfg or self.cfg, MONTH, result, write=write)

    def _persist(self, result, report):
        d = os.path.join(self.cfg["paths"]["out_dir"], MONTH)
        os.makedirs(d, exist_ok=True)
        for name, obj in (("payslips.json", result), ("verification.json", report)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, default=str)

    # ------------------------------------------------------------ 正常路径
    def test_end_to_end_pass(self):
        result = self._compute()
        report = self._verify(result)
        self.assertEqual(report["verdict"], "PASS", report["blocking"])
        self.assertEqual(result["totals"]["trips"], 12)
        self.assertEqual(result["totals"]["gross"], "2090.00")
        self.assertEqual(result["totals"]["net"], "2340.00")
        self.assertEqual(result["errors"], [])

    # ------------------------------------------------------------ 核对能拦错
    def test_verify_blocks_tampered_result(self):
        result = self._compute()
        result["totals"]["net"] = "99999.00"
        report = self._verify(result)
        self.assertEqual(report["verdict"], "BLOCK")
        self.assertTrue(any(c["id"] == "integrity" and c["status"] == "FAIL"
                            for c in report["checks"]))

    def test_verify_blocks_unconfirmed_rule(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["rules"]["drivers"]["D01"]["confirmed"] = False
        result = self._compute(cfg)
        report = self._verify(result, cfg)
        self.assertEqual(report["verdict"], "BLOCK")
        self.assertTrue(any(c["id"] == "rule_compliance" and c["status"] == "FAIL"
                            for c in report["checks"]))

    def test_verify_blocks_missing_rule(self):
        cfg = copy.deepcopy(self.cfg)
        del cfg["rules"]["drivers"]["D01"]["wage"]
        result = self._compute(cfg)
        report = self._verify(result, cfg)
        self.assertEqual(report["verdict"], "BLOCK")
        self.assertTrue(result["errors"])

    def test_verify_blocks_duplicate_trips(self):
        result = self._compute()
        real = ingest.load_trips(self.cfg, MONTH)
        trips = list(real[0])
        trips.append(dict(trips[0]))  # 制造一条完全相同的运输单
        with mock.patch.object(ingest, "load_trips", return_value=(trips, [], [])):
            report = self._verify(result)
        self.assertEqual(report["verdict"], "BLOCK")
        self.assertTrue(any(c["id"] == "completeness" and c["status"] == "FAIL"
                            for c in report["checks"]))

    def test_verify_blocks_unmapped_driver(self):
        result = self._compute()
        real = ingest.load_trips(self.cfg, MONTH)
        trips = list(real[0])
        extra = dict(trips[0])
        extra.update({"driver": "名册外某人", "driver_resolved": False,
                      "route": "名册外路线", "source_row": 999})
        trips.append(extra)
        with mock.patch.object(ingest, "load_trips", return_value=(trips, [], [])):
            report = self._verify(result)
        self.assertEqual(report["verdict"], "BLOCK")

    def test_verify_blocks_balance_mismatch(self):
        result = self._compute()
        result["payslips"][0]["net"] = "1.00"
        report = self._verify(result)
        self.assertEqual(report["verdict"], "BLOCK")
        self.assertTrue(any(c["id"] == "balance" and c["status"] == "FAIL"
                            for c in report["checks"]))

    def test_verify_flags_large_variance(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["payroll"]["checks"]["variance_warn_pct"] = 1
        cfg["payroll"]["checks"]["variance_block_pct"] = 1
        result = self._compute(cfg)
        report = self._verify(result, cfg)
        self.assertEqual(report["verdict"], "BLOCK")
        self.assertTrue(any(c["id"] == "variance" and c["status"] == "FAIL"
                            for c in report["checks"]))

    # ------------------------------------------------------------ 确认闸门
    def _ready(self, verdict="PASS"):
        result = self._compute()
        report = self._verify(result)
        if verdict != report["verdict"]:
            report = copy.deepcopy(report)
            report["verdict"] = verdict
        self._persist(result, report)
        return result, report

    def test_dispatch_blocked_without_confirmation(self):
        self._ready()
        with self.assertRaises(gate.GateError):
            notify.dispatch(self.cfg, MONTH, dry_run=False, channel="stdout")

    def test_confirm_rejected_when_blocked(self):
        self._ready("BLOCK")
        preview = gate.make_preview(self.cfg, MONTH)
        with self.assertRaises(gate.GateError):
            gate.confirm(self.cfg, MONTH, preview["token"], "示例发起人")

    def test_review_requires_explicit_accept(self):
        self._ready("REVIEW")
        preview = gate.make_preview(self.cfg, MONTH)
        owner = self.cfg["payroll"]["gate"]["owner"]
        with self.assertRaises(gate.GateError):
            gate.confirm(self.cfg, MONTH, preview["token"], owner)
        rec = gate.confirm(self.cfg, MONTH, preview["token"], owner,
                           accept_review=True)
        self.assertTrue(rec["accept_review"])

    def test_confirm_invalidated_when_result_changes(self):
        self._ready()
        preview = gate.make_preview(self.cfg, MONTH)
        owner = self.cfg["payroll"]["gate"]["owner"]
        gate.confirm(self.cfg, MONTH, preview["token"], owner)
        # 确认后结果被改动
        p = os.path.join(self.cfg["paths"]["out_dir"], MONTH, "payslips.json")
        with open(p, "r", encoding="utf-8") as f:
            result = json.load(f)
        result["totals"]["net"] = "1.00"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        with self.assertRaises(gate.GateError):
            gate.assert_dispatch_allowed(self.cfg, MONTH)

    def test_wrong_owner_rejected(self):
        self._ready()
        preview = gate.make_preview(self.cfg, MONTH)
        with self.assertRaises(gate.GateError):
            gate.confirm(self.cfg, MONTH, preview["token"], "别人")

    # ------------------------------------------------------------ 私发安全
    def test_dry_run_never_calls_feishu(self):
        self._ready()
        preview = gate.make_preview(self.cfg, MONTH)
        gate.confirm(self.cfg, MONTH, preview["token"], "示例发起人")
        with mock.patch.object(adapters, "_send_feishu_dm") as sender:
            report = notify.dispatch(self.cfg, MONTH, dry_run=True, channel="feishu")
            sender.assert_not_called()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["failed"], 0)
        # open_id 缺失的两人必须被明确列出，绝不猜
        self.assertEqual(len(report["missing_open_id"]), 2)

    def test_dispatch_is_idempotent(self):
        self._ready()
        preview = gate.make_preview(self.cfg, MONTH)
        gate.confirm(self.cfg, MONTH, preview["token"], "示例发起人")
        first = notify.dispatch(self.cfg, MONTH, dry_run=False, channel="stdout")
        audit.mark_sent(self.cfg, MONTH, "D01", "sent", "", "stdout")
        second = notify.dispatch(self.cfg, MONTH, dry_run=False, channel="stdout")
        self.assertLess(second["sent"], first["sent"] + 1)
        self.assertTrue(any(s["reason"].startswith("本月已成功发送")
                            for s in second["skipped"]))


    # --------------------------------------------- 弹性人数 / 飞书主源收敛
    def test_compute_only_active_drivers_no_zero_rows(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["roster"]["drivers"].append(
            {"id": "D99", "name": "本月无单司机", "open_id": "", "notify": True})
        result = self._compute(cfg)
        self.assertEqual(result["totals"]["drivers"], 4)
        self.assertNotIn("D99", [p["driver"] for p in result["payslips"]])
        report = self._verify(result, cfg)
        self.assertEqual(report["verdict"], "PASS", report["blocking"])

    def test_dispatch_only_feishu_roster_drivers(self):
        self.cfg["_roster_meta"]["feishu_enabled"] = True
        marks = {"D01": True, "D02": False, "D03": True, "D04": False}
        for d in self.cfg["roster"]["drivers"]:
            d["feishu"] = marks[d["id"]]
        self._ready()
        preview = gate.make_preview(self.cfg, MONTH)
        gate.confirm(self.cfg, MONTH, preview["token"], "示例发起人")
        with mock.patch.object(adapters, "_send_feishu_dm") as sender:
            report = notify.dispatch(self.cfg, MONTH, dry_run=True,
                                     channel="feishu")
            sender.assert_not_called()
        self.assertTrue(report["feishu_only_dispatch"])
        self.assertEqual([r["driver"] for r in report["results"]], ["D01"])
        # 本地名册司机只作信息记录，不进 missing_open_id、不催办
        self.assertEqual(len(report["missing_open_id"]), 1)
        self.assertEqual(sum("不在飞书名册" in s["reason"]
                             for s in report["skipped"]), 2)


    def test_feishu_dm_falls_back_to_user_identity(self):
        bad = mock.Mock(returncode=1, stdout="", stderr="bot not in chat")
        good = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(adapters.subprocess, "run",
                               side_effect=[bad, good]) as run:
            out = adapters._send_feishu_dm("ou_example_test", "text", "key",
                                           self.cfg)
        self.assertTrue(out["ok"])
        self.assertEqual(out["identity"], "user")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][4], "bot")
        self.assertEqual(run.call_args_list[1].args[0][4], "user")
        # 幂等键在两次尝试里必须一致，防止换身份重发第二遍
        k1 = run.call_args_list[0].args[0][run.call_args_list[0].args[0]
                                          .index("--idempotency-key") + 1]
        k2 = run.call_args_list[1].args[0][run.call_args_list[1].args[0]
                                          .index("--idempotency-key") + 1]
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
