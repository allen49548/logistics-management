"""从单个月份工作表中动态识别「项目 → 金额」。

两种取数策略，全部由配置驱动：
  1. ``label``  —— 汇总区标签（如「车贷支出」）+ 右侧最近的数字；
  2. ``detail`` —— 明细表头（如「运费」「工资」）+ 合计行（无合计行时按明细求和）。
本模块只负责"识别到什么"，不判断谁优先——优先级由 calculator 按配置决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl

from common import normalize_label, parse_number, period_text, statement_like
from config import Config
from discovery import detect_sheet


@dataclass
class RawMatch:
    item_key: str
    category: str
    item_name: str
    value: float
    strategy: str
    cell: str
    label: str = ""
    alias: str = ""
    alias_rank: int = 99
    note: str = ""
    header: str = ""
    from_total_row: bool = True
    duplicates: List[str] = field(default_factory=list)


@dataclass
class MonthExtraction:
    period: Dict[str, int]
    period_text: str
    sheet: Optional[str] = None
    matches: Dict[str, List[RawMatch]] = field(default_factory=dict)
    anomalies: List[str] = field(default_factory=list)

    def add(self, match: RawMatch) -> None:
        bucket = self.matches.setdefault(match.item_key, [])
        # 同项目、同策略、同金额视为同一事实（如「车贷支出」与「车贷月供」都写了 1000），只留一条
        for existing in bucket:
            if existing.strategy == match.strategy and abs(existing.value - match.value) <= 0.005:
                dup = f"{match.label or match.header}@{match.cell}"
                if dup not in existing.duplicates:
                    existing.duplicates.append(dup)
                return
        bucket.append(match)

    @property
    def available(self) -> bool:
        return self.sheet is not None and bool(self.matches)


def _alias_index(cfg: Config) -> Dict[str, List[tuple]]:
    """归一化别名 → [(item_key, alias_rank)]；同一别名可被多个项目声明。"""
    index: Dict[str, List[tuple]] = {}
    seen: set = set()
    for item in cfg.items:
        excludes = [normalize_label(x) for x in item.get("alias_exclude", [])]
        for rank, alias in enumerate(item.get("aliases", [])):
            norm = normalize_label(alias)
            if not norm:
                continue
            if any(ex and ex in norm for ex in excludes):
                continue
            unique = (item["key"], norm)
            if unique in seen:  # 「工资（元）」与「工资」归一化后相同，只保留优先级更高的一条
                continue
            seen.add(unique)
            index.setdefault(norm, []).append((item["key"], rank, alias, excludes))
    return index


def _row_notes(ws, row: int, start_col: int, cfg: Config, known: set) -> str:
    """抓取金额右侧的短文本批注（如「500管理费」），用于解释口径。"""
    max_off = int(cfg.get("scan.amount.note_max_offset", 3))
    max_len = int(cfg.get("scan.amount.note_max_len", 24))
    notes: List[str] = []
    for offset in range(1, max_off + 1):
        col = start_col + offset
        if col > ws.max_column:
            break
        value = ws.cell(row=row, column=col).value
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or parse_number(text, cfg.get("scan.amount.strip_chars", [])) is not None:
            continue
        if normalize_label(text, bool(cfg.get("scan.label.strip_parenthetical", True))) in known:
            continue
        if len(text) <= max_len:
            notes.append(text)
    return " / ".join(notes)


def _extract_labels(ws, cfg: Config, extraction: MonthExtraction, alias_index: Dict[str, List[tuple]]) -> None:
    strip_paren = bool(cfg.get("scan.label.strip_parenthetical", True))
    max_len = int(cfg.get("scan.label.max_len", 24))
    max_offset = int(cfg.get("scan.amount.max_offset", 4))
    strip_chars = cfg.get("scan.amount.strip_chars", [])
    stop_at_label = bool(cfg.get("scan.amount.stop_at_known_label", True))
    header_rows = {int(r) for r in cfg.get("detail.header_rows", [1])}
    known = set(alias_index.keys())

    for row in ws.iter_rows():
        for cell in row:
            raw = cell.value
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            if not text or len(text) > max_len:
                continue
            norm = normalize_label(text, strip_paren)
            if norm not in alias_index:
                continue
            if cell.row in header_rows:
                continue  # 表头行只供 detail 策略使用，不当作汇总标签

            value: Optional[float] = None
            value_cell = ""
            for offset in range(1, max_offset + 1):
                col = cell.column + offset
                if col > ws.max_column:
                    break
                probe = ws.cell(row=cell.row, column=col)
                if probe.value is None or (isinstance(probe.value, str) and not probe.value.strip()):
                    continue
                if isinstance(probe.value, str) and stop_at_label:
                    probe_norm = normalize_label(probe.value, strip_paren)
                    if probe_norm in known:
                        break
                parsed = parse_number(probe.value, strip_chars)
                if parsed is not None:
                    value = parsed
                    value_cell = probe.coordinate
                    break
                if statement_like(probe.value):
                    extraction.anomalies.append(
                        f"单元格 {probe.coordinate} 为公式但无缓存值，无法读取「{text}」"
                    )
                    break
            if value is None:
                continue

            note = _row_notes(ws, cell.row, cell.column + offset, cfg, known)

            for item_key, rank, alias, _excludes in alias_index[norm]:
                item = cfg.item(item_key)
                extraction.add(
                    RawMatch(
                        item_key=item_key,
                        category=item["category"],
                        item_name=item.get("name", item_key),
                        value=value,
                        strategy="label",
                        cell=value_cell,
                        label=text,
                        alias=alias,
                        alias_rank=rank,
                        note=note,
                    )
                )


def _find_total_row(ws, cfg: Config) -> Optional[int]:
    keywords = [normalize_label(k) for k in cfg.get("detail.total_row_keywords", ["合计", "总计"])]
    for row in range(1, ws.max_row + 1):
        for col in (1, 2, 3):
            value = ws.cell(row=row, column=col).value
            if not isinstance(value, str):
                continue
            norm = normalize_label(value)
            if any(kw and kw in norm for kw in keywords):
                return row
    return None


def _find_header_cell(ws, cfg: Config, spec: Dict[str, Any]) -> Optional[tuple]:
    rows = [int(r) for r in cfg.get("detail.header_rows", [1])]
    keywords = [normalize_label(k) for k in spec.get("header_keywords", [])]
    excludes = [normalize_label(k) for k in spec.get("header_exclude", [])]
    for row in rows:
        if row > ws.max_row:
            continue
        for col in range(1, ws.max_column + 1):
            value = ws.cell(row=row, column=col).value
            if not isinstance(value, str):
                continue
            norm = normalize_label(value)
            if not norm:
                continue
            if not any(kw in norm for kw in keywords):
                continue
            if any(ex and ex in norm for ex in excludes):
                continue
            return row, col, value.strip()
    return None


def _first_label_row(ws, alias_index: Dict[str, List[tuple]], start_row: int, cfg: Config) -> Optional[int]:
    """找汇总区起点：第一条命中项目别名的行，用于界定明细数据的结束。"""
    strip_paren = bool(cfg.get("scan.label.strip_parenthetical", True))
    max_len = int(cfg.get("scan.label.max_len", 24))
    for row in range(start_row, ws.max_row + 1):
        for col in range(1, ws.max_column + 1):
            value = ws.cell(row=row, column=col).value
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > max_len:
                continue
            if normalize_label(text, strip_paren) in alias_index:
                return row
    return None


def _extract_detail(ws, cfg: Config, extraction: MonthExtraction, alias_index: Dict[str, List[tuple]]) -> None:
    if not cfg.get("detail.enabled", True):
        return
    strip_chars = cfg.get("scan.amount.strip_chars", [])
    total_row = _find_total_row(ws, cfg)
    header_rows = [int(r) for r in cfg.get("detail.header_rows", [1])]
    first_data_row = max(header_rows) + 1

    label_row = _first_label_row(ws, alias_index, first_data_row, cfg)
    bounds = [ws.max_row]
    if total_row and total_row > first_data_row:
        bounds.append(total_row - 1)
    if label_row:
        bounds.append(label_row - 1)
    data_end = max(first_data_row, min(bounds))

    for item in cfg.items:
        spec = item.get("detail")
        if not spec:
            continue
        hit = _find_header_cell(ws, cfg, spec)
        if not hit:
            continue
        _, col, header = hit

        value: Optional[float] = None
        cell_ref = ""
        from_total = False
        if total_row and total_row > first_data_row:
            candidate = parse_number(ws.cell(row=total_row, column=col).value, strip_chars)
            if candidate is not None:
                value = candidate
                cell_ref = ws.cell(row=total_row, column=col).coordinate
                from_total = True

        if value is None and spec.get("allow_sum_fallback", cfg.get("detail.fallback_to_sum", True)):
            stop_at_subtotal = bool(cfg.get("detail.stop_at_subtotal", True))
            total = 0.0
            counted = 0
            last_row = first_data_row
            for row in range(first_data_row, data_end + 1):
                parsed = parse_number(ws.cell(row=row, column=col).value, strip_chars)
                if parsed is None:
                    continue
                # 遇到"等于已累计值"的行视为小计行（表内常见无关键词的小计），不重复累加
                if stop_at_subtotal and counted >= 2 and abs(parsed - total) <= max(abs(total) * 1e-6, 0.01):
                    break
                total += parsed
                counted += 1
                last_row = row
            if counted:
                value = round(total, 6)
                cell_ref = (
                    f"SUM({ws.cell(row=first_data_row, column=col).coordinate}:"
                    f"{ws.cell(row=last_row, column=col).coordinate})"
                )

        if value is None:
            continue
        extraction.add(
            RawMatch(
                item_key=item["key"],
                category=item["category"],
                item_name=item.get("name", item["key"]),
                value=value,
                strategy="detail",
                cell=cell_ref,
                header=header,
                from_total_row=from_total,
            )
        )


def extract_month(path: Path, cfg: Config, period: Dict[str, int]) -> MonthExtraction:
    extraction = MonthExtraction(period=period, period_text=period_text(period))
    hit = detect_sheet(path, cfg, period)
    if not hit.sheet:
        extraction.anomalies.append(
            cfg.msg("missing_sheet", sheet=cfg.sheet_name(period), month=period["month"]) + f"：{hit.reason}"
        )
        return extraction

    extraction.sheet = hit.sheet
    if hit.reason:
        extraction.anomalies.append(hit.reason)

    try:
        workbook = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:  # noqa: BLE001
        extraction.anomalies.append(f"源文件无法打开（{type(exc).__name__}: {exc}）")
        extraction.sheet = None
        return extraction

    try:
        ws = workbook[hit.sheet]
        alias_index = _alias_index(cfg)
        _extract_labels(ws, cfg, extraction, alias_index)
        _extract_detail(ws, cfg, extraction, alias_index)
    finally:
        workbook.close()
    return extraction
