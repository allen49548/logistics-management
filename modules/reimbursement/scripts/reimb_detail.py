#!/usr/bin/env python3
"""统一「报销明细」sheet 写入（2026-11-14 23:53 建表 / 2026-11-15 21:38 精简列 + 按月分节）。

用户原话（建表）：「为了兼容表格数据，你可以在表格内新建一个报销明细。这样就不会有兼容的问题了。
所有的表单统一在表内建一个报销明细。」
用户原话（2026-11-15 21:38 精简）：「报销系统输出的数据字段太多了，只保留核心数据即可，
日期，订单号，类型，金额！每个月放一起」

可见列只有 4 个：日期 / 订单号 / 类型 / 金额(元)。
第 5 列「唯一键」是**隐藏技术列**（WPS/Excel 中不可见），仍然存 message_id：
  reimb_detail.py 靠它做逐行幂等，去掉会让重跑重复追加行 —— 这会直接违反
  「同一笔报销只入账一次」铁律。台账 sqlite 仍保留全部字段，审计能力不损失。

「每个月放一起」= 同一个「报销明细」sheet 内按凭证月份升序分节：
  「2026年11月（共 32 笔 ¥1737.06）」标题行 + 该月逐笔明细，末尾一行总计。
  不新建月份 sheet（唯一报销明细 sheet 的规则不变），不新建行到原有月份 sheet。

为什么要 zip 层精确插入，而不是 openpyxl 整体 save：
  司机工资表是 WPS 生成的 xlsx，含 customXml/、tables/、comments、vmlDrawing、
  dbsheet 命名空间等 openpyxl 不认识或不保真的部件。整体重写会丢特性、改公式引用。
  本脚本只改 4 个清单类部件并新增 1 个 worksheet 部件，其余所有部件字节原样复制。

用法：
  python3 scripts/reimb_detail.py apply --data <data.json>
  python3 scripts/reimb_detail.py inspect --file <xlsx>

data.json:
{
  "run_id": "2026-11-15_2140",
  "year": 2026,
  "backup_dir": "state/backups/2026-11-15_2140",
  "precreate_all": true,          // 给该年份目录下所有司机报表预建/迁移「报销明细」
  "entries": [                    // 本轮新增入账条目，按 driver 分组追加
    {"driver":"王博成","key":"om_xxx","receipt_date":"2026-11-11",
     "pay_time":"2026-11-11 12:49:53","amount":45.14,"order_no":"...",
     "category":"充电","merchant":"...","image_file":"...","message_time":"...",
     "entry_id":"30","task_url":"...","note":""}
  ]
}

旧 13 列布局（唯一键/司机/消费日期/支付时间/金额/订单号/费用类别/…）由 apply
自动识别并整表迁移为 4 列 + 隐藏键，无需单独迁移命令；迁移同样先备份、后校验、失败回滚。

输出 JSON：{"ok", "backup_dir", "files":[{file, action, rows_added, total_rows,
            total_amount, verify}], "errors":[]}
"""
import argparse
import json
import os
import re
import shutil
import sys
import zipfile
from xml.sax.saxutils import escape as xml_escape, unescape as xml_unescape

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底

# —— 下列「默认布局」与改造前逐字一致；运行时由 config 覆盖（见 _layout）。
# 4 个可见字段 + 1 个隐藏技术列（幂等键，绝不参与展示）
_DEFAULT_HEADERS = ["日期", "订单号", "类型", "金额(元)", "唯一键"]
_DEFAULT_WIDTHS = [14, 32, 18, 12, 0]
_DEFAULT_HIDDEN = {4}             # E 列隐藏
_DEFAULT_AMOUNT_COL = 3           # D 列 = 金额
_DEFAULT_KEY_COL = 4              # E 列 = 隐藏唯一键
_DEFAULT_DATE_COL = 0             # A 列 = 日期
_DEFAULT_ORDER_COL = 1            # B 列 = 订单号
_DEFAULT_TYPE_COL = 2             # C 列 = 类型
_DEFAULT_SHEET_NAME = "报销明细"
# 2026-11-16 18:55 用户新口径：sheet 名统一「11月报销明细」；旧名识别后重命名不新建。
_DEFAULT_STYLE = {"font_name": "苹方-简", "font_size": 12,
                  "horizontal": "center", "vertical": "center"}
