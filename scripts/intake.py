"""报单模块：飞书群运输单 -> 结构化明细 -> 按司机分组 -> 数据源，再调用内置月度制表模块写入。

新增能力，仅此一处实现；写入不自己造轮子，调用 modules/driver-monthly-sheet 的 run，
保证与现有司机报表格式一致。解析规则、字段别名、司机归属、去重键全部来自配置。
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from openpyxl import Workbook, load_workbook

from common import (
    ROOT,
    EXIT_BUSINESS,
    EXIT_CONFIG,
    emit,
    expand,
    load_config,
    make_key,
    match_alias,
    normalize_driver,
    read_json,
    run_delegate,
    write_json,
)

SEMANTIC_TO_COLUMN: Dict[str, str] = {}  # 运行时由配置填充


def _intake_cfg() -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    modules, cfg = load_config()
    icfg = cfg.get("intake", {})
    if not icfg.get("fields"):
        raise ValueError("配置缺少 intake.fields（字段别名）")
    return modules, cfg, icfg


def _column_map(icfg: Dict[str, Any]) -> Dict[str, str]:
    """语义键 -> 数据源中文列名。列名本身即字段别名，反查即可。"""
    mapping: Dict[str, str] = {}
    fields = icfg["fields"]
    for col in icfg.get("data_source", {}).get("columns", []):
        key = match_alias(col, fields)
        if key:
            mapping[key] = col
    return mapping


def parse_message_text(text: str, icfg: Dict[str, Any]) -> Dict[str, Any]:
    """从一条群文本消息里抽字段。

    支持两种常见写法（均配置化识别）：
      1) "字段: 值" / "字段：值" 多行或分号分隔；
      2) 以空白/逗号/制表符分隔、按别名顺序对齐的值序列。
    不能可靠识别的字段留空，不猜数字。
    """
    fields: Dict[str, Any] = {"_raw": text}
    normalized = str(text or "").replace("\u3000", " ").strip()
    # 形式1：key:value
    kv = re.findall(r"([^\s:：,，;；]+)\s*[:：]\s*([^\s,，;；]+)", normalized)
    if kv:
        for label, value in kv:
            key = match_alias(label, icfg["fields"])
            if key:
                fields[key] = value
        return fields
    # 形式2：分隔值序列，按配置列顺序对齐
    cols = icfg.get("data_source", {}).get("columns", [])
    parts = [p for p in re.split(r"[\s,，;；\t]+", normalized) if p]
    if cols and len(parts) >= 2:
        for col, value in zip(cols, parts):
            key = match_alias(col, icfg["fields"])
            if key:
                fields[key] = value
    return fields


def parse_table(path: str | Path, icfg: Dict[str, Any], sheet: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取附件 xlsx/csv，按表头别名映射为语义字段。"""
    p = Path(expand(str(path)) or str(path))  # type: ignore[arg-type]
    if not p.exists():
        raise FileNotFoundError(f"数据源不存在：{p}")
    rows: List[List[Any]] = []
    if p.suffix.lower() == ".csv":
        with open(p, "r", encoding="utf-8-sig", newline="") as fh:
            rows = [list(r) for r in csv.reader(fh)]
    else:
        wb = load_workbook(p, data_only=True, read_only=True)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    if not rows:
        return []
    header = [str(c or "").strip() for c in rows[0]]
    keys = [match_alias(h, icfg["fields"]) for h in header]
    out: List[Dict[str, Any]] = []
    for r in rows[1:]:
        rec: Dict[str, Any] = {}
        for key, value in zip(keys, r):
            if key and value is not None and str(value).strip():
                rec[key] = value
        if rec:
            rec["_source"] = p.name
            out.append(rec)
    return out


