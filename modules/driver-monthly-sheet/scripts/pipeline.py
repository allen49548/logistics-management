#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py —— 编排：模板 -> 数据源分组 -> 报销聚合 -> 生成 sheet -> 精确插入。

关键设计：
- 一切路径/字段/命名来自配置，无硬编码业务名。
- 幂等两阶段：无数据源先建空 sheet；有数据源填入，exist_policy=fill 时
  对已存在 sheet 只填空单元格、保留公式与原数据，绝不重建。
- 性能：数据源一次遍历分组；模板只读一次；每个司机一张新 sheet 一次写入。
"""
from __future__ import annotations

import os
import re
import json
from decimal import Decimal

from xlsxzip import insert_sheet, fill_sheet, list_sheets, _read_parts
from sheet_writer import TemplateInfo, build_sheet_xml
from styles_bridge import bridge_styles, collect_used_xfs
from source_reader import read_source, driver_names
from reimb_reader import read_reimbursement


def resolve_paths(cfg: dict, year: int, month: int, base_dir: str) -> dict:
    pc = cfg.get("paths", {})
    workdir = pc.get("workdir", "")
    ctx = {"workdir": workdir, "year": year, "month": month,
           "month2": "%02d" % month}

    def rp(key):
        v = pc.get(key, "")
        if not v:
            return ""
        v = v.format(**ctx)
        if not os.path.isabs(v):
            v = os.path.join(base_dir, v)
        return os.path.normpath(v)

    return {
        "workdir": workdir if os.path.isabs(workdir) else os.path.join(base_dir, workdir),
        "template": rp("template"),
        "source": rp("source"),
        "target_dir": rp("target_dir"),
        "backup_dir": rp("backup_dir"),
        "state_dir": rp("state_dir"),
        "target_file": pc.get("target_file", "{target_dir}/{driver}{year}.xlsx"),
    }


def target_path(resolved: dict, driver: str, year: int, month: int) -> str:
    ctx = {"target_dir": resolved["target_dir"], "workdir": resolved["workdir"],
           "driver": driver, "year": year, "month": month, "month2": "%02d" % month}
    p = resolved["target_file"].format(**ctx)
    if not os.path.isabs(p):
        p = os.path.join(resolved["workdir"], p)
    return os.path.normpath(p)


def sheet_name_for(cfg: dict, year: int, month: int) -> str:
    pattern = cfg.get("sheet", {}).get("name_pattern", "{year}年{month}月")
    m = month
    if cfg.get("sheet", {}).get("month_zero_pad"):
        m = "%02d" % month
    return pattern.format(year=year, month=m, month2="%02d" % month)


def _is_temp_file(fn: str) -> bool:
    """WPS/Excel 打开文件时生成的临时锁文件，不是真 xlsx，必须跳过。"""
    return fn.startswith("~$") or fn.startswith(".~") or fn.startswith("~") or fn.startswith(".")


def _discover_drivers(resolved: dict, cfg: dict, year: int) -> list:
    dc = cfg.get("drivers", {})
    explicit = dc.get("list", []) or []
    exclude = set(dc.get("exclude", []) or [])
    found = list(explicit)
    if dc.get("from", "target_dir") == "target_dir":
        tdir = resolved["target_dir"]
        if os.path.isdir(tdir):
            pat = re.compile(r"^(?P<driver>.+)%d\.xlsx$" % year)
            for fn in os.listdir(tdir):
                if _is_temp_file(fn):
                    continue
                m = pat.match(fn)
                if m:
                    found.append(m.group("driver"))
    seen, out = set(), []
    for d in found:
        if d in seen or d in exclude:
            continue
        seen.add(d)
        out.append(d)
    return out


def _map_value(tpl_col_name: str, sem: str, rec: dict, reimb: dict, cfg: dict):
    """按 column_map 的语义键从数据源/报销取一个模板单元格的值。"""
    if sem in ("blank", "", None):
        return None
    if isinstance(sem, str) and sem.startswith("literal:"):
        return sem[len("literal:"):]
    if sem and sem.startswith("source."):
        key = sem[len("source."):]
        # 优先语义键（别名归一），再原始列名
        if key in rec:
            return rec[key]
        aliases = cfg.get("source", {}).get("column_aliases", {})
        if key in aliases:
            for cand in aliases[key]:
                if cand in rec:
                    return rec[cand]
        return None
    if sem and sem.startswith("reimb."):
        key = sem[len("reimb."):]
        r = reimb or {}
        if key == "count":
            return r.get("count", 0)
        if key in ("amount", "amount_sum", "total"):
            a = r.get("amount", Decimal("0"))
            return float(a) if isinstance(a, Decimal) else a
        if key.startswith("rows["):
            return None
        return r.get(key)
    # 无前缀：先当语义键再当原始列名
    if sem in rec:
        return rec[sem]
    return None


def run(cfg: dict, year: int, month: int, base_dir: str,
        source_override: str | None = None) -> dict:
    report = {"ok": False, "year": year, "month": month, "sheets": [],
              "skipped": [], "errors": [], "warnings": [], "source": {}}
    resolved = resolve_paths(cfg, year, month, base_dir)
    template = resolved["template"]
    if not template or not os.path.isfile(template):
        report["errors"].append("模板文件不存在: %s" % template)
        return report

    sheet_name = sheet_name_for(cfg, year, month)

    # 1) 解析模板
    try:
        info = TemplateInfo.from_template(template, cfg)
    except Exception as e:
        report["errors"].append("解析模板失败: %s: %s" % (type(e).__name__, e))
        return report
    report["template"] = {"file": template, "headers": [n for _, n in info.headers]}

    # 2) 数据源（可不存在 -> 生成空 sheet）
    src_path = source_override or resolved["source"]
    source_result = None
    if src_path and os.path.isfile(src_path):
        source_result = read_source(src_path, cfg)
        report["source"] = {"file": src_path, "ok": source_result["ok"],
                            "driver_count": len(source_result["drivers"]),
                            "warnings": source_result["warnings"],
                            "errors": source_result["errors"]}
        if not source_result["ok"]:
            report["errors"].extend(source_result["errors"])
            return report
        src_drivers = driver_names(source_result)
    else:
        report["source"] = {"file": src_path, "ok": False, "reason": "数据源不存在，生成空 sheet"}
        src_drivers = []

    # 3) 报销聚合
    all_drivers = _discover_drivers(resolved, cfg, year)
    reimb_result = read_reimbursement(cfg, year, month, all_drivers or (src_drivers or None))
    report["reimbursement"] = {"ok": reimb_result["ok"], "mode": reimb_result["mode"],
                               "drivers": list(reimb_result["data"].keys()),
                               "errors": reimb_result["errors"]}

    # 4) 确定要处理的司机：有目标文件的；无数据时用全部目标司机（生成空 sheet）
    candidates = all_drivers if all_drivers else src_drivers
    candidates = sorted(set(candidates) | set(src_drivers))

    write_cfg = cfg.get("run", {})
    dry_run = bool(write_cfg.get("dry_run", True))
    exist_policy = cfg.get("sheet", {}).get("exist_policy", "fill")

    # 5) 读目标工作簿 styles.xml（用第一个存在的目标文件做样式基准）
    style_source_path = None
    for d in candidates:
        p = target_path(resolved, d, year, month)
        if os.path.isfile(p):
            style_source_path = p
            break
    if not style_source_path:
        report["errors"].append("找不到任何目标司机表文件，无法写入（target_file=%s）" % resolved["target_file"])
        return report
    try:
        target_parts = _read_parts(style_source_path)
        target_styles = target_parts["xl/styles.xml"]
        tpl_parts = _read_parts(template)
        tpl_styles_key = next((k for k in tpl_parts if k.endswith("/styles.xml")), "xl/styles.xml")
        if tpl_styles_key not in tpl_parts:
            report["errors"].append("模板缺少 styles.xml")
            return report
        used_xfs = collect_used_xfs(info, cfg)
        new_styles_xml, style_map = bridge_styles(tpl_parts[tpl_styles_key], target_styles, used_xfs)
    except Exception as e:
        report["errors"].append("样式桥接失败: %s: %s" % (type(e).__name__, e))
        return report

    # 6) 逐司机生成 sheet
    column_map = cfg.get("template", {}).get("column_map", {})
    header_name_to_col = {name: c for c, name in info.headers}
    col_count = max(header_name_to_col.values(), default=0)

    for driver in candidates:
        tpath = target_path(resolved, driver, year, month)
        if not os.path.isfile(tpath):
            report["skipped"].append({"driver": driver, "reason": "目标文件不存在", "file": tpath})
            continue
        existing = list_sheets(tpath)
        if sheet_name in existing and exist_policy == "skip":
            report["skipped"].append({"driver": driver, "reason": "sheet 已存在且策略为 skip", "file": tpath})
            continue
        if sheet_name in existing and exist_policy == "fill":
            # fill 语义：已存在即不重建、不动原文件（含不注入 styles），保证幂等零改动
            report["skipped"].append({"driver": driver, "reason": "sheet 已存在(fill 不重建)", "file": tpath})
            continue

        # 组装行：有该司机数据就逐条；无则空行
        recs = (source_result["drivers"].get(driver, []) if source_result else []) or []
        reimb = reimb_result["data"].get(driver, {})
        rows = []
        for rec in recs:
            row = [None] * col_count
            for col_name, sem in column_map.items():
                if col_name in header_name_to_col:
                    ci = header_name_to_col[col_name]
                    row[ci - 1] = _map_value(col_name, sem, rec, reimb, cfg)
            rows.append(row)
        if not rows:
            # 空 sheet：仍写一行空数据以保留样式骨架（字段来自模板表头）
            rows = [[None] * col_count]

        include_header = True
        try:
            sheet_xml = build_sheet_xml(info, rows, style_map, cfg, include_header=include_header)
        except Exception as e:
            report["errors"].append("生成 sheet XML 失败(%s): %s: %s" % (driver, type(e).__name__, e))
            continue

        entry = {"driver": driver, "file": tpath, "sheet": sheet_name,
                 "rows": len(rows) if recs else 0, "mode": "new"}
        if sheet_name in existing:
            entry["mode"] = exist_policy

        if dry_run:
            entry["dry_run"] = True
            entry["xml_bytes"] = len(sheet_xml)
            report["sheets"].append(entry)
            continue

        bdir = resolved["backup_dir"] or None
        verify = bool(write_cfg.get("verify_after_write", True))
        extra = {"xl/styles.xml": new_styles_xml}
        if sheet_name in existing and exist_policy == "overwrite":
            res = fill_sheet(tpath, sheet_name, sheet_xml, bdir, verify=verify, extra_parts=extra)
        else:
            res = insert_sheet(tpath, sheet_name, sheet_xml, bdir, verify=verify, extra_parts=extra)
        entry.update({"ok": res.get("ok"), "errors": res.get("errors"),
                      "backup": res.get("backup")})
        report["sheets"].append(entry)

    report["ok"] = len(report["errors"]) == 0
    return report