_DEFAULT_LEGACY_NAMES = ["报销明细"]
_DEFAULT_NO_MONTH = "未标月份"
_DEFAULT_DATE_PRIORITY = ("receipt_date", "pay_time", "message_time", "created_at")
# 兼容旧代码/外部 import：默认常量仍存在，值为原硬编码
SHEET_NAME = _DEFAULT_SHEET_NAME
STYLE = dict(_DEFAULT_STYLE)              # 单元格样式（字体/字号/对齐）
LEGACY_SHEET_NAMES = list(_DEFAULT_LEGACY_NAMES)  # 迁移时识别的旧 sheet 名
HEADERS = _DEFAULT_HEADERS
WIDTHS = _DEFAULT_WIDTHS
HIDDEN_COLS = _DEFAULT_HIDDEN
AMOUNT_COL = _DEFAULT_AMOUNT_COL
KEY_COL = _DEFAULT_KEY_COL
DATE_COL = _DEFAULT_DATE_COL
ORDER_COL = _DEFAULT_ORDER_COL
TYPE_COL = _DEFAULT_TYPE_COL
SKIP_KEYS = {"", "唯一键", "合计"}
NO_MONTH = _DEFAULT_NO_MONTH


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _layout(cfg):
    """按 config.report_detail_sheet 计算布局；缺项回退默认值，保证输出不变。"""
    rds = cfg.get("report_detail_sheet") or {}
    cols = rds.get("columns") or {}
    visible = list(cols.get("visible") or _DEFAULT_HEADERS[:4])
    hidden = list(cols.get("hidden_technical") or _DEFAULT_HEADERS[4:])
    headers = visible + hidden
    # 宽度：与列一一对应；配置可给 widths，否则按默认列位置回填
    widths = list(cols.get("widths") or _DEFAULT_WIDTHS[:len(headers)])
    if len(widths) < len(headers):
        widths += _DEFAULT_WIDTHS[len(widths):len(headers)]
    hidden_idx = set(range(len(visible), len(headers)))
    # 列位置按表头名解析，找不到则回退默认位置
    def idx(name, fallback):
        return headers.index(name) if name in headers else fallback
    return {
        "sheet_name": rds.get("sheet_name") or rc_get(cfg, "sheet_name", _DEFAULT_SHEET_NAME),
        "headers": headers,
        "widths": widths,
        "hidden_cols": hidden_idx,
        "amount_col": idx("金额(元)", _DEFAULT_AMOUNT_COL),
        "key_col": idx("唯一键", _DEFAULT_KEY_COL),
        "date_col": idx("日期", _DEFAULT_DATE_COL),
        "order_col": idx("订单号", _DEFAULT_ORDER_COL),
        "type_col": idx("类型", _DEFAULT_TYPE_COL),
        "date_priority": tuple(cols.get("date_source_priority") or _DEFAULT_DATE_PRIORITY),
        # 未标月份哨兵取 runtime 可配置项（默认「未标月份」，与原硬编码一致）。
        # 注意：report_detail_sheet.columns.undated_group 是带括号的注释文案，不能当数据键。
        "undated_group": rc_get(cfg, "undated_group", _DEFAULT_NO_MONTH),
        "month_label": cols.get("month_label") or "YYYY年M月（共 N 笔 ¥X）",
        "skip_keys": {"", (headers[-1] if headers else "唯一键"), "合计"},
        # 2026-11-16：样式与旧 sheet 名迁移列表
        "style": {**_DEFAULT_STYLE, **(rds.get("style") or {})},
        "legacy_sheet_names": list(rds.get("legacy_sheet_names") or _DEFAULT_LEGACY_NAMES),
    }


_DATE_PRIORITY = _DEFAULT_DATE_PRIORITY


def apply_layout_config(cfg):
    """用 config 值刷新模块级布局常量；内部函数无需逐个传参。
    在任何命令入口调用一次；缺省值与改造前完全相同。
    """
    global SHEET_NAME, HEADERS, WIDTHS, HIDDEN_COLS, AMOUNT_COL, KEY_COL
    global DATE_COL, ORDER_COL, TYPE_COL, SKIP_KEYS, NO_MONTH, _DATE_PRIORITY
    global STYLE, LEGACY_SHEET_NAMES
    lay = _layout(cfg)
    SHEET_NAME = lay["sheet_name"]
    HEADERS = lay["headers"]
    WIDTHS = lay["widths"]
    HIDDEN_COLS = lay["hidden_cols"]
    AMOUNT_COL = lay["amount_col"]
    KEY_COL = lay["key_col"]
    DATE_COL = lay["date_col"]
    ORDER_COL = lay["order_col"]
    TYPE_COL = lay["type_col"]
    SKIP_KEYS = lay["skip_keys"]
    NO_MONTH = lay["undated_group"]
    _DATE_PRIORITY = lay["date_priority"]
    STYLE = lay["style"]
    LEGACY_SHEET_NAMES = lay["legacy_sheet_names"]


def col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _style_attr(style_id):
    """2026-11-16：单元格样式索引属性（居中/苹方-简/12）。"""
    return ' s="%d"' % style_id if isinstance(style_id, int) else ""


def cell_str(ref, value, style_id=None):
    return '<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
        ref, _style_attr(style_id), xml_escape("" if value is None else str(value)))


