#!/usr/bin/env python3
"""【能力 1 / 共 2】计算工资。

输入：运输单明细 + 报销数据 + 已确认的计薪规则 + 名册 + 统一模板配置
输出：out/<month>/payslips.json（逐行明细 + 每人汇总 + 全员合计 + 输入指纹）

铁律：
  * 只读输入，绝不改写用户桌面上的历史工资表或报销台账。
  * 规则缺失 / 求值失败 / 司机未映射 -> 绝不猜、绝不填 0，记录 error 交给核对阻断。
  * 金额全程 Decimal，逐行按精度取整后再累加，与 Excel SUM 口径一致。
  * 「有几个司机计算几个」：只按当月**实际有运输单（或报销）**的司机出工资条，
    绝不铺全员名册的零值行、不凑人数、不因谁没到齐记异常。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

import config as cfgmod
import ingest
import rules
from rules import D, RuleError, dec, quantize


SCHEMA = 1


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _money(v: Decimal, p: int) -> str:
    return str(quantize(v, p))


def _rule_for(driver_id: str, cfg: dict):
    return cfg["rules"]["drivers"].get(driver_id)


def compute(cfg: dict, month: str, write: bool = True) -> dict:
    precision = int(cfg["payroll"].get("precision", 2))
    checks = cfg["payroll"].get("checks", {})
    tables = cfg["rules"].get("tables", {})

    trips, warnings, used_paths = ingest.load_trips(cfg, month)
    reimb, reimb_warnings, reimb_paths = ingest.load_reimbursements(cfg, month)
    warnings += reimb_warnings
    used_paths += reimb_paths

    rule_problems = rules.validate_rules(cfg["rules"])
    for p in rule_problems:
        warnings.append("规则: " + p)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for t in trips:
        grouped[t["driver"]].append(t)

    # 名册是弹性的：飞书优先 + 本地补充只决定"司机归属与能否私发"，
    # 不决定计算人数。有几个司机有单就算几个，绝不铺 0 单行凑人数。
    # 报销表里有、运输单里没有的司机仍要暴露，避免漏算报销。
    for did in reimb:
        grouped.setdefault(did, [])

    error_agg: dict[tuple[str, str], int] = defaultdict(int)
    payslips = []
    for driver_id in sorted(grouped):
        rows = grouped[driver_id]
        spec = _rule_for(driver_id, cfg)
        names = {d["id"]: d.get("name", d["id"]) for d in cfg["roster"]["drivers"]}
        display = names.get(driver_id, driver_id)
        lines, gross = [], Decimal(0)
        for t in sorted(rows, key=lambda x: (x["date"], x["source_row"])):
            if spec is None:
                lines.append({"date": t["date"], "route": t["route"],
                              "km": t["km"], "wage": None,
                              "error": "no_rule", "source": t["source"],
                              "source_row": t["source_row"]})
                error_agg[(display, "无计薪规则")] += 1
                continue
            if "wage" not in spec:
                # 数组公式/手填值等无法可靠反推的情况：如实标注需人工补录，
                # 绝不编造算法、绝不把 KeyError 当成"求值失败"糊弄过去。
                lines.append({"date": t["date"], "route": t["route"],
                              "km": t["km"], "wage": None,
                              "error": "needs_manual", "source": t["source"],
                              "source_row": t["source_row"]})
                error_agg[(display, "规则需人工补录（无 wage）")] += 1
                continue
            try:
                wage = quantize(rules.evaluate(spec["wage"], t, tables, precision), precision)
                lines.append({
                    "date": t["date"], "route": t["route"], "km": t["km"],
                    "trip_no": t["trip_no"], "plate": t["plate"],
                    "wage": _money(wage, precision),
                    "rule_op": spec["wage"].get("op"),
                    "source": t["source"], "source_row": t["source_row"],
                })
                gross += wage
            except (RuleError, KeyError, TypeError) as exc:
                lines.append({"date": t["date"], "route": t["route"],
                              "km": t["km"], "wage": None, "error": str(exc),
                              "source": t["source"], "source_row": t["source_row"]})
                error_agg[(display, f"规则求值失败 - {exc}")] += 1

        reimbursement = reimb.get(driver_id, Decimal(0))
        deductions = []
        if spec:
            for adj in spec.get("adjustments", []):
                if adj.get("source") == "reimbursement":
                    continue  # 报销统一由 reimbursement 数据源提供，避免双记
                amount = dec(adj.get("amount", 0))
                if adj.get("op", "subtract") == "add":
                    gross += amount
                else:
                    deductions.append({"label": adj.get("label", "扣款"),
                                       "amount": _money(amount, precision)})
        deduction_total = sum((dec(x["amount"]) for x in deductions), Decimal(0))
        net = gross + reimbursement - deduction_total
        payslips.append({
            "driver": driver_id, "display_name": display, "month": month,
            "lines": lines, "trip_count": len(rows),
            "km_total": str(sum((dec(t["km"]) for t in rows), Decimal(0))),
            "gross": _money(gross, precision),
            "reimbursement": _money(reimbursement, precision),
            "deductions": deductions,
            "deduction_total": _money(deduction_total, precision),
            "net": _money(net, precision),
            "rule_confirmed": bool(spec and spec.get("confirmed")),
        })

    totals = {
        "drivers": len(payslips),
        "trips": len(trips),
        "km": str(sum((dec(t["km"]) for t in trips), Decimal(0))),
        "gross": _money(sum((dec(p["gross"]) for p in payslips), Decimal(0)), precision),
        "reimbursement": _money(sum((dec(p["reimbursement"]) for p in payslips), Decimal(0)), precision),
        "deduction_total": _money(sum((dec(p["deduction_total"]) for p in payslips), Decimal(0)), precision),
        "net": _money(sum((dec(p["net"]) for p in payslips), Decimal(0)), precision),
    }

    errors = [f"{name}: {reason}" + (f"（{n} 行，逐行明细见 lines）" if n > 1 else "")
              for (name, reason), n in sorted(error_agg.items())]

    result = {
        "schema": SCHEMA, "month": month, "generated_at": _now(),
        "precision": precision,
        "input_hash": cfgmod.input_fingerprint(cfg, used_paths),
        "sources": [os.path.basename(p) for p in sorted(set(used_paths))],
        "payslips": payslips, "totals": totals,
        "scope": {
            "roster_source": cfg["payroll"].get("roster", {}).get("source", "local"),
            "roster_total": len(cfg["roster"].get("drivers", [])),
            "feishu_drivers": len(cfg.get("_roster_meta", {})
                                  .get("feishu_drivers", [])),
            "computed_drivers": len(payslips),
            "note": "只计算当月实际有运输单/报销的司机；名册不铺零值行、不凑人数。",
        },
        "warnings": warnings, "errors": errors,
        "check_thresholds": checks,
        "history_month": _prev_month(month),
    }
    result["result_hash"] = cfgmod.canonical_hash(
        {k: result[k] for k in ("month", "payslips", "totals", "input_hash")})

    if write:
        out_dir = os.path.join(cfg["paths"]["out_dir"], month)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "payslips.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
    return result


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def load_result(cfg: dict, month: str) -> dict:
    path = os.path.join(cfg["paths"]["out_dir"], month, "payslips.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"未找到计算结果，请先 compute: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