def parse_messages(messages: List[Dict[str, Any]], icfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析 lark-cli 群消息 JSON（数组）。

    只接受 accepted_sources 声明的形态；图片消息标记为待转录，不猜数字。
    """
    accepted = set(icfg.get("accepted_sources", []))
    out: List[Dict[str, Any]] = []
    for m in messages:
        mid = str(m.get("message_id") or m.get("id") or "")
        sender = m.get("sender_name") or m.get("sender") or ""
        body = m.get("text") or m.get("content") or ""
        mtype = m.get("msg_type") or m.get("type") or "text"
        base: Dict[str, Any] = {
            "message_id": mid,
            "sender": sender,
            "msg_type": mtype,
            "message_time": m.get("create_time") or m.get("message_time") or "",
        }
        if mtype == "text" and body and "text_message" in accepted:
            rec = parse_message_text(str(body), icfg)
            rec.update(base)
            out.append(rec)
        elif mtype in ("image", "post") and "image_transcription" in accepted:
            rec = dict(base)
            rec["_needs_transcription"] = icfg.get("image_transcription", "agent_vision")
            rec["_raw"] = str(body)[:200]
            out.append(rec)
        elif mtype in ("file", "xlsx", "csv"):
            rec = dict(base)
            rec["_attachment"] = str(body)
            rec["_needs_transcription"] = "table_parse"
            out.append(rec)
        # 其它消息形态忽略，不算异常
    return out


def resolve_drivers(
    records: List[Dict[str, Any]], cfg: Dict[str, Any], icfg: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """司机归属：字段优先/发送者优先（配置化），别名归一，未识别单独列出。"""
    aliases = cfg.get("drivers", {}).get("aliases", {}) or {}
    mode = icfg.get("driver_from", "sender_or_field")
    resolved: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    for rec in records:
        item = dict(rec)
        name = str(rec.get("driver") or "").strip()
        if mode in ("field_only",):
            driver = normalize_driver(name, aliases)
        elif mode in ("sender_only",):
            driver = normalize_driver(str(rec.get("sender") or ""), aliases)
        else:  # sender_or_field：字段优先，其次发送者
            driver = normalize_driver(name or str(rec.get("sender") or ""), aliases)
        item["driver"] = driver
        if not driver:
            unknown.append(item)
        else:
            resolved.append(item)
    return resolved, unknown


def dedupe(
    records: List[Dict[str, Any]], icfg: Dict[str, Any], persist: bool = True
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按配置的 key_fields 去重（批内 + 历史池）。历史池不存在则从空开始。

    persist=False 用于 parse/plan 等只读预演：不把键写入历史池，
    否则预演会"消费"掉这批数据，导致后续 run 误判为重复。
    """
    dcfg = icfg.get("dedup", {})
    key_fields = dcfg.get("key_fields", ["message_id", "driver", "date", "route", "km", "trip"])
    store = Path(expand(dcfg.get("store", "state/intake/seen.jsonl")) or "state/intake/seen.jsonl")  # type: ignore[arg-type]
    seen: set[str] = set()
    if store.exists():
        for line in store.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    seen.add(json.loads(line).get("key", ""))
                except json.JSONDecodeError:
                    continue
    unique, dup = [], []
    for rec in records:
        key = make_key(rec, key_fields)
        if key in seen:
            dup.append({**rec, "_dup_key": key})
            continue
        seen.add(key)
        unique.append({**rec, "_dedup_key": key})
    # 仅真正执行（run）时落历史池，下一轮重发才能被拦住；预演不落盘。
    if persist:
        _persist_seen(unique, icfg)
    return unique, dup


def _persist_seen(new_records: List[Dict[str, Any]], icfg: Dict[str, Any]) -> None:
    dcfg = icfg.get("dedup", {})
    store = Path(expand(dcfg.get("store", "state/intake/seen.jsonl")) or "state/intake/seen.jsonl")  # type: ignore[arg-type]
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "a", encoding="utf-8") as fh:
        for rec in new_records:
            if rec.get("_dedup_key"):
                fh.write(json.dumps({"key": rec["_dedup_key"]}, ensure_ascii=False) + "\n")


def build_data_source(records: List[Dict[str, Any]], icfg: Dict[str, Any], year: int, month: int) -> Path:
    """生成标准数据源 xlsx（供 monthly-sheet 的 --source 使用）。"""
    dscfg = icfg.get("data_source", {})
    tmpl = str(dscfg.get("path", "state/intake/{year}年{month}月运输单.xlsx"))
    out = Path(expand(tmpl.format(year=year, month=month)) or tmpl)  # type: ignore[arg-type]
    out.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dscfg.get("columns", []))
    cmap = _column_map(icfg)
    wb = Workbook()
    ws = wb.active
    ws.title = str(dscfg.get("sheet_name", "运输单"))
    ws.append(columns)
    for rec in records:
        row: List[Any] = []
        for col in columns:
            key = match_alias(col, icfg["fields"])
            row.append(rec.get(key, ""))
        ws.append(row)
    wb.save(out)
    return out