def cell_num(ref, value, style_id=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return cell_str(ref, value, style_id)
    # 避免科学计数法与浮点尾巴
    s = ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return '<c r="%s"%s><v>%s</v></c>' % (ref, _style_attr(style_id), s or "0")


def row_xml(r, values, amount_col=AMOUNT_COL, style_id=None):
    cells = []
    for i, v in enumerate(values):
        ref = "%s%d" % (col_letter(i), r)
        if i == amount_col and isinstance(v, (int, float)):
            cells.append(cell_num(ref, v, style_id))
        else:
            cells.append(cell_str(ref, v, style_id))
    return '<row r="%d">%s</row>' % (r, "".join(cells))


def receipt_date(e, priority=None):
    """日期：消费日期 → 支付时间 → 消息时间 → 台账创建时间（优先级可配置）。"""
    for k in (priority or _DATE_PRIORITY):
        v = str(e.get(k) or "").strip()
        if v:
            return v[:10]
    return ""


def entry_values(e):
    """新入账条目 → 4 可见字段 + 隐藏唯一键。"""
    return [
        receipt_date(e),
        e.get("order_no") or "",
        e.get("category") or "",
        e.get("amount") or 0,
        e.get("key") or e.get("message_id") or "",
    ]


def month_key(date_str, no_month=None):
    m = re.match(r"(\d{4})-(\d{2})", str(date_str or ""))
    return "%s-%s" % (m.group(1), m.group(2)) if m else (no_month or NO_MONTH)


def month_label(key, count, amount, lay=None):
    no_month = (lay or {}).get("undated_group", NO_MONTH)
    if key == no_month:
        return "%s（共 %d 笔 ¥%.2f）" % (no_month, count, amount)
    y, m = key.split("-")
    tpl = (lay or {}).get("month_label", "YYYY年M月（共 N 笔 ¥X）")
    return (tpl.replace("YYYY", y).replace("M", str(int(m)))
               .replace("N", str(count)).replace("X", "%.2f" % amount))


def build_display_rows(records, lay=None):
    """把结构化明细按月份分节展开为 sheet 行（不含表头/合计由调用方加）。"""
    lay = lay or {}
    no_month = lay.get("undated_group", NO_MONTH)
    groups = {}
    for rec in records:
        groups.setdefault(month_key(rec["date"], no_month), []).append(rec)
    # 未标月份永远排最后；组内按日期升序，同日保持原有顺序（稳定排序，保证幂等）
    ordered = sorted(groups, key=lambda k: (k == no_month, k))
    out = []
    ncol = len(lay.get("headers", HEADERS))
    for k in ordered:
        items = sorted(groups[k], key=lambda x: (x["date"] == "", x["date"]))
        sub = round(sum(float(x["amount"] or 0) for x in items), 2)
        out.append([month_label(k, len(items), sub, lay)] + [""] * (ncol - 1))
        for rec in items:
            out.append([rec["date"], rec["order_no"], rec["category"],
                        rec["amount"], rec["key"]])
    return out


def sheet_xml(rows, lay=None, style_id=None):
    """rows: list of (row_number, values)。E 列隐藏，只展示 4 个核心字段。"""
    lay = lay or {}
    headers = lay.get("headers", HEADERS)
    widths = lay.get("widths", WIDTHS)
    hidden_cols = lay.get("hidden_cols", HIDDEN_COLS)
    col_defs = []
    for i, w in enumerate(widths):
        if i in hidden_cols:
            col_defs.append('<col min="%d" max="%d" width="0" hidden="1" customWidth="1"/>'
                            % (i + 1, i + 1))
        else:
            col_defs.append('<col min="%d" max="%d" width="%d" customWidth="1"/>'
                            % (i + 1, i + 1, w))
    cols = "".join(col_defs)
    body = "".join(row_xml(r, v, amount_col=lay.get("amount_col", AMOUNT_COL),
                           style_id=style_id) for r, v in rows)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="%s"><dimension ref="A1:%s%d"/>'
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
            '</sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>'
            '<cols>%s</cols><sheetData>%s</sheetData>'
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
            '</worksheet>') % (NS_MAIN, col_letter(len(headers) - 1), max(1, len(rows)), cols, body)


def total_row(amount, lay=None):
    lay = lay or {}
    key_col = lay.get("key_col", KEY_COL)
    amount_col = lay.get("amount_col", AMOUNT_COL)
    out = [""] * len(lay.get("headers", HEADERS))
    out[0] = "合计"
    out[amount_col] = round(float(amount), 2)
    if key_col < len(out):
        out[key_col] = ""
    return out


def read_parts(path):
    with zipfile.ZipFile(path) as z:
        return [(i, z.read(i.filename)) for i in z.infolist()]


