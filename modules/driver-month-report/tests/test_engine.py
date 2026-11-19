"""引擎单元/集成测试：不依赖真实业务文件，用临时构造的工作簿验证规则。

运行：python3 tests/test_engine.py  （或 python3 -m unittest discover -s tests）
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from calculator import compute_driver, compute_month, summarize  # noqa: E402
from common import normalize_label, parse_number  # noqa: E402
from config import Config, ConfigError  # noqa: E402
from discovery import cleanup_stale_outputs, discover_drivers  # noqa: E402
from expr import ExpressionError, evaluate, try_evaluate  # noqa: E402
from extractor import extract_month  # noqa: E402


def _write_driver_sheet(path: Path, sheet: str, rows: list, labels: list) -> None:
    """rows: [(工资, 运费)]；labels: [(标签, 值)]，写在 H/J 列的汇总区。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    headers = ["日期", "业务", "车牌", "名字", "联系方式", "趟次", "路线", "公里数", "工资", "运费"]
    for idx, head in enumerate(headers, start=1):
        ws.cell(row=1, column=idx, value=head)
    for offset, (wage, freight) in enumerate(rows):
        row = 2 + offset
        ws.cell(row=row, column=1, value=46000 + offset)
        ws.cell(row=row, column=8, value=100)
        ws.cell(row=row, column=9, value=wage)
        ws.cell(row=row, column=10, value=freight)

    start = len(rows) + 3
    for offset, (label, value) in enumerate(labels):
        row = start + offset
        ws.cell(row=row, column=8, value=label)
        ws.cell(row=row, column=9, value=value)
    wb.save(path)


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dmr-test-"))
        self.driver_file = self.tmp / "测试司机2026.xlsx"

        _write_driver_sheet(
            self.driver_file,
            "2026.7",
            rows=[(500, 2000), (500, 2000)],
            labels=[("工资支出", 1000), ("总运费", 4000), ("报销支出", 500), ("车贷支出", 1000), ("净利润", 1500)],
        )
        wb = openpyxl.load_workbook(self.driver_file)
        ws = wb.create_sheet("2026.8")
        headers = ["日期", "业务", "车牌", "名字", "联系方式", "趟次", "路线", "公里数", "工资", "运费"]
        for idx, head in enumerate(headers, start=1):
            ws.cell(row=1, column=idx, value=head)
        for offset, (wage, freight) in enumerate([(600, 2500), (600, 2500)]):
            row = 2 + offset
            ws.cell(row=row, column=1, value=46030 + offset)
            ws.cell(row=row, column=9, value=wage)
            ws.cell(row=row, column=10, value=freight)
        # 汇总区：工资合计含奖金；车贷出现两个同义标签（必须去重成一个值）
        labels = [
            ("工资支出", 1500), ("奖金", 300), ("总运费", 5000),
            ("报销支出", 800), ("车贷支出", 1000), ("车贷月供", 1000), ("净利润", 1700),
        ]
        for offset, (label, value) in enumerate(labels):
            row = 6 + offset
            ws.cell(row=row, column=8, value=label)
            ws.cell(row=row, column=9, value=value)
        wb.save(self.driver_file)
        wb.close()

        self.cfg = Config.load(
            overrides={
                "source": {"driver_dir": str(self.tmp), "file_patterns": ["*2026.xlsx"]},
                "output": {"dir": str(self.tmp), "overwrite": {"enabled": True, "mode": "recycle"}},
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 基础件 --
    def test_label_normalization(self) -> None:
        self.assertEqual(normalize_label("工资（元）"), "工资")
        self.assertEqual(normalize_label(" 车贷支出: "), "车贷支出")
        self.assertEqual(normalize_label("总工资支出"), "总工资支出")

    def test_amount_parsing(self) -> None:
        self.assertEqual(parse_number("¥1,234.50", ["¥", ","]), 1234.5)
        self.assertIsNone(parse_number("无"))
        self.assertEqual(parse_number(-300), -300.0)

    def test_expression_engine(self) -> None:
        self.assertEqual(evaluate("income - wage - expense", {"income": 100, "wage": 30, "expense": 20}), 50)
        self.assertEqual(evaluate("round((a-b)/b, 4)", {"a": 12, "b": 10}), 0.2)
        self.assertIsNone(try_evaluate("(a-b)/b", {"a": 1, "b": 0}))
        with self.assertRaises(ExpressionError):
            evaluate("__import__('os')", {})

    def test_period_must_be_adjacent(self) -> None:
        with self.assertRaises(ConfigError):
            Config.load(overrides={"report": {"current_period": "2026-08", "previous_period": "2026-06"}})

    # ------------------------------------------------------------ 取数 ----
    def test_extraction_dedupes_loan_aliases(self) -> None:
        extraction = extract_month(self.driver_file, self.cfg, self.cfg.current_period)
        loan_values = [m.value for m in extraction.matches["expense_loan"]]
        self.assertEqual(loan_values, [1000.0])  # 两个同义标签只保留一个取值，绝不累加

    def test_bonus_not_double_counted(self) -> None:
        current = compute_month(extract_month(self.driver_file, self.cfg, self.cfg.current_period), self.cfg)
        previous = compute_month(extract_month(self.driver_file, self.cfg, self.cfg.previous_period), self.cfg)
        bonus = current.items["bonus"]
        self.assertTrue(bonus.excluded)
        self.assertEqual(current.role_totals["wage"], 1500)
        self.assertEqual(current.role_totals["expense"], 800 + 1000)
        self.assertEqual(current.net_profit, 1700)
        self.assertEqual(current.check_diff, 0)
        self.assertEqual(previous.net_profit, 1500)

    def test_mom_only_previous_month(self) -> None:
        current = compute_month(extract_month(self.driver_file, self.cfg, self.cfg.current_period), self.cfg)
        previous = compute_month(extract_month(self.driver_file, self.cfg, self.cfg.previous_period), self.cfg)
        result = compute_driver("测试司机", self.driver_file, current, previous, self.cfg)
        self.assertAlmostEqual(result.delta, 200)
        self.assertAlmostEqual(result.ratio, 200 / 1500)
        self.assertTrue(result.comparable)
        self.assertEqual(result.trend, "up")

    def test_summary_ratios_use_comparable_base(self) -> None:
        current = compute_month(extract_month(self.driver_file, self.cfg, self.cfg.current_period), self.cfg)
        previous = compute_month(extract_month(self.driver_file, self.cfg, self.cfg.previous_period), self.cfg)
        result = compute_driver("测试司机", self.driver_file, current, previous, self.cfg)
        summary = summarize([result], self.cfg)
        self.assertAlmostEqual(summary["ratio"], summary["delta"] / summary["net_comparable_previous"])

    def test_missing_month_is_flagged_not_invented(self) -> None:
        wb = openpyxl.load_workbook(self.driver_file)
        del wb["2026.7"]
        wb.save(self.driver_file)
        wb.close()
        extraction = extract_month(self.driver_file, self.cfg, self.cfg.previous_period)
        self.assertIsNone(extraction.sheet)
        self.assertTrue(any("2026.7" in note for note in extraction.anomalies))
        previous = compute_month(extraction, self.cfg)
        self.assertIsNone(previous.net_profit)

    # ------------------------------------------------------------ 文件 ----
    def test_discovery_skips_summary_files(self) -> None:
        (self.tmp / "2026年8月汇总.xlsx").write_bytes(b"x")
        drivers, _ = discover_drivers(self.cfg)
        self.assertEqual([d.name for d in drivers], ["测试司机"])

    def test_cleanup_recycles_existing_report(self) -> None:
        stale = self.tmp / "2026年8月汇总.xlsx"
        stale.write_bytes(b"old")
        result = cleanup_stale_outputs(self.cfg)
        self.assertFalse(stale.exists())
        self.assertTrue(result.removed)
        archived = Path(result.removed[0][1])
        self.assertTrue(archived.exists())
        self.assertEqual(archived.read_bytes(), b"old")

    def test_cleanup_can_be_disabled(self) -> None:
        stale = self.tmp / "2026年8月汇总.xlsx"
        stale.write_bytes(b"old")
        cfg = Config.load(
            overrides={
                "source": {"driver_dir": str(self.tmp)},
                "output": {"dir": str(self.tmp), "overwrite": {"enabled": False}},
            }
        )
        result = cleanup_stale_outputs(cfg)
        self.assertEqual(result.removed, [])
        self.assertTrue(stale.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
