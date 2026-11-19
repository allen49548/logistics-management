#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""source_reader.py —— 读数据源表格文档（单表+司机姓名列），按司机分组。

不硬编码列名：driver_column 与 column_aliases 全部来自配置。
用 openpyxl read_only 流式读取，一次遍历完成分组，性能好。
"""
from __future__ import annotations

import os
from collections import OrderedDict


def _norm(s) -> str:
    return "" if s is None else str(s).strip()


def _match_header(header: str, candidates: list) -> bool:
    h = _norm(header)
    return any(_norm(c) and _norm(c) == h for c in candidates)


def _excel_serial_to_date(v):
    from datetime import datetime, timedelta
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
    return v


def read_source(path: str, cfg: dict) -> dict:
    """返回 {ok, drivers: {司机: [ {语义键: 值}, ... ]}, headers, col_index, warnings}。"""
    scfg = cfg["source"]
    out = {"ok": False, "file": path, "drivers": OrderedDict(),
           "headers": [], "col_index": {}, "warnings": [], "errors": []}
    if not path or not os.path.isfile(path):
        out["errors"].append("数据源文件不存在: %s" % path)
        return out
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            sn = scfg.get("sheet")
            ws = wb[sn] if (sn and sn in wb.sheetnames) else wb.worksheets[0]
            rows = ws.iter_rows(values_only=True)
            hr = int(scfg.get("header_row", 1))
            header = None
            for i, row in enumerate(rows, start=1):
                if i == hr:
                    header = [_norm(v) for v in row]
                    break
            if header is None:
                out["errors"].append("数据源没有第 %d 行表头" % hr)
                return out
            out["headers"] = header

            # 解析司机列
            driver_candidates = scfg.get("driver_column", ["司机", "姓名"])
            dcol = next((i for i, h in enumerate(header) if _match_header(h, driver_candidates)), None)
            if dcol is None:
                out["errors"].append("数据源找不到司机列（候选: %s；实际表头: %s）" %
                                     (", ".join(driver_candidates), ", ".join(header)))
                return out

            # 语义键 -> 列下标
            aliases = scfg.get("column_aliases", {})
            semantic_col = {}
            used = set()
            for sem, cands in aliases.items():
                idx = next((i for i, h in enumerate(header) if h and _match_header(h, cands)), None)
                if idx is not None:
                    semantic_col[sem] = idx
                    used.add(idx)
                else:
                    out["warnings"].append("数据源缺少语义列 %s（候选 %s）" % (sem, cands))
            out["col_index"] = semantic_col

            # 同时保留原始列名 -> 列下标（供 column_map 的 source.<原始列名> 使用）
            raw_col = {h: i for i, h in enumerate(header) if h}

            skip_empty = bool(scfg.get("skip_empty_rows", True))
            dsr = int(scfg.get("data_start_row", hr + 1))
            for i, row in enumerate(rows, start=hr + 1):
                if i < dsr:
                    continue
                if dcol >= len(row):
                    continue
                driver = _norm(row[dcol])
                if not driver:
                    if skip_empty:
                        continue
                    driver = "未识别司机"
                driver = _normalize_driver(driver, cfg)
                rec = {}
                nonempty = False
                for sem, idx in semantic_col.items():
                    if idx < len(row):
                        v = row[idx]
                        rec[sem] = v
                        if v not in (None, ""):
                            nonempty = True
                # 原始字段也写入（键为原始列名），兼容 source.<列名> 映射
                for h, idx in raw_col.items():
                    if idx < len(row):
                        rec.setdefault(h, row[idx])
                rec["_driver"] = driver
                rec["_row"] = i
                if skip_empty and not nonempty:
                    continue
                out["drivers"].setdefault(driver, []).append(rec)
            out["ok"] = True
            out["raw_col"] = raw_col
        finally:
            wb.close()
    except Exception as e:
        out["errors"].append("读取数据源异常: %s: %s" % (type(e).__name__, e))
    return out


def _normalize_driver(name: str, cfg: dict) -> str:
    aliases = cfg["source"].get("driver_aliases", {}) or {}
    mode = cfg.get("drivers", {}).get("name_normalize", "strip")
    n = name.strip() if mode in ("strip", "") else name
    return aliases.get(n, n)


def driver_names(source_result: dict) -> list:
    return list(source_result.get("drivers", {}).keys())