def find_detail_sheet(parts):
    """返回 (sheet_xml_part_name, rel_id) 或 (None, None)。

    2026-11-16：sheet 名由「报销明细」统一为「11月报销明细」。为兼容历史报表，
    按 SHEET_NAME（新名）→ LEGACY_SHEET_NAMES（旧名）顺序查找；命中旧名时同样返回，
    apply_one 会就地重命名该 sheet（不新建、不丢数据）。
    """
    wb = next((d.decode("utf-8") for i, d in parts if i.filename == "xl/workbook.xml"), "")
    names = [SHEET_NAME] + [n for n in LEGACY_SHEET_NAMES if n != SHEET_NAME]
    tag = None
    for nm in names:
        m = re.search(r'<sheet[^>]*name="%s"[^>]*/>' % re.escape(nm), wb)
        if m:
            tag = m.group(0)
            break
    if not tag:
        return None, None
    rid = re.search(r'r:id="([^"]+)"', tag)
    rels = next((d.decode("utf-8") for i, d in parts
                 if i.filename == "xl/_rels/workbook.xml.rels"), "")
    if not rid:
        return None, None
    r = re.search(r'<Relationship[^>]*Id="%s"[^>]*Target="([^"]+)"' % re.escape(rid.group(1)), rels)
    if not r:
        return None, None
    target = r.group(1)
    part = "xl/" + target.lstrip("/") if not target.startswith("/") else target.lstrip("/")
    return part, rid.group(1)


def has_detail_sheet(path, lay=None):
    """轻量判断某报表是否已有「报销明细」sheet（只解压 workbook.xml，不读整个 zip、不碰 openpyxl）。

    只匹配**新名**（sheet_name，如「11月报销明细」）；旧名 sheet 不算命中，这样
    precreate_all 仍会走 apply_one 做旧名迁移（不丢历史数据）。用于：已有新名明细
    sheet 且本轮无该司机新凭证的报表直接跳过，避免每轮对全部司机报表做一次完整
    zip 重写 + openpyxl 回读校验。
    """
    lay = lay or {}
    sheet_name = lay.get("sheet_name", SHEET_NAME)
    try:
        with zipfile.ZipFile(path) as z:
            wb = z.read("xl/workbook.xml").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    return bool(re.search(r'<sheet[^>]*name="%s"[^>]*/>' % re.escape(sheet_name), wb))


