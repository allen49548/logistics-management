#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py —— 编排：模板 -> 数据源分组 -> 报销聚合 -> 生成 sheet -> 精确插入/追加。

关键设计（2026-11-19 用户定案：只增不覆盖）：
- 一切路径/字段/命名来自配置，无硬编码业务名。
- 目标标识 = 数据源（图表/报表）里的司机名称：以司机名定位 target_dir 下
  {driver}{year}.xlsx，只处理数据源里出现的司机；无数据源时才回退到全量目标司机
  生成空 sheet 框架。
- 只新增、绝不覆盖：sheet 不存在则新建写入；sheet 已存在则按去重指纹只追加
  本轮真正新增的行，保留已有 sheet 与原有内容，不重建、不清空、不替换。
  exist_policy=overwrite 在写入层被强制降级为 append（覆盖能力此技能不再提供）。
- 性能：数据源一次遍历分组；模板只读一次；每个司机一张新 sheet 一次写入。
"""
from __future__ import annotations

import os
import re
import json
from decimal import Decimal

from xlsxzip import (insert_sheet, list_sheets, _read_parts,
                     read_sheet_data, append_rows, create_workbook_from_template)
from sheet_writer import TemplateInfo, build_sheet_xml, build_append_rows_xml
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


def _fp_cell(v) -> str:
    """去重指纹的单元格归一化：与 read_sheet_data 读旧行规则保持一致。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v).strip()


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

    # 4) 目标标识 = 数据源（图表/报表）里的司机名称（2026-11-19 用户定案）。
    #    只处理数据源里出现的司机，用司机名定位 target_dir 下的 {driver}{year}.xlsx；
    #    没有任何数据源时才回退到全部目标司机（生成空 sheet 框架，兼容旧行为）。
    #    不再把 all_drivers 与 src_drivers 求并集——那会把无数据的司机也扫进来。
    if src_drivers:
        candidates = list(src_drivers)
    else:
        candidates = list(all_drivers)
    # 显式 exclude 仍生效
    exclude = set(cfg.get("drivers", {}).get("exclude", []) or [])
    candidates = [d for d in candidates if d not in exclude]

    write_cfg = cfg.get("run", {})
    dry_run = bool(write_cfg.get("dry_run", True))
    sheet_cfg = cfg.get("sheet", {})
    # 覆盖能力此技能不再提供：无论配置成什么，写入层一律按「只增不覆盖」处理。
    exist_policy = "append"

    # 5) 样式桥接：优先用第一个存在的目标文件做基准；若全部为新建文件，
    #    则直接用模板自身 styles 做基准（新建文件本来就复制模板部件，样式天然一致）。
    style_source_path = None
    for d in candidates:
        p = target_path(resolved, d, year, month)
        if os.path.isfile(p):
            style_source_path = p
            break
    try:
        tpl_parts = _read_parts(template)
        tpl_styles_key = next((k for k in tpl_parts if k.endswith("/styles.xml")), "xl/styles.xml")
        if tpl_styles_key not in tpl_parts:
            report["errors"].append("模板缺少 styles.xml")
            return report
        if style_source_path:
            target_styles = _read_parts(style_source_path)["xl/styles.xml"]
        else:
            target_styles = tpl_parts[tpl_styles_key]
        used_xfs = collect_used_xfs(info, cfg)
        new_styles_xml, style_map = bridge_styles(tpl_parts[tpl_styles_key], target_styles, used_xfs)
    except Exception as e:
        report["errors"].append("样式桥接失败: %s: %s" % (type(e).__name__, e))
        return report

    # 6) 逐司机：目标标识=司机名；sheet 不存在则新建，已存在则去重追加
    column_map = cfg.get("template", {}).get("column_map", {})
    header_name_to_col = {name: c for c, name in info.headers}
    col_count = max(header_name_to_col.values(), default=0)

    # 去重指纹列：配置 sheet.dedup_columns（模板列名列表）。未配置时用整行。
    # 例：["日期","车牌","司机","路线"] 表示这四项相同即视为同一条，不重复追加。
    dedup_names = sheet_cfg.get("dedup_columns", []) or []
    fingerprint_cols = [header_name_to_col[n] for n in dedup_names if n in header_name_to_col]

    bdir = resolved["backup_dir"] or None
    verify = bool(write_cfg.get("verify_after_write", True))

    for driver in candidates:
        tpath = target_path(resolved, driver, year, month)
        file_exists = os.path.isfile(tpath)
        existing = list_sheets(tpath) if file_exists else []
        sheet_exists = file_exists and (sheet_name in existing)

        # --- 组装本轮该司机的新行 ---
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

        # --- 情形 A：sheet 已存在 → 只读旧数据、去重后追加（绝不覆盖）---
        if sheet_exists:
            sd = read_sheet_data(tpath, sheet_name, fingerprint_cols or None)
            if not sd.get("ok"):
                report["errors"].append("读取已有 sheet 失败(%s): %s" % (driver, sd.get("errors")))
                continue
            seen_fps = {r["fp"] for r in sd["rows"] if r["row"] >= sd["data_start_row"]}
            dedup_rows, dup_count = [], 0
            for row in rows:
                # 归一化须与 read_sheet_data 一致：None/越界一律 ""，其余 str().strip()
                fp = "|".join(_fp_cell(row[i - 1] if i - 1 < len(row) else None)
                              for i in (fingerprint_cols or list(range(1, col_count + 1))))
                if fp in seen_fps:
                    dup_count += 1
                    continue
                seen_fps.add(fp)  # 本批内部也去重
                dedup_rows.append(row)
            entry = {"driver": driver, "file": tpath, "sheet": sheet_name,
                     "mode": "append", "incoming": len(rows),
                     "added": len(dedup_rows), "duplicates": dup_count}
            if dry_run:
                entry["dry_run"] = True
                report["sheets"].append(entry)
                continue
            if not dedup_rows:
                entry.update({"ok": True, "noop": True, "reason": "无新增行(全部重复或无数据)"})
                report["sheets"].append(entry)
                continue
            # 复用已有数据行的样式索引：恒等映射，无需注入 styles.xml，零样式改动
            data_rows = [r for r in sd["rows"] if r["row"] >= sd["data_start_row"]]
            style_tpl = data_rows[0]["styles"] if data_rows else {}
            start_row = (sd["max_row"] or (sd["data_start_row"] - 1)) + 1
            try:
                row_xmls = build_append_rows_xml(info, dedup_rows, {}, cfg,
                                                  start_row=start_row,
                                                  template_col_styles=style_tpl)
            except Exception as e:
                report["errors"].append("生成追加行失败(%s): %s: %s" % (driver, type(e).__name__, e))
                continue
            res = append_rows(tpath, sheet_name, row_xmls, bdir, verify=verify)
            entry.update({"ok": res.get("ok"), "errors": res.get("errors"),
                          "backup": res.get("backup"), "start_row": start_row})
            report["sheets"].append(entry)
            continue

        # --- 情形 B：sheet 不存在（文件或 sheet 缺失）→ 新建后写入 ---
        if not rows:
            # 无数据源时仍写一行空骨架，保留模板样式（兼容旧行为）
            rows = [[None] * col_count]
        try:
            sheet_xml = build_sheet_xml(info, rows, style_map, cfg, include_header=True)
        except Exception as e:
            report["errors"].append("生成 sheet XML 失败(%s): %s: %s" % (driver, type(e).__name__, e))
            continue
        mode = "new_sheet" if file_exists else "new_file"
        entry = {"driver": driver, "file": tpath, "sheet": sheet_name,
                 "rows": len(rows) if recs else 0, "mode": mode}
        if dry_run:
            entry["dry_run"] = True
            entry["xml_bytes"] = len(sheet_xml)
            report["sheets"].append(entry)
            continue
        if file_exists:
            res = insert_sheet(tpath, sheet_name, sheet_xml, bdir, verify=verify,
                               extra_parts={"xl/styles.xml": new_styles_xml})
        else:
            # 目标司机文件不存在：以模板为骨架新建，保留 WPS 特有部件
            res = create_workbook_from_template(template, tpath, sheet_name, sheet_xml, bdir,
                                                extra_parts={"xl/styles.xml": new_styles_xml})
        entry.update({"ok": res.get("ok"), "errors": res.get("errors"),
                      "backup": res.get("backup")})
        report["sheets"].append(entry)

    report["ok"] = len(report["errors"]) == 0
    return report
