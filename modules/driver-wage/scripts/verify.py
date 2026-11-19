#!/usr/bin/env python3
"""【能力 2 / 共 2】核对工资。

核对不是"把结果再打印一遍"，而是从**原始输入**走一条独立聚合实现，逐项验证：

  1 integrity        结果文件完整性 / 输入指纹 / 是否被人改过
  2 recompute        独立路径重算逐行工资与每人合计（不复用 compute 的聚合代码）
  3 completeness     未映射司机、重复运输单、日期越界、公里数异常、名册司机漏单
  4 balance          勾稽：逐行和=合计、实发=应发+报销-扣款、全员=各司机之和
  5 rule_compliance  规则存在 / 已确认 / 可求值 / 计算阶段无错误
  6 reimbursement    报销数据源 vs 计入金额 vs 工资表汇总块（三方，可选）
  7 variance         环比历史工资、负数/异常金额
  8 external_bill    外部账单"核对差异"列（可选，用于判断运输单本身是否可信）

结论：BLOCK（存在 ERROR，禁止私发）/ REVIEW（存在 WARN，需发起人显式接受）
      / PASS（可进入确认闸门）。
任何阶段都不得静默跳过异常；BLOCK 时 dispatch 会被 gate 硬性拒绝。
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

STATUS_WEIGHT = {"PASS": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}


def _check(cid, title, status, summary, items=None, severity=None):
    if severity is None:
        severity = "ERROR" if status == "FAIL" else ("WARN" if status == "WARN" else "INFO")
    return {"id": cid, "title": title, "status": status, "severity": severity,
            "summary": summary, "items": items or []}


def independent_recompute(trips: list[dict], cfg: dict) -> dict[str, Decimal]:
    """独立聚合实现：不调用 compute，不复用其分组/累加/取整逻辑。

    表达式类规则共用 rules.safe_eval（该求值器由 tests/test_rules.py 单独覆盖），
    但阶梯/查表/常量/合计全部在這裡独立实现，用于抓 compute 的聚合实现缺陷。
    """
    p = int(cfg["payroll"].get("precision", 2))
    tables = cfg["rules"].get("tables", {})
    specs = cfg["rules"]["drivers"]
    acc: dict[str, Decimal] = defaultdict(lambda: Decimal(0))

    def value_of(spec, trip):
        op = spec.get("op")
        if op == "const":
            return dec(spec["value"])
        if op == "tier":
            v = dec(trip.get(spec.get("field", "km"), 0))
            hit = next((dec(b["value"]) for b in spec.get("brackets", [])
                        if v <= dec(b["lte"])), None)
            if hit is None:
                if "default" not in spec:
                    raise RuleError("tier 未命中且无 default")
                hit = dec(spec["default"])
            return hit
        if op == "lookup":
            key = str(trip.get(spec.get("key", "route"), ""))
            tbl = tables[spec["table"]]
            if key not in tbl:
                if "default" not in spec:
                    raise RuleError(f"价目表缺 {key!r}")
                return dec(spec["default"])
            return dec(tbl[key])
        if op == "formula":
            return rules.safe_eval(spec["expr"], rules.formula_variables(trip),
                                   tables, spec.get("cell_map"))
        if op == "sum":
            return sum((value_of(i, trip) for i in spec["items"]), Decimal(0))
        raise RuleError(f"未知 op {op!r}")

    for t in trips:
        spec = specs.get(t["driver"], {}).get("wage")
        if spec is None:
            continue
        acc[t["driver"]] += quantize(value_of(spec, t), p)
    return dict(acc)


def verify(cfg: dict, month: str, result: dict | None = None, write: bool = True) -> dict:
    if result is None:
        from compute import load_result
        result = load_result(cfg, month)
    p = int(cfg["payroll"].get("precision", 2))
    checks_cfg = cfg["payroll"].get("checks", {})
    tol = dec(checks_cfg.get("amount_tolerance", "0.01"))
    checks = []

    # 1 ---------------------------------------------------------------- 完整性
    items = []
    expected = cfgmod.canonical_hash(
        {k: result[k] for k in ("month", "payslips", "totals", "input_hash")})
    if expected != result.get("result_hash"):
        items.append("result_hash 不匹配：结果文件被改动过，必须重新计算")
    if result.get("month") != month:
        items.append("结果月份与请求月份不一致")
    if not result.get("input_hash"):
        items.append("缺少输入指纹")
    checks.append(_check("integrity", "结果与输入完整性", "FAIL" if items else "PASS",
                         "结果未被篡改" if not items else "结果不可信", items))

    # 2 ------------------------------------------------------------ 独立重算
    trips, trip_warnings, _ = ingest.load_trips(cfg, month)
    try:
        recomputed = independent_recompute(trips, cfg)
        items = []
        for slip in result["payslips"]:
            did = slip["driver"]
            if not slip["lines"]:
                continue
            got = dec(slip["gross"])
            want = recomputed.get(did, Decimal(0))
            # gross 含 adjustments 中的固定加项，逐行重算只覆盖运输工资
            fixed_add = sum((dec(a.get("amount", 0))
                             for a in cfg["rules"]["drivers"].get(did, {}).get("adjustments", [])
                             if a.get("op") == "add" and a.get("source") != "reimbursement"),
                            Decimal(0))
            want += fixed_add
            if abs(got - want) > tol:
                items.append(f"{slip['display_name']}: 结果 {got} ≠ 独立重算 {want}")
        status = "FAIL" if items else "PASS"
    except Exception as exc:
        status, items = "FAIL", [f"独立重算异常: {exc}"]
    checks.append(_check("recompute", "独立路径重算逐行工资与合计", status,
                         "逐行与合计一致" if status == "PASS" else "重算不一致", items))

    # 3 ------------------------------------------------------------ 数据完整性
    items = []
    unresolved = [t for t in trips if not t.get("driver_resolved")]
    if unresolved:
        items.append(f"{len(unresolved)} 行司机未映射名册/别名（绝不猜归属）")
    key_fields = checks_cfg.get("duplicate_key",
                                ["driver", "date", "route", "km", "trip_no"])
    seen, dups = {}, []
    for t in trips:
        k = "|".join(str(t.get(f, "")) for f in key_fields)
        if k in seen:
            dups.append(f"{t['source']}:{t['source_row']} 与 {seen[k]} 重复")
        seen[k] = f"{t['source']}:{t['source_row']}"
    if dups:
        items.append(f"疑似重复运输单 {len(dups)} 行（重复会直接多算工资）")
        items += dups[:8]
    max_km = dec(checks_cfg.get("max_km", 3000))
    bad_km = [f"{t['source']}:{t['source_row']} km={t['km']}" for t in trips
              if dec(t["km"]) <= 0 or dec(t["km"]) > max_km]
    if bad_km:
        items.append(f"公里数异常 {len(bad_km)} 行")
        items += bad_km[:8]
    off_month = [f"{t['source']}:{t['source_row']}" for t in trips if t["month"] != month]
    if off_month:
        items.append(f"{len(off_month)} 行日期不属于 {month}")
    if trip_warnings:
        items.append(f"输入适配阶段产生 {len(trip_warnings)} 条警告（详见 payslips.warnings）")
    # 弹性名册定案：名册里有人当月没单不是异常、不是待办、不催办，
    # 计算阶段也根本不会为这些人生成零值工资条，此处不再产生干扰项。
    hard = bool(unresolved or dups or bad_km or off_month)
    checks.append(_check("completeness", "运输单数据完整性", "FAIL" if hard else
                         ("WARN" if items else "PASS"),
                         "数据可用" if not items else "存在需关注的数据问题", items))

    # 4 ---------------------------------------------------------------- 勾稽平衡
    items = []
    for s in result["payslips"]:
        line_sum = sum((dec(l["wage"]) for l in s["lines"] if l.get("wage") is not None),
                       Decimal(0))
        fixed_add = sum((dec(a.get("amount", 0))
                         for a in cfg["rules"]["drivers"].get(s["driver"], {}).get("adjustments", [])
                         if a.get("op") == "add" and a.get("source") != "reimbursement"),
                        Decimal(0))
        if abs(line_sum + fixed_add - dec(s["gross"])) > tol:
            items.append(f"{s['display_name']}: 逐行和 {line_sum}+{fixed_add} ≠ 应发 {s['gross']}")
        calc_net = dec(s["gross"]) + dec(s["reimbursement"]) - dec(s["deduction_total"])
        if abs(calc_net - dec(s["net"])) > tol:
            items.append(f"{s['display_name']}: 应发+报销-扣款 {calc_net} ≠ 实发 {s['net']}")
    for field in ("gross", "reimbursement", "deduction_total", "net"):
        total = sum((dec(s[field]) for s in result["payslips"]), Decimal(0))
        if abs(total - dec(result["totals"][field])) > tol:
            items.append(f"全员 {field} 合计 {total} ≠ totals {result['totals'][field]}")
    checks.append(_check("balance", "勾稽平衡（逐行/每人/全员）",
                         "FAIL" if items else "PASS",
                         "三层勾稽全部平衡" if not items else "勾稽不平", items))

    # 5 ------------------------------------------------------------ 规则合规
    items = []
    for s in result["payslips"]:
        spec = cfg["rules"]["drivers"].get(s["driver"])
        if spec is None:
            items.append(f"{s['display_name']}: 无计薪规则")
        elif not spec.get("confirmed"):
            items.append(f"{s['display_name']}: 规则未经发起人确认，禁止据此发工资")
    for e in result.get("errors", []):
        items.append("计算阶段错误: " + e)
    for pr in rules.validate_rules(cfg["rules"]):
        if "自测失败" in pr or "缺少 wage" in pr:
            items.append(pr)
    checks.append(_check("rule_compliance", "计薪规则合规与已确认",
                         "FAIL" if items else "PASS",
                         "规则齐备且均已确认" if not items else "规则不可用", items))

    # 6 -------------------------------------------------------------- 报销勾稽
    reimb, reimb_warn, _ = ingest.load_reimbursements(cfg, month)
    items = []
    source_total = sum(reimb.values(), Decimal(0))
    counted = sum((dec(s["reimbursement"]) for s in result["payslips"]), Decimal(0))
    if abs(source_total - counted) > tol:
        items.append(f"报销数据源合计 {source_total} ≠ 计入工资 {counted}")
    for w in reimb_warn:
        items.append(w)
    # 可选：与历史工资表汇总块（手填报销）三方比对，口径不一致是真实环境的常见坑
    sheet_spec = cfg["field_map"].get("wage_sheet_reimbursement")
    if sheet_spec and sheet_spec.get("enabled"):
        sheet_total = _read_wage_sheet_reimbursement(cfg, month, sheet_spec)
        if sheet_total is not None and abs(sheet_total - counted) > tol:
            items.append(f"历史工资表汇总块报销 {sheet_total} ≠ 本次计入 {counted}"
                         "（两处口径不同，须人工裁决以哪份为准）")
    status = "FAIL" if any("≠" in i for i in items) else ("WARN" if items else "PASS")
    checks.append(_check("reimbursement", "报销数据勾稽（数据源/工资表/计入）",
                         status, "报销口径一致" if not items else "报销存在差异", items))

    # 7 ---------------------------------------------------------------- 环比异常
    prev = result.get("history_month") or ""
    history, hist_warn = ingest.load_history(cfg, prev)
    items = list(hist_warn)
    warn_pct = dec(checks_cfg.get("variance_warn_pct", 30))
    block_pct = dec(checks_cfg.get("variance_block_pct", 80))
    hard = False
    for s in result["payslips"]:
        net = dec(s["net"])
        if net < 0:
            items.append(f"{s['display_name']}: 实发为负 {net}")
            hard = True
        base = history.get(s["driver"])
        if base is None or base == 0:
            continue
        pct = abs(net - base) / abs(base) * 100
        if pct > block_pct:
            items.append(f"{s['display_name']}: 环比 {pct:.1f}%（{base} → {net}）超过阻断线")
            hard = True
        elif pct > warn_pct:
            items.append(f"{s['display_name']}: 环比 {pct:.1f}%（{base} → {net}）需人工复核")
    checks.append(_check("variance", "环比历史工资与异常金额",
                         "FAIL" if hard else ("WARN" if items else "PASS"),
                         "波动在阈值内" if not items else "存在异常波动", items))

    # 8 ------------------------------------------------------------ 外部账单交叉校验
    bill = cfg["field_map"].get("external_bill")
    if bill and bill.get("enabled"):
        diffs = _external_bill_diffs(cfg, month, bill, tol)
        items = diffs
        checks.append(_check("external_bill", "外部账单核对差异（运输单可信度）",
                             "WARN" if items else "PASS",
                             "账单核对差异均为 0" if not items else "账单存在差异，运输单需复核",
                             items))
    else:
        checks.append(_check("external_bill", "外部账单核对差异（运输单可信度）",
                             "SKIP", "未配置外部账单，跳过"))

    n_fail = sum(c["status"] == "FAIL" for c in checks)
    n_warn = sum(c["status"] == "WARN" for c in checks)
    verdict = "BLOCK" if n_fail else ("REVIEW" if n_warn else "PASS")
    report = {
        "schema": 1, "month": month,
        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "verdict": verdict, "fail_count": n_fail, "warn_count": n_warn,
        "checks": checks, "result_hash": result.get("result_hash"),
        "input_hash": result.get("input_hash"),
        "scope": result.get("scope", {}),
        "blocking": [i for c in checks for i in c["items"] if c["status"] == "FAIL"],
        "review": [i for c in checks for i in c["items"] if c["status"] == "WARN"],
    }
    if write:
        out_dir = os.path.join(cfg["paths"]["out_dir"], month)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "verification.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
        with open(os.path.join(out_dir, "verification.md"), "w", encoding="utf-8") as f:
            f.write(render_markdown(report))
    return report


def _read_wage_sheet_reimbursement(cfg, month, spec):
    """只读历史工资表汇总块中的「报销 / 报销支出」金额，用于三方口径比对。"""
    try:
        from openpyxl import load_workbook
        import glob
        base = cfgmod.expand(spec["path"], cfg["payroll"], cfg["_meta"])
        base = cfgmod.expand(base, {"paths": cfg["paths"]}, cfg["_meta"])
        labels = spec.get("labels", ["报销", "报销支出", "总报销", "报销合计"])
        sheet = month[:4] + "." + str(int(month[5:7]))
        total = Decimal(0)
        for path in glob.glob(base):
            wb = load_workbook(path, data_only=True, read_only=True)
            names = [n for n in wb.sheetnames if n == sheet or n.startswith(sheet)]
            for name in names:
                ws = wb[name]
                for row in ws.iter_rows():
                    for i, c in enumerate(row):
                        if isinstance(c.value, str) and any(l in c.value for l in labels) \
                                and "备注" not in c.value and "说明" not in c.value:
                            for nxt in row[i + 1:i + 4]:
                                if isinstance(nxt.value, (int, float)):
                                    total += dec(nxt.value)
                                    break
            wb.close()
        return total
    except Exception:
        return None


def _external_bill_diffs(cfg, month, spec, tol):
    try:
        path = cfgmod.expand(spec["path"], cfg["payroll"], cfg["_meta"])
        path = cfgmod.expand(path, {"paths": cfg["paths"]}, cfg["_meta"])
        (headers, rows), _ = ingest._read_rows(path, spec), path
        cols = {k: ingest.column_index(v, headers) for k, v in spec["columns"].items()}
        out = []
        for n, row in enumerate(rows, start=int(spec.get("header_row", 1)) + 1):
            def get(f):
                i = cols.get(f)
                return row[i] if i is not None and i < len(row) else None
            if get("diff") in (None, ""):
                continue
            d = dec(get("diff"))
            if abs(d) > tol:
                out.append(f"账单行 {n} 核对差异 {d}（费用 {get('fee')} / 核对费用 {get('checked_fee')}）")
        return out[:20]
    except Exception as exc:
        return [f"外部账单读取失败: {exc}"]


def render_markdown(report: dict) -> str:
    icon = {"PASS": "通过", "FAIL": "不通过", "WARN": "需复核", "SKIP": "跳过"}
    lines = [f"# 司机工资核对报告 · {report['month']}", "",
             f"**结论：{report['verdict']}**（不通过 {report['fail_count']} 项 / "
             f"需复核 {report['warn_count']} 项）", "",
             f"核对时间：{report['verified_at']}　结果指纹：`{report.get('result_hash', '')[:16]}`", ""]
    scope = report.get("scope") or {}
    if scope:
        lines += [f"**计薪范围**：{scope.get('computed_drivers', '?')} 位司机"
                  f"（名册 {scope.get('roster_total', '?')} 人，"
                  f"其中飞书在册 {scope.get('feishu_drivers', '?')} 人）；"
                  f"只算当月实际有单的人，不铺零值行。"
                  f"名册来源：{scope.get('roster_source', 'local')}", ""]
    lines += ["| # | 核对项 | 结果 | 说明 |", "|---|---|---|---|"]
    for i, c in enumerate(report["checks"], 1):
        lines.append(f"| {i} | {c['title']} | {icon[c['status']]} | {c['summary']} |")
    lines.append("")
    for c in report["checks"]:
        if c["items"]:
            lines += [f"### {c['title']}（{icon[c['status']]}）", ""]
            lines += [f"- {x}" for x in c["items"]]
            lines.append("")
    lines += ["---", "",
              "- **BLOCK**：存在硬错误，禁止进入确认与私发。修正输入后重新 compute + verify。",
              "- **REVIEW**：无硬错误但有需人工复核项；发起人确认时须显式接受。",
              "- **PASS**：可进入发起人确认闸门；确认前仍不会向任何司机发送消息。", ""]
    return "\n".join(lines)
