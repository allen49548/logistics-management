#!/usr/bin/env python3
"""可插拔来源 / 通道 / init 引导接入的测试。全部在临时目录，不外发、不联网。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import adapters
import config as cfgmod

ROOT = os.path.join(os.path.dirname(__file__), "..", "scripts")
EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "config")


class RosterSourceTest(unittest.TestCase):
    def test_unknown_source_raises(self):
        cfg = cfgmod.load_all("example")
        cfg["payroll"]["roster"]["source"] = "weixin"
        with self.assertRaises(cfgmod.ConfigError):
            adapters.get_roster_source(cfg)

    def test_local_is_alias_for_manual(self):
        cfg = cfgmod.load_all("example")
        # example 用 "local"，应被识别为 manual 且不报错
        src = adapters.get_roster_source(cfg)
        self.assertEqual(src.name, "manual")

    def test_manual_source_does_not_inject_feishu_key(self):
        cfg = cfgmod.load_all("example")
        adapters.apply_roster_source(cfg)
        for d in cfg["roster"]["drivers"]:
            self.assertNotIn("feishu", d)
        self.assertFalse(cfg["_roster_meta"]["feishu_enabled"])

    def test_csv_source_reads_roster_file(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "roster.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("id,name,open_id,notify\nD01,张三,ou_abc,true\nD02,李四,,false\n")
            cfg = cfgmod.load_all("example")
            cfg["roster"]["drivers"] = []  # 用 csv 作为权威名册
            cfg["payroll"]["roster"]["source"] = "csv"
            cfg["payroll"]["roster"]["csv"] = {"path": csv_path}
            adapters.apply_roster_source(cfg)
            ids = {x["id"] for x in cfg["roster"]["drivers"]}
            self.assertEqual(ids, {"D01", "D02"})
            by = {x["id"]: x for x in cfg["roster"]["drivers"]}
            self.assertEqual(by["D01"]["open_id"], "ou_abc")
            self.assertFalse(by["D02"]["notify"])


class ChannelTest(unittest.TestCase):
    def test_unknown_channel_raises(self):
        with self.assertRaises(cfgmod.ConfigError):
            adapters.get_channel("weixin")

    def test_dry_run_never_sends(self):
        ch = adapters.get_channel("dry-run")
        r = ch.send("ou_x", "text", "key", {})
        self.assertEqual(r["status"], "dry-run")

    def test_csv_export_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"paths": {"out_dir": d}}
            ch = adapters.get_channel("csv-export")
            r = ch.send("ou_driver001", "工资条内容", "key-2026-09-D01", cfg)
            self.assertTrue(r["ok"])
            self.assertTrue(os.path.isfile(r["file"]))
            with open(r["file"], encoding="utf-8") as f:
                self.assertIn("工资条内容", f.read())


class InitTest(unittest.TestCase):
    def test_init_produces_draft_config(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "local")
            import init as init_mod
            res = init_mod.run(ROOT, None, None,
                               os.path.join(ROOT, "..", "assets", "工资表模板示例.xlsx"),
                               None, out, "2026")
            self.assertTrue(res["ok"])
            for fn in ("payroll.json", "rules.json", "roster.json",
                      "style.json", "field_map.json", "REVIEW.md"):
                self.assertTrue(os.path.isfile(os.path.join(out, fn)), fn)
            rules = json.load(open(os.path.join(out, "rules.json")))
            # 草稿规则必须全部未确认（设计上绝不自动放行）
            for spec in rules["drivers"].values():
                self.assertFalse(spec["confirmed"])
            review = open(os.path.join(out, "REVIEW.md"), encoding="utf-8").read()
            self.assertIn("你必须确认", review)


if __name__ == "__main__":
    unittest.main(verbosity=2)
