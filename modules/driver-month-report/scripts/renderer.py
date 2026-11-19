"""把计算结果渲染成管理者可读的 Excel 报表。

版式、列、KPI 卡片、涨跌配色、数字格式全部来自配置（config/report.json），
本模块不含任何项目名、金额或结论的硬编码。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from calculator import DriverResult
from config import Config

FONT_NAME = "PingFang SC"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
ACCENT_FILL = PatternFill("solid", fgColor="D9E2F3")
SECTION_FILL = PatternFill("solid", fgColor="2F5597")
TOTAL_FILL = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TITLE_FONT = Font(name=FONT_NAME, size=20, bold=True, color="1F3864")
SUBTITLE_FONT = Font(name=FONT_NAME, size=9, color="595959")
HEADER_FONT = Font(name=FONT_NAME, size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name=FONT_NAME, size=10, color="262626")
BOLD_FONT = Font(name=FONT_NAME, size=10, bold=True, color="262626")
NOTE_FONT = Font(name=FONT_NAME, size=9, color="595959")


def _fill(color: str) -> PatternFill:
    return PatternFill("solid", fgColor=color)


def _trend_text(key: str, cfg: Config) -> str:
    return {"up": "↑ 上升", "down": "↓ 下降", "flat": "— 持平"}.get(key, "—")


def _note_text(result: DriverResult, cfg: Config, limit: int = 360) -> str:
    """汇总表的说明列只留决策相关信息；逐单元格的取数来源放「项目明细」页。"""
    seen, notes = set(), []
    for note in result.notes:
        clean = " ".join(str(note).split())
        if not clean or clean in seen or "[来源]" in clean:
            continue
        seen.add(clean)
        notes.append(clean)
    text = "；".join(notes) if notes else "—"
    return text if len(text) <= limit else text[: limit - 1] + "…（全量见「口径与校验」）"


def _kpi_spans(band: int, cards: int) -> List[tuple]:
    """把 KPI 卡片均分到前 ``band`` 列上，余数并入最后一张卡片。"""
    if cards <= 0 or band <= 0:
        return []
    cards = min(cards, band)
    per = max(1, round(band / cards))
    spans: List[tuple] = []
    start = 0
    for idx in range(cards):
        end = band - 1 if idx == cards - 1 else start + per - 1
        spans.append((start, end))
        start = end + 1
    return spans


# ------------------------------------------------------------------ 汇总 -----
def write_summary_sheet(ws: Worksheet, results: List[DriverResult], summary: Dict[str, Any], cfg: Config) -> None:
    columns = cfg.get("columns", [])
    ncols = len(columns)
    last_col = get_column_letter(ncols)
    ctx = {
        **cfg.current_period,
        "prev_year": cfg.previous_period["year"],
        "prev_month": cfg.previous_period["month"],
        "unit": cfg.unit,
        "generated_at": summary.get("generated_at", ""),
    }

    # 标题与副标题
    ws.merge_cells(f"A1:{last_col}1")
    title_cell = ws["A1"]
    title_cell.value = cfg.title
    title_cell.font = TITLE_FONT
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 32

    ws.merge_cells(f"A2:{last_col}2")
    sub = ws["A2"]
    sub.value = cfg.msg("subtitle", **ctx)
    sub.font = SUBTITLE_FONT
    sub.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[2].height = 22

    # KPI 卡片（默认只用前 11 列，避免被最宽的说明列撑变形）
    kpis = cfg.get("kpi", [])
    if kpis:
        band = min(ncols, int(cfg.get("render.kpi_band_columns", ncols)))
        spans = _kpi_spans(band, len(kpis))
        for kpi, (start_idx, end_idx) in zip(kpis, spans):
            start = start_idx + 1
            end = end_idx + 1
            col_letter = get_column_letter(start)
            end_letter = get_column_letter(end)
            if start != end:
                ws.merge_cells(f"{col_letter}4:{end_letter}4")
                ws.merge_cells(f"{col_letter}5:{end_letter}5")
            label = ws[f"{col_letter}4"]
            label.value = str(kpi["title"]).format(**ctx)
            label.font = Font(name=FONT_NAME, size=9, color="595959")
            label.alignment = Alignment(horizontal="center", vertical="center")
            label.fill = ACCENT_FILL
            label.border = BORDER

            value_cell = ws[f"{col_letter}5"]
            raw = summary.get(kpi["key"])
            value_cell.value = _format_value(raw, kpi.get("format"), cfg)
            value_cell.alignment = Alignment(horizontal="center", vertical="center")
            value_cell.border = BORDER
            if kpi.get("highlight"):
                value_cell.font = Font(
                    name=FONT_NAME,
                    size=18,
                    bold=True,
                    color=_signed_color(raw, cfg),
                )
                value_cell.fill = _fill(cfg.highlight.get("fill", "FFF2CC"))
            else:
                value_cell.font = Font(name=FONT_NAME, size=16, bold=True, color="1F3864")
                value_cell.fill = _fill("FFFFFF")
        ws.row_dimensions[4].height = 20
        ws.row_dimensions[5].height = 30

    # 环比结论横幅
    banner_row = 7
    ws.merge_cells(f"A{banner_row}:{last_col}{banner_row}")
    banner = ws[f"A{banner_row}"]
    banner.value = _banner_text(summary, cfg, ctx)
    banner.font = Font(name=FONT_NAME, size=12, bold=True, color=_signed_color(summary.get("delta"), cfg))
    banner.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
    banner.fill = _fill(cfg.highlight.get("banner_fill", "FCE4D6"))
    ws.row_dimensions[banner_row].height = 30

    # 表头
    header_row = 9
    for idx, col in enumerate(columns, start=1):
        cell = ws.cell(row=header_row, column=idx)
        cell.value = str(col["title"]).format(**ctx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[header_row].height = 30

    # 数据行
    first_data = header_row + 1
    for offset, result in enumerate(results):
        row = first_data + offset
        values = _row_values(offset + 1, result, cfg)
        for idx, col in enumerate(columns, start=1):
            cell = ws.cell(row=row, column=idx)
            key = col["key"]
            value = values.get(key)
            cell.border = BORDER
            cell.font = BOLD_FONT if col.get("bold") else BODY_FONT
            if key == "note":
                cell.font = NOTE_FONT
                cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                cell.value = value
            elif col.get("format") in ("money", "percent", "int"):
                cell.value = value if isinstance(value, (int, float)) else ("—" if value is None else value)
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if isinstance(value, (int, float)):
                    cell.number_format = cfg.number_formats.get(col["format"], "General")
            else:
                cell.value = value
                cell.alignment = Alignment(
                    horizontal=col.get("align", "left"), vertical="center", wrap_text=bool(col.get("wrap"))
                )
            if col.get("highlight") and key in ("delta", "ratio", "trend"):
                # 不可比司机不用涨跌色，避免把"不完整月份"的差额读成经营变化
                color = _signed_color(value, cfg) if result.comparable else cfg.highlight.get("flat_color", "808080")
                cell.font = Font(name=FONT_NAME, size=10, bold=True, color=color)
                cell.fill = _fill(cfg.highlight.get("fill", "FFF2CC"))
        note_len = len(str(values.get("note", "")))
        ws.row_dimensions[row].height = max(24, min(132, 15 * max(1, -(-note_len // 28))))

    last_data = first_data + len(results) - 1

    # 合计行
    total_row = last_data + 1
    values = _total_values(summary, cfg)
    for idx, col in enumerate(columns, start=1):
        key = col["key"]
        cell = ws.cell(row=total_row, column=idx)
        cell.fill = TOTAL_FILL
        cell.border = BORDER
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color="1F3864")
        if key == "index":
            cell.value = "合计"
            cell.alignment = Alignment(horizontal="center", vertical="center")
        elif key == "driver":
            cell.value = f"{len(results)} 位司机"
            cell.alignment = Alignment(horizontal="left", vertical="center")
        elif key in values:
            value = values[key]
            if isinstance(value, (int, float)):
                cell.value = value
                cell.number_format = cfg.number_formats.get(col.get("format", "money"), "General")
            else:
                cell.value = value if key == "trend" else "—"
            cell.alignment = Alignment(horizontal="right", vertical="center")
        else:
            cell.value = "—" if key != "note" else "见上方逐人说明"
            cell.alignment = Alignment(horizontal="left", vertical="center")
        if key in ("delta", "ratio"):
            cell.font = Font(name=FONT_NAME, size=10, bold=True, color=_signed_color(cell.value, cfg))

    # 图例
    legend_row = total_row + 2
    ws.merge_cells(f"A{legend_row}:{last_col}{legend_row}")
    legend = ws[f"A{legend_row}"]
    legend.value = (
        "读表说明：涨红跌绿（红=环比上升，绿=环比下降）；“—”表示该项无法从源表确认，原因见右侧说明列；"
        "“其他支出合计”不含工资，工资单列，避免重复计算。"
    )
    legend.font = Font(name=FONT_NAME, size=9, color="808080")
    legend.alignment = Alignment(horizontal="left", vertical="center")

    # 列宽 / 冻结 / 筛选
    for idx, col in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = col.get("width", 14)
    ws.freeze_panes = ws.cell(row=first_data, column=3)
    ws.auto_filter.ref = f"A{header_row}:{last_col}{last_data}"

    # 环比比例数据条，强化视觉对比
    ratio_col = next((i for i, c in enumerate(columns, start=1) if c.get("databar")), None)
    if ratio_col and results:
        letter = get_column_letter(ratio_col)
        ws.conditional_formatting.add(
            f"{letter}{first_data}:{letter}{last_data}",
            DataBarRule(start_type="num", start_value=-1, end_type="num", end_value=1, color="F4B183", showValue=True),
        )


def _banner_text(summary: Dict[str, Any], cfg: Config, ctx: Dict[str, Any]) -> str:
    if not summary.get("has_previous") or summary.get("delta") is None:
        return cfg.msg("banner_no_prev").format(**ctx)
    delta = summary["delta"]
    word = cfg.messages.get("up" if delta > 0 else "down" if delta < 0 else "flat", "")
    ratio_value = summary.get("ratio")
    ratio_text = f"{ratio_value * 100:+.2f}%" if ratio_value is not None else "比例不适用"
    return cfg.msg("banner").format(
        **ctx,
        cur=cfg.money(summary.get("net_current")),
        prev=cfg.money(summary.get("net_previous")),
        trend_word=word,
        abs_delta=cfg.money(abs(delta)),
        ratio=ratio_text,
        comparable=summary.get("comparable", 0),
        up=summary.get("up", 0),
        down=summary.get("down", 0),
        incomparable=summary.get("incomparable", 0),
    )


def _signed_color(value: Any, cfg: Config) -> str:
    if not isinstance(value, (int, float)):
        return cfg.highlight.get("flat_color", "808080")
    if value > 0:
        return cfg.highlight.get("increase_color", "C00000")
    if value < 0:
        return cfg.highlight.get("decrease_color", "008000")
    return cfg.highlight.get("flat_color", "808080")


def _format_value(value: Any, fmt: Optional[str], cfg: Config) -> str:
    if value is None:
        return "—"
    if fmt == "money":
        return cfg.money(float(value))
    if fmt == "percent":
        return f"{float(value) * 100:+.2f}%"
    if fmt == "int":
        return f"{int(value)}"
    return str(value)


def _row_values(index: int, result: DriverResult, cfg: Config) -> Dict[str, Any]:
    current, previous = result.current, result.previous
    return {
        "index": index,
        "driver": result.name,
        "income": current.role_totals.get("income"),
        "wage": current.role_totals.get("wage"),
        "expense": current.role_totals.get("expense"),
        "net_profit": current.net_profit,
        "prev_net_profit": previous.net_profit,
        "delta": result.delta,
        "ratio": result.ratio,
        "trend": _trend_text(result.trend, cfg) if (result.comparable and result.delta is not None) else "—",
        "status": result.status,
        "note": _note_text(result, cfg),
    }


def _total_values(summary: Dict[str, Any], cfg: Config) -> Dict[str, Any]:
    return {
        "income": summary.get("income_total"),
        "wage": summary.get("wage_total"),
        "expense": summary.get("expense_total"),
        "net_profit": summary.get("net_current"),
        "prev_net_profit": summary.get("net_previous"),
        "delta": summary.get("delta"),
        "ratio": summary.get("ratio"),
        "trend": _trend_text(
            "up" if (summary.get("delta") or 0) > 0 else "down" if (summary.get("delta") or 0) < 0 else "flat",
            cfg,
        ),
        "status": "—",
    }


# ------------------------------------------------------------------ 明细 -----
def write_detail_sheet(ws: Worksheet, results: List[DriverResult], cfg: Config) -> None:
    headers = ["月份", "司机", "类别", "项目", "金额", "取数方式", "来源文件", "工作表", "单元格/来源", "原始标签", "状态 / 说明"]
    widths = [10, 11, 10, 14, 14, 12, 20, 12, 16, 14, 46]
    for idx, (head, width) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=idx)
        cell.value = head
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[1].height = 24

    row = 2
    for result in results:
        for month_result in (result.current, result.previous):
            if month_result.sheet is None:
                continue
            ordered = sorted(
                month_result.items.values(),
                key=lambda item: (cfg.categories.get(item.category, {}).get("order", 99), item.key),
            )
            for item in ordered:
                if item.value is None:
                    continue
                status = item.status or ("已计入" if not item.excluded else "不重复计入")
                if item.strategy == "derive":
                    status = "倒算值（非独立校验）"
                cell_values = [
                    month_result.period_text,
                    result.name,
                    item.category,
                    item.name,
                    item.value,
                    cfg.strategy_title(item.strategy),
                    result.path.name,
                    month_result.sheet,
                    item.cell or "—",
                    item.label or item.header or "—",
                    "；".join(x for x in [status, item.note] if x),
                ]
                for idx, value in enumerate(cell_values, start=1):
                    cell = ws.cell(row=row, column=idx)
                    cell.value = value
                    cell.border = BORDER
                    cell.font = BODY_FONT
                    if idx == 5:
                        cell.number_format = cfg.number_formats.get("money", "#,##0.00")
                        cell.alignment = Alignment(horizontal="right", vertical="center")
                    elif idx == 11:
                        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                        cell.font = NOTE_FONT
                    else:
                        cell.alignment = Alignment(horizontal="center", vertical="center")
                ws.row_dimensions[row].height = 26 if len(str(cell_values[-1])) > 30 else 18
                row += 1

    ws.freeze_panes = "A2"
    if row > 2:
        ws.auto_filter.ref = f"A1:K{row - 1}"


# -------------------------------------------------------------- 口径信息 -----
def write_spec_sheet(
    ws: Worksheet,
    results: List[DriverResult],
    summary: Dict[str, Any],
    cfg: Config,
    cleanup_lines: List[str],
) -> None:
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 42
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 40

    row = 1

    def section(title: str) -> None:
        nonlocal row
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        cell = ws.cell(row=row, column=1)
        cell.value = title
        cell.font = Font(name=FONT_NAME, size=12, bold=True, color="FFFFFF")
        cell.fill = SECTION_FILL
        cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[row].height = 24
        row += 1

    def kv(pairs: List[tuple]) -> None:
        nonlocal row
        for key, value in pairs:
            ws.cell(row=row, column=1, value=key).font = BOLD_FONT
            ws.cell(row=row, column=1).alignment = Alignment(horizontal="left", vertical="center")
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
            cell = ws.cell(row=row, column=2, value=value)
            cell.font = BODY_FONT
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            for col in range(1, 5):
                ws.cell(row=row, column=col).border = BORDER
            ws.row_dimensions[row].height = 20
            row += 1

    section("一、报表信息")
    kv([
        ("报表名称", cfg.title),
        ("当前期间", f"{cfg.current_period['year']}年{cfg.current_period['month']}月（{cfg.sheet_name(cfg.current_period)}）"),
        ("环比期间", f"{cfg.previous_period['year']}年{cfg.previous_period['month']}月（{cfg.sheet_name(cfg.previous_period)}）——仅与此一个月比较"),
        ("数据源目录", str(cfg.driver_dir)),
        ("源文件模式", "、".join(cfg.get("source.file_patterns", []))),
        ("统计司机数", f"{len(results)} 人"),
        ("生成时间", summary.get("generated_at", "")),
        ("配置文件", str(cfg.source_path)),
    ])

    row += 1
    section("二、计算口径（全部来自配置）")
    variables = cfg.get("calculation.variables", {})
    kv([
        ("净利润公式", cfg.get("calculation.net_profit_expression", "")),
        ("变量映射", "；".join(f"{var} = {role}" for var, role in variables.items())),
        ("环比增减额公式", cfg.get("mom.delta_expression", "")),
        ("环比增减比例公式", cfg.get("mom.ratio_expression", "")),
        ("校验差异容差", f"{cfg.get('calculation.check_tolerance', 0.01)}"),
        ("取数优先级（默认）", " → ".join(cfg.strategy_title(s) for s in cfg.get("resolution.default_order", []))),
        ("重叠科目判定", f"依据 {cfg.get('overlap.decide_by', '')}；无法判定时 {cfg.get('overlap.fallback', 'exclude')}"),
        ("可比性阈值", f"{cfg.previous_period['month']}月参照项 / {cfg.current_period['month']}月参照项 ≥ {cfg.get('comparability.min_ratio', 0.6)}"),
    ])

    row += 1
    section("三、项目识别规则（关键词动态匹配，不写死项目名）")
    head = ["项目 key", "名称", "类别 / 角色", "识别别名（依次优先）"]
    for idx, text in enumerate(head, start=1):
        cell = ws.cell(row=row, column=idx, value=text)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
    row += 1
    for item in cfg.all_items:
        role = cfg.category_role(item["category"])
        name = item.get("name", "") + ("（未启用）" if item.get("disabled") else "")
        values = [
            item["key"],
            name,
            f"{item['category']} / {role}",
            "、".join(item.get("aliases", [])),
        ]
        for idx, text in enumerate(values, start=1):
            cell = ws.cell(row=row, column=idx, value=text)
            cell.font = BODY_FONT
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            cell.border = BORDER
        ws.row_dimensions[row].height = 20
        row += 1

    row += 1
    section("四、解析参数")
    amount = cfg.get("scan.amount", {})
    kv([
        ("金额搜索方向 / 最大偏移", f"{amount.get('direction')} / {amount.get('max_offset')} 列"),
        ("遇到已知标签即停止", str(amount.get("stop_at_known_label"))),
        ("金额清洗字符", " ".join(amount.get("strip_chars", []))),
        ("标签最大长度", str(cfg.get("scan.label.max_len"))),
        ("明细表头行", str(cfg.get("detail.header_rows"))),
        ("合计行关键词", "、".join(cfg.get("detail.total_row_keywords", []))),
        ("无合计行时按明细求和", str(cfg.get("detail.fallback_to_sum"))),
        ("取数冲突容差", f"{cfg.get('resolution.agreement_tolerance_pct')}"),
    ])

    row += 1
    section("五、重生成前的旧报表清理（避免重复数据）")
    if cleanup_lines:
        for line in cleanup_lines:
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            cell = ws.cell(row=row, column=1, value=line)
            cell.font = BODY_FONT
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            row += 1
    else:
        kv([("清理记录", "无")])

    row += 1
    section("六、异常 / 缺失清单（未能确认的均已标注原因，未臆造数值）")
    head = ["司机", "月份", "类别", "说明"]
    for idx, text in enumerate(head, start=1):
        cell = ws.cell(row=row, column=idx, value=text)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
    row += 1
    has_issue = False
    for result in results:
        for month_result in (result.current, result.previous):
            for note in month_result.notes:
                has_issue = True
                values = [result.name, month_result.period_text, "备注", note]
                for idx, text in enumerate(values, start=1):
                    cell = ws.cell(row=row, column=idx, value=text)
                    cell.font = BODY_FONT
                    cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                    cell.border = BORDER
                ws.row_dimensions[row].height = 30 if len(str(note)) > 40 else 18
                row += 1
        if result.incomparable_reason:
            has_issue = True
            values = [result.name, summary.get("current_period_text", str(cfg.current_period["month"])), "环比", result.incomparable_reason]
            for idx, text in enumerate(values, start=1):
                cell = ws.cell(row=row, column=idx, value=text)
                cell.font = BODY_FONT
                cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                cell.border = BORDER
            row += 1
    if not has_issue:
        kv([("异常", "无")])

    row += 1
    section("七、汇总口径")
    kv([
        ("净利润合计（当前月）", cfg.money(summary.get("net_current"))),
        ("净利润合计（上月）", cfg.money(summary.get("net_previous"))),
        ("环比增减额", cfg.money(summary.get("delta"))),
        ("环比增减比例", f"{summary['ratio'] * 100:+.2f}%" if summary.get("ratio") is not None else "不适用"),
        ("可比司机数", f"{summary.get('comparable', 0)} / {summary.get('drivers', 0)} 人"),
        ("异常 / 缺失条目", f"{summary.get('issues', 0)} 条"),
    ])


def build_workbook(
    results: List[DriverResult],
    summary: Dict[str, Any],
    cfg: Config,
    cleanup_lines: Optional[List[str]] = None,
) -> Workbook:
    wb = Workbook()
    wb.properties.creator = cfg.get("report.generated_by", "driver-month-report")
    wb.properties.title = cfg.title
    wb.properties.description = (
        f"{cfg.title}（环比 {cfg.previous_period['year']}年{cfg.previous_period['month']}月）；"
        "由配置化报表引擎自动生成，取数与计算口径见「口径与校验」页。"
    )

    ws = wb.active
    ws.title = cfg.sheet_title("summary", cfg.current_period)
    write_summary_sheet(ws, results, summary, cfg)
    ws.sheet_view.showGridLines = False

    ws_detail = wb.create_sheet(cfg.sheet_title("detail", cfg.current_period))
    write_detail_sheet(ws_detail, results, cfg)

    ws_spec = wb.create_sheet(cfg.sheet_title("spec", cfg.current_period))
    write_spec_sheet(ws_spec, results, summary, cfg, cleanup_lines or [])
    ws_spec.sheet_view.showGridLines = False

    return wb
