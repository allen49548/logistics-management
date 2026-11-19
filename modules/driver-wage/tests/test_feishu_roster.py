#!/usr/bin/env python3
"""飞书名册主源、弹性人数与飞书私发收敛的测试。全程 mock，不联网、不外发。"""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import config as cfgmod  # noqa: E402
import feishu_roster  # noqa: E402

DEPT_JSON = json.dumps({
    "ok": True,
    "data": {"users": [
        {"open_id": "ou_dept_driver_0001", "localized_name": "司机甲",
         "department": "物流部", "is_activated": True, "is_cross_tenant": False},
        {"open_id": "ou_owner_00000001", "localized_name": "示例发起人",
         "department": "物流部", "is_activated": True, "is_cross_tenant": False},
        {"open_id": "ou_external_00001", "localized_name": "外部人员",
         "department": "物流部", "is_activated": True, "is_cross_tenant": True},
    ]},
})
CHAT_RAW = ("[page 1] fetching...\nFound 2 user(s) and 1 bot(s)\n"
            + json.dumps({
                "ok": True,
                "data": {"users": [
                    {"member_id": "ou_chat_driver_0001", "name": "司机乙"},
                    {"member_id": "ou_owner_00000001", "name": "示例发起人"},
                ], "bots": [{"member_id": "ou_bot_0000000001",
                             "name": "示例发起人's Feishu CLI"}]},
            }))


def roster_cfg(**over):
    d = {"source": "feishu", "merge": "union", "feishu": {
        "cli": "lark-cli", "department_query": "物流",
        "department_contains": "物流", "chat_id": "",
        "exclude_names": ["示例发起人"], "exclude_open_ids": ["ou_owner_00000001"],
        "exclude_external_users": True, "page_size": 30, "timeout": 10,
        "max_age_hours": 24}}
    d["feishu"].update(over)
    return d


class ParseTest(unittest.TestCase):
    def test_parses_cli_output_with_progress_lines(self):
        self.assertEqual(len(feishu_roster._users_from(CHAT_RAW)), 2)
        self.assertEqual(len(feishu_roster._users_from(DEPT_JSON)), 3)

    def test_empty_output_is_safe(self):
        self.assertEqual(feishu_roster._users_from("no json here"), [])


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.local = [
            {"id": "司机甲", "name": "司机甲", "open_id": "", "notify": True},
            {"id": "司机丙", "name": "司机丙", "open_id": "", "notify": True},
            {"id": "司机丁", "name": "司机丁", "open_id": "ou_old_0000001",
             "notify": False},
        ]
        self.cache = {"synced_at": "2026-11-14T20:00:00+08:00", "drivers": [
            {"name": "司机甲", "open_id": "ou_dept_driver_0001"},
            {"name": "司机乙", "open_id": "ou_chat_driver_0001"},
        ]}

    def test_feishu_overrides_open_id_and_local_is_kept(self):
        out, meta = feishu_roster.merge_roster(
            self.local, self.cache, {}, roster_cfg())
        by = {d["id"]: d for d in out}
        self.assertEqual(by["司机甲"]["open_id"], "ou_dept_driver_0001")
        self.assertTrue(by["司机甲"]["feishu"])
        self.assertFalse(by["司机丙"]["feishu"])
        self.assertEqual(by["司机丙"]["source"], "local")
        # 飞书新增、本地没有的司机也要纳入（弹性名册）
        self.assertTrue(by["司机乙"]["feishu"])
        self.assertEqual(meta["total_drivers"], 4)
        self.assertEqual(len(meta["feishu_drivers"]), 2)

    def test_explicit_notify_false_is_respected(self):
        out, _ = feishu_roster.merge_roster(
            self.local, {"drivers": [
                {"name": "司机丁", "open_id": "ou_dept_driver_0001"}]},
            {}, roster_cfg())
        by = {d["id"]: d for d in out}
        self.assertFalse(by["司机丁"]["notify"])

    def test_alias_maps_feishu_name_to_local_id(self):
        out, _ = feishu_roster.merge_roster(
            self.local, {"drivers": [
                {"name": "别名甲", "open_id": "ou_alias_000000001"}]},
            {"别名甲": "司机甲"}, roster_cfg())
        by = {d["id"]: d for d in out}
        self.assertTrue(by["司机甲"]["feishu"])
        self.assertNotIn("别名甲", by)

    def test_missing_cache_degrades_safely(self):
        out, meta = feishu_roster.merge_roster(
            self.local, None, {}, roster_cfg())
        self.assertFalse(meta["cache_valid"])
        self.assertTrue(meta["stale"])
        self.assertTrue(all(not d["feishu"] for d in out))

    def test_invalid_open_id_never_enters_roster(self):
        out, meta = feishu_roster.merge_roster(
            self.local, {"drivers": [
                {"name": "司机甲", "open_id": "guessed-id"}]}, {}, roster_cfg())
        self.assertEqual(meta["feishu_drivers"], [])
        self.assertTrue(all(not d["feishu"] for d in out))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="payroll-roster-")
        self.cfg = copy.deepcopy(cfgmod.load_all("example"))
        self.cfg["payroll"]["roster"] = roster_cfg(
            chat_id="oc_example_chat_0001",
            cache_path=os.path.join(self.tmp, "feishu_roster.json"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_excludes_owner_bot_and_external(self):
        def fake_run(cmd, timeout):
            if any("chat-members-list" in str(x) for x in cmd):
                return 0, CHAT_RAW, ""
            return 0, DEPT_JSON, ""
        with mock.patch.object(feishu_roster, "_run", side_effect=fake_run):
            out = feishu_roster.sync(self.cfg)
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(out["names"]), ["司机乙", "司机甲"])
        with open(out["cache"], encoding="utf-8") as f:
            cached = json.load(f)
        ids = [d["open_id"] for d in cached["drivers"]]
        self.assertNotIn("ou_owner_00000001", ids)
        self.assertNotIn("ou_external_00001", ids)
        self.assertNotIn("ou_bot_0000000001", ids)

    def test_sync_failure_never_writes_fake_cache(self):
        with mock.patch.object(feishu_roster, "_run",
                               return_value=(1, "", "unauthorized")):
            out = feishu_roster.sync(self.cfg)
        self.assertFalse(out["ok"])
        self.assertEqual(out["cache"], "")
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "feishu_roster.json")))


class ConfigIntegrationTest(unittest.TestCase):
    def test_example_profile_keeps_legacy_behavior(self):
        cfg = cfgmod.load_all("example")
        self.assertFalse(cfg["_roster_meta"]["feishu_enabled"])
        self.assertTrue(all("feishu" not in d
                            for d in cfg["roster"]["drivers"]))

    def test_feishu_source_merges_cached_drivers(self):
        cfg = copy.deepcopy(cfgmod.load_all("example"))
        cfg["payroll"]["roster"] = roster_cfg()
        cache = {"synced_at": "2026-11-14T20:00:00+08:00", "drivers": [
            {"name": "示例司机A", "open_id": "ou_real_driver_000001"}]}
        with mock.patch.object(feishu_roster, "load_cached",
                               return_value=cache):
            # 重新触发合并（load_all 内部局部导入同一模块）
            cfgmod._apply_roster_source(cfg)
        by = {d["id"]: d for d in cfg["roster"]["drivers"]}
        self.assertTrue(by["D01"]["feishu"])
        self.assertEqual(by["D01"]["open_id"], "ou_real_driver_000001")
        self.assertFalse(by["D02"]["feishu"])
        self.assertTrue(cfg["_roster_meta"]["cache_valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
