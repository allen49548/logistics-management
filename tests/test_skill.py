"""物流管理系统单测（离线、脱敏）：路由注册、配置化字段识别、去重、分组、数据源生成。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from common import make_key, match_alias, normalize_driver  # noqa: E402
from intake import (  # noqa: E402
    _column_map,
    build_data_source,
    dedupe,
    group_by_driver,
    parse_message_text,
    parse_messages,
    resolve_drivers,
)

FIELDS = {
    "date": ["日期", "出车日期", "date"],
    "driver": ["司机", "姓名", "driver"],
    "route": ["路线", "运输地点", "route"],
    "km": ["公里数", "公里", "km"],
    "wage": ["工资", "运费", "wage"],
}


class TestRouting(unittest.TestCase):
    def test_all_modules_registered(self):
        data = json.loads((ROOT / "config" / "modules.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data["modules"]), {"reimbursement", "wage", "summary", "monthly_sheet"})
        for key, m in data["modules"].items():
            root_value = m["skill_root"]
            self.assertTrue(root_value)
            self.assertTrue(root_value.startswith("modules/"), (key, root_value))
            self.assertNotIn("~/.workbuddy/skills/", root_value)
            self.assertNotIn("/Users/", root_value)
            module_root = ROOT / root_value
            self.assertTrue(module_root.is_dir(), module_root)
            self.assertTrue(m["delegates"])
            for a in m["delegates"].values():
                self.assertTrue(a["script"])
                self.assertTrue((module_root / a["script"]).exists(), (key, a["script"]))

    def test_cli_help_and_doctor(self):
        for argv in (["--help"], ["list"], ["doctor"]):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "logistics.py"), *argv],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if argv != ["--help"]:
                self.assertTrue(json.loads(proc.stdout)["ok"])


class TestFieldDetection(unittest.TestCase):
    def test_alias_longest_match(self):
        self.assertEqual(match_alias("出车日期", FIELDS), "date")
        self.assertEqual(match_alias("司机姓名", FIELDS), "driver")
        self.assertIsNone(match_alias("无关列", FIELDS))

    def test_column_map_from_config(self):
        icfg = {"fields": FIELDS, "data_source": {"columns": ["日期", "司机", "路线", "公里数", "工资"]}}
        self.assertEqual(_column_map(icfg), {
            "date": "日期", "driver": "司机", "route": "路线", "km": "公里数", "wage": "工资"
        })

    def test_parse_key_value_text(self):
        rec = parse_message_text("日期:2026-08-01 司机:张三 路线:A-B 公里数:100 工资:250", {"fields": FIELDS})
        self.assertEqual(rec["driver"], "张三")
        self.assertEqual(rec["km"], "100")

    def test_parse_messages_marks_image_without_guessing(self):
        msgs = [{"message_id": "m1", "type": "image", "sender": "张三", "text": "[图片]"}]
        out = parse_messages(msgs, {"accepted_sources": ["image_transcription"], "fields": FIELDS})
        self.assertEqual(out[0]["_needs_transcription"], "agent_vision")
        self.assertNotIn("km", out[0])


class TestDedupGroup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = str(Path(self.tmp.name) / "seen.jsonl")
        self.icfg = {
            "fields": FIELDS,
            "dedup": {"key_fields": ["message_id", "driver", "date", "route", "km"], "store": self.store},
            "data_source": {"columns": ["日期", "司机", "路线", "公里数", "工资"], "sheet_name": "运输单",
                            "path": str(Path(self.tmp.name) / "{year}年{month}月运输单.xlsx")},
            "driver_from": "field_only",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedupe_in_batch_and_history(self):
        recs = [
            {"message_id": "m1", "driver": "甲", "date": "2026-08-01", "route": "A-B", "km": 100},
            {"message_id": "m2", "driver": "乙", "date": "2026-08-01", "route": "A-B", "km": 100},
        ]
        unique, dup = dedupe(recs, self.icfg)
        self.assertEqual(len(unique), 2)
        self.assertFalse(dup)
        unique2, dup2 = dedupe(recs, self.icfg)
        self.assertEqual(len(unique2), 0)
        self.assertEqual(len(dup2), 2)

    def test_driver_alias_normalization(self):
        recs = [{"message_id": "m1", "driver": "幸荣超", "date": "2026-08-01"}]
        resolved, unknown = resolve_drivers(recs, {"drivers": {"aliases": {"幸荣超": "辛荣超"}}}, self.icfg)
        self.assertEqual(resolved[0]["driver"], "辛荣超")
        self.assertFalse(unknown)

    def test_group_and_build_source(self):
        recs = [
            {"message_id": "m1", "driver": "甲", "date": "2026-08-01", "route": "A-B", "km": 100, "wage": 250},
            {"message_id": "m2", "driver": "甲", "date": "2026-08-02", "route": "B-C", "km": 80, "wage": 200},
            {"message_id": "m3", "driver": "乙", "date": "2026-08-01", "route": "A-B", "km": 60, "wage": 150},
        ]
        grouped = group_by_driver(recs)
        self.assertEqual(set(grouped), {"甲", "乙"})
        self.assertEqual(len(grouped["甲"]), 2)
        out = build_data_source(recs, self.icfg, 2026, 8)
        self.assertTrue(out.exists())
        from openpyxl import load_workbook
        wb = load_workbook(out, data_only=True)
        self.assertEqual([c.value for c in wb.active[1]], ["日期", "司机", "路线", "公里数", "工资"])
        self.assertEqual(wb.active.max_row, 4)
        wb.close()

    def test_make_key_stable(self):
        a = {"driver": "甲", "date": "2026-08-01"}
        b = {"date": "2026-08-01", "driver": "甲"}
        self.assertEqual(make_key(a, ["driver", "date"]), make_key(b, ["driver", "date"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