def read_shared_strings(parts):
    """WPS/Excel 保存后会把 inlineStr 转成共享字符串，必须解析才能读出唯一键。"""
    raw = next((d for i, d in parts if i.filename == "xl/sharedStrings.xml"), None)
    if raw is None:
        return []
    txt = raw.decode("utf-8")
    out = []
    for si in re.findall(r"<si\b[^>]*>(.*?)</si>", txt, re.S):
        out.append(xml_unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
    return out


def parse_cells(raw_row, shared=None):
    """解析一行内各单元格的值，返回 {列字母: 文本值}。

    兼容三种写法：inlineStr（脚本自写）、t="s" 共享字符串（WPS/Excel 保存后）、纯数值。
    """
    shared = shared or []
    out = {}
    for cm in re.finditer(r'<c r="([A-Z]+)\d+"([^>]*)>(.*?)</c>', raw_row, re.S):
        col, attrs, inner = cm.group(1), cm.group(2), cm.group(3)
        t = re.search(r'<t[^>]*>(.*?)</t>', inner, re.S)
        v = re.search(r"<v>(.*?)</v>", inner, re.S)
        if 't="s"' in attrs and v:
            try:
                idx = int(v.group(1))
                s = shared[idx] if 0 <= idx < len(shared) else ""
            except ValueError:
                s = ""
        else:
            s = t.group(1) if t else (v.group(1) if v else "")
        out[col] = xml_unescape(s)
    return out


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def parse_existing_records(xml_text, shared=None):
    """把已有明细 sheet 解析成结构化记录，兼容旧 13 列布局并自动迁移。

    返回 (records, layout, month_group_count)。
      records: [{date, order_no, category, amount, key}, ...]
      layout:  "v2" = 4 列 + 隐藏键（当前）；"v1" = 旧 13 列；"empty" = 空表
    """
    m = re.search(r"<sheetData>(.*?)</sheetData>", xml_text or "", re.S)
    if not m:
        return [], "empty", 0
    raw_rows = re.findall(r"<row\b[^>]*>.*?</row>", m.group(1), re.S)
    if not raw_rows:
        return [], "empty", 0
    first = parse_cells(raw_rows[0], shared)
    head_a = (first.get("A") or "").strip()
    layout = "v1" if head_a == "唯一键" else ("v2" if head_a == "日期" else "empty")
    if layout == "empty":
        return [], "empty", 0

    records = []
    month_rows = 0
    for raw in raw_rows[1:]:
        c = parse_cells(raw, shared)
        if layout == "v1":
            key = (c.get("A") or "").strip()
            if key in SKIP_KEYS:
                continue
            date = (c.get("C") or "").strip()[:10] or (c.get("D") or "").strip()[:10] \
                or (c.get("J") or "").strip()[:10]
            rec = {
                "key": key,
                "date": date,
                "order_no": (c.get("F") or "").strip(),
                "category": (c.get("G") or "").strip(),
                "amount": to_float((c.get("E") or "").strip()),
            }
        else:
            key = (c.get("E") or "").strip()
            # 月份分节行与合计行的隐藏键列为空，不是明细
            if key in SKIP_KEYS:
                continue
            rec = {
                "key": key,
                "date": (c.get("A") or "").strip(),
                "order_no": (c.get("B") or "").strip(),
                "category": (c.get("C") or "").strip(),
                "amount": to_float((c.get("D") or "").strip()),
            }
        records.append(rec)
    return records, layout, month_rows


def next_ids(parts):
    wb = next((d.decode("utf-8") for i, d in parts if i.filename == "xl/workbook.xml"), "")
    rels = next((d.decode("utf-8") for i, d in parts
                 if i.filename == "xl/_rels/workbook.xml.rels"), "")
    sheet_ids = [int(x) for x in re.findall(r'sheetId="(\d+)"', wb)]
    rid_nums = [int(x) for x in re.findall(r'Id="rId(\d+)"', rels)]
    existing_parts = {i.filename for i, _ in parts}
    k = max([int(x) for x in re.findall(r"xl/worksheets/sheet(\d+)\.xml", " ".join(existing_parts))] or [0]) + 1
    while "xl/worksheets/sheet%d.xml" % k in existing_parts:
        k += 1
    return max(sheet_ids or [0]) + 1, "rId%d" % (max(rid_nums or [0]) + 1), \
        "xl/worksheets/sheet%d.xml" % k


def patch_workbook(wb, sheet_id, rid):
    tag = '<sheet name="%s" sheetId="%d" r:id="%s"/>' % (SHEET_NAME, sheet_id, rid)
    if "</sheets>" not in wb:
        raise ValueError("workbook.xml 缺少 </sheets>")
    return wb.replace("</sheets>", tag + "</sheets>", 1)


def patch_rels(rels, rid, part):
    target = part[len("xl/"):] if part.startswith("xl/") else part
    tag = ('<Relationship Id="%s" Type="%s/worksheet" Target="%s"/>' % (rid, REL_NS, target))
    if "</Relationships>" not in rels:
        raise ValueError("workbook.xml.rels 缺少 </Relationships>")
    return rels.replace("</Relationships>", tag + "</Relationships>", 1)


def patch_content_types(ct, part):
    tag = ('<Override PartName="/%s" ContentType="application/vnd.openxmlformats-'
           'officedocument.spreadsheetml.worksheet+xml"/>' % part)
    if "</Types>" not in ct:
        raise ValueError("[Content_Types].xml 缺少 </Types>")
    return ct.replace("</Types>", tag + "</Types>", 1)


def patch_app_xml(app):
    """同步工作表数量与名称，避免 Excel/WPS 打开时提示修复。"""
    if not app:
        return app
    try:
        m = re.search(r'(<vt:vector size=")(\d+)(" baseType="lpstr">)', app)
        if not m:
            return app
        app = app[:m.start(2)] + str(int(m.group(2)) + 1) + app[m.end(2):]
        app = re.sub(r'(<vt:variant><vt:lpstr>工作表</vt:lpstr></vt:variant><vt:variant><vt:i4>)(\d+)',
                     lambda x: x.group(1) + str(int(x.group(2)) + 1), app, count=1)
        app = app.replace("</vt:vector></TitlesOfParts>",
                          "<vt:lpstr>%s</vt:lpstr></vt:vector></TitlesOfParts>" % SHEET_NAME, 1)
    except Exception:
        pass
    return app


def _attr(tag, name):
    m = re.search(r'\b%s="([^"]*)"' % re.escape(name), tag)
    return m.group(1) if m else None


def actual_sheet_name(parts, detail_part):
    """由 detail worksheet 部件反查其在 workbook.xml 中的当前显示名。"""
    wb = next((d.decode("utf-8") for i, d in parts if i.filename == "xl/workbook.xml"), "")
    rels = next((d.decode("utf-8") for i, d in parts
                 if i.filename == "xl/_rels/workbook.xml.rels"), "")
    suffix = detail_part[len("xl/"):] if detail_part.startswith("xl/") else detail_part
    for rel in re.finditer(r'<Relationship\b[^>]*/>', rels):
        tag = rel.group(0)
        target = _attr(tag, "Target") or ""
        norm = ("xl/" + target.lstrip("/")) if not target.startswith("/") else target.lstrip("/")
        if norm == detail_part or norm.endswith("/" + suffix) or target == suffix:
            rid = _attr(tag, "Id")
            if not rid:
                continue
            sm = re.search(r'<sheet\b[^>]*r:id="%s"[^>]*/>' % re.escape(rid), wb)
            if sm:
                return _attr(sm.group(0), "name")
    return None


def rename_sheet_in_workbook(wb, old, new):
    """就地改 workbook.xml 中某 sheet 的 name（只改 name 属性，其余不动）。"""
    m = re.search(r'<sheet\b[^>]*name="%s"[^>]*/>' % re.escape(old), wb)
    if not m:
        return wb
    tag = re.sub(r'name="%s"' % re.escape(old), 'name="%s"' % new, m.group(0), count=1)
    return wb[:m.start()] + tag + wb[m.end():]


def rename_sheet_in_app(app, old, new):
    """同步 docProps/app.xml 的 TitlesOfParts 中旧 sheet 名。"""
    if not app or not old:
        return app
    token = "<vt:lpstr>%s</vt:lpstr>" % old
    if token in app:
        return app.replace(token, "<vt:lpstr>%s</vt:lpstr>" % new)
    return app


def _xf_open_count(body):
    """cellXfs 内 <xf 开标签数 = Excel 真实样式索引上限（WPS 的 count 属性常偏小）。"""
    return len(re.findall(r'<xf\b', body))


def ensure_style(parts, style=None):
    """在 styles.xml 里幂等追加「苹方-简/12/居中」的 font+cellXf。

    返回 (new_styles_bytes_or_None, style_index)；已存在则复用索引、返回 None 不改字节
    （保证重复 apply 时 styles.xml 零差异，verify 通过）。styles.xml 缺失则跳过样式。
    索引一律按「开标签顺序」计算：WPS 生成的 count 属性可能与实际标签数不一致，
    若用 count 会导致 s= 指向错误样式（曾致居中/字体失效）。
    """
    st = style or STYLE
    font_name = str(st.get("font_name") or "苹方-简")
    try:
        font_size = float(st.get("font_size") or 12)
    except (TypeError, ValueError):
        font_size = 12.0
    halign = str(st.get("horizontal") or "center")
    valign = str(st.get("vertical") or "center")

    idx = next((i for i, (info, d) in enumerate(parts)
                if info.filename == "xl/styles.xml"), None)
    if idx is None:
        return None, None
    _, data = parts[idx]
    text = data.decode("utf-8")
    changed = False

    # —— fonts ——（索引 = <font 开标签顺序）
    fm = re.search(r'(<fonts\b[^>]*count=")(\d+)("[^>]*>)(.*?)(</fonts>)', text, re.S)
    if not fm:
        return None, None
    font_body = fm.group(4)
    font_tags = re.findall(r'<font\b[^>]*(?:/>|>.*?</font>)', font_body, re.S)
    font_idx = None
    for k, tag in enumerate(font_tags):
        nm = re.search(r'<name\b[^>]*val="([^"]*)"', tag)
        sz = re.search(r'<sz\b[^>]*val="([^"]*)"', tag)
        if nm and sz and nm.group(1) == font_name:
            try:
                if abs(float(sz.group(1)) - font_size) < 0.01:
                    font_idx = k
                    break
            except (TypeError, ValueError):
                pass
    if font_idx is None:
        font_idx = len(re.findall(r'<font\b', font_body))  # 真实索引
        size_txt = ("%g" % font_size)
        new_font = ('<font><sz val="%s"/><color theme="1"/><name val="%s"/>'
                    '<charset val="134"/></font>') % (size_txt, xml_escape(font_name))
        real_count = len(re.findall(r'<font\b', font_body)) + 1
        new_fonts = ('<fonts count="%d">' % real_count) + font_body + new_font + "</fonts>"
        text = text[:fm.start()] + new_fonts + text[fm.end():]
        changed = True

    # —— cellXfs ——（索引 = <xf 开标签顺序）
    fm2 = re.search(r'(<cellXfs\b[^>]*count=")(\d+)("[^>]*>)(.*?)(</cellXfs>)', text, re.S)
    if not fm2:
        return (text.encode("utf-8") if changed else None), None
    xf_body = fm2.group(4)
    xf_tags = re.findall(r'<xf\b[^>]*(?:/>|>.*?</xf>)', xf_body, re.S)
    xf_idx = None
    for k, tag in enumerate(xf_tags):
        fid = _attr(tag, "fontId")
        if fid is None or fid != str(font_idx):
            continue
        hal = re.search(r'\bhorizontal="([^"]*)"', tag)
        val = re.search(r'\bvertical="([^"]*)"', tag)
        def _ok(m, want):
            return (m and m.group(1) == want) or (not m and ('%s="%s"' % (
                "horizontal" if want == halign else "vertical", want)) in tag)
        if _ok(hal, halign) and _ok(val, valign):
            xf_idx = k
            break
    if xf_idx is None:
        xf_idx = len(re.findall(r'<xf\b', xf_body))  # 真实索引
        new_xf = ('<xf numFmtId="0" fontId="%d" fillId="0" borderId="0" xfId="0" '
                  'applyFont="1" applyAlignment="1"><alignment horizontal="%s" '
                  'vertical="%s"/></xf>') % (font_idx, halign, valign)
        real_count = len(re.findall(r'<xf\b', xf_body)) + 1
        new_cellxfs = ('<cellXfs count="%d">' % real_count) + xf_body + new_xf + "</cellXfs>"
        text = text[:fm2.start()] + new_cellxfs + text[fm2.end():]
        changed = True

    return (text.encode("utf-8") if changed else None), xf_idx



def write_zip(path, parts, replacements, additions):
    tmp = path + ".tmp_reimb"
    with zipfile.ZipFile(tmp, "w") as zout:
        for info, data in parts:
            zout.writestr(info, replacements.get(info.filename, data))
        for name, data in additions.items():
            zout.writestr(name, data)
    os.replace(tmp, path)


def verify(path, before_parts, changed_names, expect_rows, expect_amount):
    """回读校验：未改部件字节完全一致 + openpyxl 能打开 + 明细行数/金额一致。

    明细行判定改为「隐藏 E 列（唯一键）非空且非表头」，月份分节行不计入。
    金额统一取 D 列（4 列布局）。
    """
    after = {i.filename: d for i, d in read_parts(path)}
    before = {i.filename: d for i, d in before_parts}
    diffs = [n for n in before if n not in changed_names and before.get(n) != after.get(n)]
    ws_parts = [n for n in changed_names if n.startswith("xl/worksheets/")]
    result = {"unchanged_parts": len(before) - len(changed_names),
              "unexpected_diffs": diffs, "openpyxl_ok": False,
              "detail_rows": 0, "detail_amount": 0.0, "sheet_present": False,
              "hidden_key_column": any(b'hidden="1"' in after.get(n, b"") for n in ws_parts)}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
        result["sheet_present"] = SHEET_NAME in wb.sheetnames
        ws = wb[SHEET_NAME]
        vals = list(ws.values)
        body = []
        for r in vals[1:]:
            if not r:
                continue
            key = str(r[KEY_COL] or "").strip() if len(r) > KEY_COL else ""
            if key in SKIP_KEYS:
                continue
            body.append(r)
        result["detail_rows"] = len(body)
        result["detail_amount"] = round(sum(to_float(r[AMOUNT_COL]) for r in body), 2)
        result["visible_headers"] = [str(x or "") for x in (vals[0] if vals else [])][:4]
        result["openpyxl_ok"] = True
        result["sheet_count"] = len(wb.sheetnames)
        wb.close()
    except Exception as e:
        result["openpyxl_error"] = str(e)
    result["rows_match"] = result["detail_rows"] == expect_rows
    result["amount_match"] = abs(result["detail_amount"] - float(expect_amount)) < 0.005
    result["ok"] = (not diffs and result["openpyxl_ok"] and result["sheet_present"]
                    and result["rows_match"] and result["amount_match"])
    return result


def apply_one(path, entries, backup_dir, lay=None):
    """在单个 xlsx 内建/写/迁移「报销明细」。返回 (result_dict, error_or_None)。"""
    lay = lay or {}
    sheet_name = lay.get("sheet_name", SHEET_NAME)
    key_col = lay.get("key_col", KEY_COL)
    amount_col = lay.get("amount_col", AMOUNT_COL)
    date_priority = lay.get("date_priority", _DEFAULT_DATE_PRIORITY)
    parts = read_parts(path)
    shared = read_shared_strings(parts)
    detail_part, _ = find_detail_sheet(parts)
    created = detail_part is None
    before = {i.filename: d for i, d in parts}
    replacements = {}
    additions = {}
    changed = set()
    renamed_from = None

    if created:
        sheet_id, rid, detail_part = next_ids(parts)
        for name, fn in (("xl/workbook.xml", lambda s: patch_workbook(s, sheet_id, rid)),
                         ("xl/_rels/workbook.xml.rels", lambda s: patch_rels(s, rid, detail_part)),
                         ("[Content_Types].xml", lambda s: patch_content_types(s, detail_part)),
                         ("docProps/app.xml", patch_app_xml)):
            if name in before:
                replacements[name] = fn(before[name].decode("utf-8")).encode("utf-8")
                changed.add(name)
        existing, layout = [], "empty"
        additions[detail_part] = ""
        changed.add(detail_part)
    else:
        existing, layout, _ = parse_existing_records(before[detail_part].decode("utf-8"), shared)
        changed.add(detail_part)
        # 2026-11-16：旧名「报销明细」→ 新名「11月报销明细」，就地重命名（不新建、不丢数据）
        actual = actual_sheet_name(parts, detail_part)
        renamed_from = None
        if actual and actual != sheet_name:
            renamed_from = actual
            if "xl/workbook.xml" in before:
                replacements["xl/workbook.xml"] = rename_sheet_in_workbook(
                    before["xl/workbook.xml"].decode("utf-8"), actual, sheet_name).encode("utf-8")
                changed.add("xl/workbook.xml")
            if "docProps/app.xml" in before:
                replacements["docProps/app.xml"] = rename_sheet_in_app(
                    before["docProps/app.xml"].decode("utf-8"), actual, sheet_name).encode("utf-8")
                changed.add("docProps/app.xml")

    # 2026-11-16：样式（居中/苹方-简/12），幂等注入 styles.xml
    styles_patch, style_id = ensure_style(parts, lay.get("style"))
    if styles_patch is not None:
        replacements["xl/styles.xml"] = styles_patch
        changed.add("xl/styles.xml")

    # 幂等：已有唯一键不再追加
    seen = {x["key"] for x in existing}
    fresh = []
    for e in entries:
        k = e.get("key") or e.get("message_id") or ""
        if k in seen:
            continue
        seen.add(k)
        v = entry_values(e)
        fresh.append({"key": v[4], "date": v[0], "order_no": v[1],
                      "category": v[2], "amount": v[3]})

    records = existing + fresh
    display = build_display_rows(records)
    amount_sum = round(sum(float(x["amount"] or 0) for x in records), 2)

    values = [HEADERS] + display + [total_row(amount_sum)]
    rows = [(i + 1, v) for i, v in enumerate(values)]
    sheet = sheet_xml(rows, lay=lay, style_id=style_id)
    if created:
        additions[detail_part] = sheet.encode("utf-8")
    else:
        replacements[detail_part] = sheet.encode("utf-8")

    # 备份（必须先成功）
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, os.path.basename(path))
    shutil.copy2(path, backup)
    write_zip(path, parts, replacements, additions)

    expect_rows = len(records)
    v = verify(path, parts, changed, expect_rows, amount_sum)
    if not v["ok"]:
        # 校验失败立即回滚
        shutil.copy2(backup, path)
        v["rolled_back"] = True
    return {
        "file": path, "action": "created" if created else ("migrated" if layout == "v1" else "appended"),
        "layout_before": layout, "detail_part": detail_part,
        "rows_added": len(fresh),
        "rows_skipped_duplicate": len(entries) - len(fresh),
        "total_rows": expect_rows, "total_amount": amount_sum,
        "month_sections": sum(1 for x in display if x[1] == "" and x[2] == ""
                              and x[3] == "" and x[4] == ""),
        "renamed_from": renamed_from, "sheet_name": sheet_name,
        "style_index": style_id, "style": lay.get("style"),
        "backup": backup, "verify": v,
    }, (None if v["ok"] else "回读校验失败，已回滚：%s" % json.dumps(v, ensure_ascii=False)[:400])