def group_by_driver(records: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    g: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        g[str(rec.get("driver", ""))].append(rec)
    return dict(g)


def cmd_parse(args) -> None:
    _, cfg, icfg = _intake_cfg()
    src = read_json(args.messages)
    messages = src if isinstance(src, list) else src.get("messages", [])
    parsed = parse_messages(messages, icfg)
    resolved, unknown = resolve_drivers(parsed, cfg, icfg)
    unique, dup = dedupe(resolved, icfg, persist=False)
    payload = {
        "ok": True,
        "action": "parse",
        "parsed": len(parsed),
        "resolved": len(resolved),
        "unknown": unknown,
        "duplicates": len(dup),
        "needs_transcription": sum(1 for r in unique if r.get("_needs_transcription")),
        "records": unique,
    }
    emit(payload)
    if args.out:
        write_json(args.out, payload)


def cmd_plan(args) -> None:
    _, cfg, icfg = _intake_cfg()
    src = read_json(args.messages)
    messages = src if isinstance(src, list) else src.get("messages", [])
    parsed = parse_messages(messages, icfg)
    resolved, unknown = resolve_drivers(parsed, cfg, icfg)
    unique, dup = dedupe(resolved, icfg, persist=False)
    # 附件需先转成表格记录（若 --attachment 提供）
    if args.attachment:
        unique.extend(parse_table(args.attachment, icfg, args.sheet))
    grouped = group_by_driver(unique)
    required = set(icfg.get("required_fields", []))
    incomplete = [
        {"driver": d, "rows": len(rs), "missing_fields": sorted(required - set(k for r in rs for k in r))}
        for d, rs in grouped.items()
    ]
    emit({
        "ok": True,
        "action": "plan",
        "year": args.year,
        "month": args.month,
        "data_source": expand(str(icfg.get("data_source", {}).get("path", "")).format(year=args.year, month=args.month)),
        "drivers": {d: len(rs) for d, rs in grouped.items()},
        "unknown": len(unknown),
        "duplicates": len(dup),
        "needs_transcription": sum(1 for r in unique if r.get("_needs_transcription")),
        "required_fields": sorted(required),
        "incomplete": incomplete,
        "write": icfg.get("write", {}),
    })


def cmd_run(args) -> None:
    modules, cfg, icfg = _intake_cfg()
    wcfg = icfg.get("write", {})
    src = read_json(args.messages)
    messages = src if isinstance(src, list) else src.get("messages", [])
    parsed = parse_messages(messages, icfg)
    resolved, unknown = resolve_drivers(parsed, cfg, icfg)
    unique, dup = dedupe(resolved, icfg)
    if args.attachment:
        unique.extend(parse_table(args.attachment, icfg, args.sheet))
    needs = [r for r in unique if r.get("_needs_transcription")]
    if needs and not args.assume_transcribed:
        emit({
            "ok": False,
            "exit_code": EXIT_BUSINESS,
            "error": f"有 {len(needs)} 条记录需先转录为结构化字段，未猜数；转录后用 --attachment 提供表格再跑",
            "needs_transcription": len(needs),
        })
        return
    data_source = build_data_source(unique, icfg, args.year, args.month)
    # dedupe() 已把本批通过的键写入历史池，此处不再重复持久化

    action_key = wcfg.get("action", "run")
    module_key = wcfg.get("module", "monthly_sheet")
    module_cfg = modules["modules"][module_key]
    action_cfg = module_cfg["delegates"][action_key]
    extra = ["--year", str(args.year), "--month", str(args.month), "--source", str(data_source)]
    if not args.no_dry_run and wcfg.get("default_dry_run", True):
        pass  # monthly-sheet 默认 dry-run，不加 --no-dry-run 即不落盘
    else:
        extra.append("--no-dry-run")
    result = run_delegate(
        module_cfg, action_cfg, extra,
        timeout=cfg.get("runtime", {}).get("subprocess_timeout_sec"),
        capture=True,  # 捕获子进程输出并嵌入本命令的单一 JSON，避免两段 JSON 粘连污染 stdout
    )
    # 尝试把委托脚本的 JSON 结果解析为结构化字段，便于上层判断
    delegate_payload: Any = None
    stdout_text = result.get("stdout", "")
    if stdout_text:
        try:
            delegate_payload = json.loads(stdout_text)
        except json.JSONDecodeError:
            delegate_payload = stdout_text[-2000:]
    emit({
        "ok": result["ok"],
        "action": "run",
        "data_source": str(data_source),
        "records": len(unique),
        "drivers": len(group_by_driver(unique)),
        "unknown": len(unknown),
        "duplicates": len(dup),
        "dry_run": "--no-dry-run" not in extra,
        "delegate": {k: v for k, v in result.items() if k not in ("stdout", "stderr")},
        "delegate_result": delegate_payload,
        "delegate_stderr": result.get("stderr", "")[-1000:],
    })


def cmd_selftest(args) -> None:
    """脱敏自测：临时目录，不写真表、不连飞书。"""
    import tempfile

    sample_icfg: Dict[str, Any] = {
        "accepted_sources": ["text_message", "image_transcription", "attached_xlsx", "attached_csv"],
        "fields": {
            "date": ["日期", "出车日期", "date"],
            "driver": ["司机", "姓名", "driver"],
            "route": ["路线", "运输地点", "route"],
            "km": ["公里数", "公里", "km"],
            "wage": ["工资", "运费", "wage"],
        },
        "dedup": {"key_fields": ["message_id", "driver", "date", "route", "km"]},
        "data_source": {"columns": ["日期", "司机", "路线", "公里数", "工资"], "sheet_name": "运输单"},
        "driver_from": "sender_or_field",
    }
    with tempfile.TemporaryDirectory() as td:
        sample_icfg["dedup"]["store"] = str(Path(td) / "seen.jsonl")
        sample_icfg["data_source"]["path"] = str(Path(td) / "{year}年{month}月运输单.xlsx")
        msgs = [
            {"message_id": "m1", "type": "text", "sender": "甲", "text": "日期:2026-08-01 司机:甲 路线:广州-深圳 公里数:120 工资:300"},
            {"message_id": "m2", "type": "text", "sender": "乙", "text": "日期:2026-08-02 司机:乙 路线:东莞-广州 公里数:80 工资:200"},
            {"message_id": "m3", "type": "image", "sender": "丙", "text": "[图片]"},
        ]
        parsed = parse_messages(msgs, sample_icfg)
        assert len(parsed) == 3, parsed
        cfg = {"drivers": {"aliases": {}}}
        resolved, unknown = resolve_drivers(parsed, cfg, sample_icfg)
        assert len(resolved) == 3 and not unknown
        unique, dup = dedupe(resolved, sample_icfg)
        assert len(unique) == 3 and not dup
        unique2, dup2 = dedupe(resolved, sample_icfg)
        assert len(unique2) == 0 and len(dup2) == 3
        ds = build_data_source(unique, sample_icfg, 2026, 8)
        assert ds.exists()
        grouped = group_by_driver(unique)
        assert set(grouped) == {"甲", "乙", "丙"}  # 图片按发送者归属，但不猜数值
        assert all("km" not in r for r in unique if r.get("_needs_transcription"))
    emit({"ok": True, "action": "selftest", "checks": 8, "note": "全部在临时目录完成，未写真表、未连飞书"})


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="物流管理系统 · 报单（飞书运输单 -> 各司机报表）")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("parse", help="解析群消息为结构化明细")
    pp.add_argument("--messages", required=True, help="lark-cli 群消息 JSON 文件")
    pp.add_argument("--out")
    pp.set_defaults(fn=cmd_parse)
    pl = sub.add_parser("plan", help="解析+分组+预演，不写文件")
    pl.add_argument("--messages", required=True)
    pl.add_argument("--attachment", help="已转录的运输单表格 xlsx/csv")
    pl.add_argument("--sheet")
    pl.add_argument("--year", type=int, required=True)
    pl.add_argument("--month", type=int, required=True)
    pl.set_defaults(fn=cmd_plan)
    pr = sub.add_parser("run", help="生成数据源并调用内置月度制表模块写入")
    pr.add_argument("--messages", required=True)
    pr.add_argument("--attachment")
    pr.add_argument("--sheet")
    pr.add_argument("--year", type=int, required=True)
    pr.add_argument("--month", type=int, required=True)
    pr.add_argument("--no-dry-run", action="store_true", help="真正写入各司机报表")
    pr.add_argument("--assume-transcribed", action="store_true", help="确认图片已人工转录（不猜数）")
    pr.set_defaults(fn=cmd_run)
    ps = sub.add_parser("selftest", help="脱敏自测")
    ps.set_defaults(fn=cmd_selftest)
    args = p.parse_args()
    try:
        args.fn(args)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        emit({"ok": False, "exit_code": EXIT_CONFIG, "error": str(exc)})
        sys.exit(EXIT_CONFIG)


if __name__ == "__main__":
    main()
