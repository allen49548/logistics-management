#!/usr/bin/env python3
"""命令行入口：按配置重新生成「N月汇总」报表。

用法：
    python3 scripts/cli.py generate                 # 用 config/report.json 的默认配置
    python3 scripts/cli.py generate --month 2026-09 --previous 2026-08
    python3 scripts/cli.py generate --dry-run       # 只演算不落盘
    python3 scripts/cli.py inspect                  # 打印识别到的原始数据，便于核对
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calculator import compute_driver, compute_month, summarize  # noqa: E402
from common import is_adjacent, period_text  # noqa: E402
from config import Config, ConfigError  # noqa: E402
from discovery import (  # noqa: E402
    DiscoveryError,
    cleanup_stale_outputs,
    cleanup_summary_text,
    discover_drivers,
)
from extractor import extract_month  # noqa: E402
from renderer import build_workbook  # noqa: E402


def _build_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    if args.month:
        overrides.setdefault("report", {})["current_period"] = args.month
    if args.previous:
        overrides.setdefault("report", {})["previous_period"] = args.previous
    if args.output_dir:
        overrides.setdefault("output", {})["dir"] = args.output_dir
    if args.no_cleanup:
        overrides.setdefault("output", {})["overwrite"] = {"enabled": False}
    return overrides


def _print_header(cfg: Config) -> None:
    print("=" * 78)
    print(f"报表：{cfg.title}    环比对象：{period_text(cfg.previous_period)}（仅此一个月）")
    print(f"源目录：{cfg.driver_dir}")
    print(f"输出：{cfg.output_dir / cfg.output_filename}")
    print("=" * 78)


def _print_cleanup(lines: List[str]) -> None:
    for line in lines:
        print(line)


def _print_result(results, summary, cfg: Config) -> None:
    cur_m, prev_m = cfg.current_period["month"], cfg.previous_period["month"]
    print(f"\n{'司机':<8}{'运费':>12}{'工资':>10}{'其他支出':>11}{f'{cur_m}月净利':>12}{f'{prev_m}月净利':>12}{'环比增减':>12}{'比例':>10}  {'状态':<10}")
    print("-" * 108)
    for item in results:
        cur = item.current
        price = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else "—"  # noqa: E731
        ratio = f"{item.ratio * 100:+.2f}%" if item.ratio is not None else "—"
        delta = f"{item.delta:+,.2f}" if item.delta is not None else "—"
        print(
            f"{item.name:<8}{price(cur.role_totals.get('income')):>12}{price(cur.role_totals.get('wage')):>10}"
            f"{price(cur.role_totals.get('expense')):>11}{price(cur.net_profit):>12}{price(item.previous.net_profit):>12}"
            f"{delta:>12}{ratio:>10}  {item.status:<10}"
        )
    print("-" * 108)
    print(
        f"合计：{cur_m}月净利润 {cfg.money(summary.get('net_current'))} ｜ "
        f"{prev_m}月净利润 {cfg.money(summary.get('net_previous'))} ｜ "
        f"环比 {cfg.money(summary.get('delta'))} ｜ "
        f"比例 {('%+.2f%%' % (summary['ratio'] * 100)) if summary.get('ratio') is not None else '不适用'}"
    )
    print(f"可比司机 {summary.get('comparable')}/{summary.get('drivers')} 人；异常/缺失 {summary.get('issues')} 条\n")


def cmd_generate(args: argparse.Namespace) -> int:
    overrides = _build_overrides(args)
    cfg = Config.load(args.config, args.rules, overrides)
    _print_header(cfg)

    if not is_adjacent(cfg.current_period, cfg.previous_period):
        raise ConfigError("环比期间必须是当前期间的上一个月")

    drivers, notes = discover_drivers(cfg)
    for note in notes:
        print(f"提示：{note}")

    # ① 重生成前先清理同名旧报表，避免重复数据
    cleanup = cleanup_stale_outputs(cfg, dry_run=args.dry_run)
    cleanup_lines = cleanup_summary_text(cleanup).splitlines()
    _print_cleanup(cleanup_lines)

    # ② 逐司机、逐月取数 + 计算
    results = []
    for driver in drivers:
        current = compute_month(extract_month(driver.path, cfg, cfg.current_period), cfg)
        previous = compute_month(extract_month(driver.path, cfg, cfg.previous_period), cfg)
        results.append(compute_driver(driver.name, driver.path, current, previous, cfg))

    summary = summarize(results, cfg)
    _print_result(results, summary, cfg)

    if args.dump_json:
        payload = _as_json(results, summary, cfg)
        dump_path = cfg.output_dir / f"{cfg.title}.json"
        if not args.dry_run:
            dump_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"结构化数据已输出：{dump_path}")

    if args.dry_run:
        print("dry-run：未写入文件。")
        return 0

    # ③ 渲染并保存
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    workbook = build_workbook(results, summary, cfg, cleanup_lines)
    out_path = cfg.output_dir / cfg.output_filename
    workbook.save(out_path)
    print(f"已生成：{out_path}")
    print(f"工作表：{', '.join(workbook.sheetnames)}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config, args.rules, _build_overrides(args))
    _print_header(cfg)
    drivers, notes = discover_drivers(cfg)
    for note in notes:
        print(f"提示：{note}")

    for driver in drivers:
        print(f"\n── {driver.name}  {driver.path.name}")
        for period in (cfg.current_period, cfg.previous_period):
            extraction = extract_month(driver.path, cfg, period)
            label = period_text(period)
            if not extraction.sheet:
                print(f"  [{label}] 无工作表：{'；'.join(extraction.anomalies)}")
                continue
            print(f"  [{label}] sheet={extraction.sheet}")
            for key, matches in extraction.matches.items():
                for match in matches:
                    print(
                        f"      {key:<18}{match.strategy:<7}{match.value:>12,.2f}  "
                        f"{match.cell:<10}{match.label or match.header}"
                        + (f"  // {match.note}" if match.note else "")
                    )
    return 0


def _as_json(results, summary, cfg: Config) -> Dict[str, Any]:
    def month_payload(month):
        return {
            "period": month.period_text,
            "sheet": month.sheet,
            "net_profit": month.net_profit,
            "source_net_profit": month.source_net,
            "check_diff": month.check_diff,
            "role_totals": month.role_totals,
            "items": {
                key: {
                    "name": item.name,
                    "category": item.category,
                    "value": item.value,
                    "strategy": item.strategy,
                    "cell": item.cell,
                    "excluded": item.excluded,
                }
                for key, item in month.items.items()
                if item.value is not None
            },
            "notes": month.notes,
        }

    return {
        "title": cfg.title,
        "current_period": period_text(cfg.current_period),
        "previous_period": period_text(cfg.previous_period),
        "summary": summary,
        "drivers": [
            {
                "driver": r.name,
                "file": r.path.name,
                "status": r.status,
                "delta": r.delta,
                "ratio": r.ratio,
                "comparable": r.comparable,
                "incomparable_reason": r.incomparable_reason,
                "current": month_payload(r.current),
                "previous": month_payload(r.previous),
            }
            for r in results
        ],
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配置化司机月度报表生成器")
    parser.add_argument("command", nargs="?", default="generate", choices=["generate", "inspect"])
    parser.add_argument("--config", type=Path, default=None, help="report.json 路径")
    parser.add_argument("--rules", type=Path, default=None, help="item_rules.json 路径")
    parser.add_argument("--month", default=None, help="当前期间，如 2026-08")
    parser.add_argument("--previous", default=None, help="环比期间，必须是当前期间的上一个月")
    parser.add_argument("--output-dir", default=None, help="输出目录（默认与源目录相同）")
    parser.add_argument("--dry-run", action="store_true", help="只演算不落盘")
    parser.add_argument("--no-cleanup", action="store_true", help="不清理同名旧报表")
    parser.add_argument("--dump-json", action="store_true", help="同时输出结构化 JSON 快照")
    args = parser.parse_args(argv)

    try:
        return cmd_generate(args) if args.command == "generate" else cmd_inspect(args)
    except (ConfigError, DiscoveryError) as exc:
        print(f"配置/源文件错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