def cmd_apply(args):
    cfg = load_config()
    apply_layout_config(cfg)
    with open(args.data, "r", encoding="utf-8") as f:
        d = json.load(f)
    run_id = d["run_id"]
    from runtime_config import current_year as rc_year
    year = int(d.get("year") or rc_year(cfg))
    root = os.path.join(cfg["report_root"], "%d年" % year, "司机工资")
    backup_dir = d.get("backup_dir") or os.path.join(cfg["state_dir"], "backups", run_id)
    if not os.path.isabs(backup_dir):
        backup_dir = os.path.join(os.path.dirname(HERE), backup_dir)
    entries = d.get("entries") or []
    by_driver = {}
    for e in entries:
        by_driver.setdefault(e["driver"], []).append(e)

    files = []
    errors = []
    precreate_all = d.get("precreate_all", True)
    if precreate_all:
        import glob
        paths = sorted(glob.glob(os.path.join(root, "*.xlsx")))
    else:
        paths = [os.path.join(root, "%s%d.xlsx" % (drv, year)) for drv in by_driver]
    for p in paths:
        if not os.path.isfile(p):
            errors.append({"file": p, "error": "文件不存在，跳过（绝不新建报表文件）"})
            continue
        drv = os.path.basename(p).replace("%d.xlsx" % year, "")
        # 性能：precreate_all 时，本轮无该司机新凭证且报表已有新名明细 sheet → 跳过。
        # 幂等、无变化；缺 sheet 仍预建、旧名 sheet 仍走迁移、有凭证仍写入，语义不变。
        if precreate_all and not by_driver.get(drv) and has_detail_sheet(p):
            continue
        res, err = apply_one(p, by_driver.get(drv, []), backup_dir)
        files.append(res)
        if err:
            errors.append({"file": p, "error": err})
    return {"ok": not errors, "run_id": run_id, "sheet": SHEET_NAME,
            "visible_columns": HEADERS[:4], "hidden_columns": HEADERS[4:],
            "backup_dir": backup_dir, "files": files, "errors": errors}


def cmd_inspect(args):
    apply_layout_config(load_config())
    parts = read_parts(args.file)
    detail_part, rid = find_detail_sheet(parts)
    out = {"file": args.file, "sheet_exists": bool(detail_part),
           "detail_part": detail_part, "rel_id": rid}
    if detail_part:
        raw = next((d.decode("utf-8") for i, d in parts if i.filename == detail_part), "")
        records, layout, _ = parse_existing_records(raw, read_shared_strings(parts))
        out["layout"] = layout
        out["rows"] = len(records)
        out["amount"] = round(sum(float(x["amount"] or 0) for x in records), 2)
        out["months"] = sorted({month_key(x["date"]) for x in records})
        out["sheets"] = re.findall(r'<sheet[^>]*name="([^"]+)"',
                                   next(d.decode("utf-8") for i, d in parts
                                        if i.filename == "xl/workbook.xml"))
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--data", required=True)
    i = sub.add_parser("inspect")
    i.add_argument("--file", required=True)
    args = ap.parse_args()
    out = cmd_apply(args) if args.cmd == "apply" else cmd_inspect(args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
