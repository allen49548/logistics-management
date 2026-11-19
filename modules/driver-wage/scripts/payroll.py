#!/usr/bin/env python3
"""司机工资技能 · 统一 CLI。

只有两个业务能力：compute（计算工资）、verify（核对工资）。
其余子命令都是这两件事的**输入准备**（sync-roster / extract-style / learn-rules）或
**交付编排**（preview / confirm / dispatch / status），不构成第三个业务功能。

典型月度流程（每月 10 号由自动化发起，止于 preview）：
  payroll.py sync-roster                          # 从飞书同步司机名册（主数据源）
  payroll.py preview  --month 2026-09             # 计算 + 核对 + 统一表格 + 待确认预览

典型月度流程（每月 10 号由自动化发起，止于 preview）：
  payroll.py preview  --month 2026-09          # 计算 + 核对 + 统一表格 + 待确认预览
  payroll.py confirm  --month 2026-09 --token <口令> --owner <发起人>
  payroll.py dispatch --month 2026-09 --no-dry-run --channel feishu

所有命令输出 JSON 摘要到 stdout，便于上层 Agent 解析。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audit  # noqa: E402
import compute  # noqa: E402
import config as cfgmod  # noqa: E402
import gate  # noqa: E402
import notify  # noqa: E402
import render  # noqa: E402
import verify  # noqa: E402


# 飞书机器人配置自检结果（2026-11-16 用户定案），由 main() 设置，_emit 统一注入
_BOT_SETUP = None


def _emit(obj):
    if _BOT_SETUP is not None and isinstance(obj, dict):
        obj = dict(obj)
        obj["bot_setup"] = _BOT_SETUP
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _brief(result, verification=None):
    return {"month": result["month"], "generated_at": result["generated_at"],
            "result_hash": result["result_hash"][:16],
            "input_hash": result["input_hash"][:16],
            "totals": result["totals"],
            "warnings": len(result["warnings"]), "errors": len(result["errors"]),
            "verdict": verification["verdict"] if verification else None}


def _roster_brief(cfg: dict, result: dict) -> dict:
    """口径摘要：飞书在册数 / 实际计算数 / 可私发数，让发起人一眼看懂人数差异。"""
    meta = cfg.get("_roster_meta", {})
    rcfg = cfg["payroll"].get("roster", {})
    feishu_only = (bool(cfg["payroll"].get("notify", {})
                        .get("feishu_roster_only", True))
                   and bool(meta.get("feishu_enabled")))
    slips = {s["driver"] for s in result["payslips"]}
    dispatchable = [d["id"] for d in cfg["roster"].get("drivers", [])
                    if d["id"] in slips and d.get("notify", True)
                    and str(d.get("open_id", "")).startswith("ou_")
                    and (not feishu_only or d.get("feishu") is True)]
    out = {"source": rcfg.get("source", "local"),
           "roster_total": len(cfg["roster"].get("drivers", [])),
           "feishu_drivers": len(meta.get("feishu_drivers", [])),
           "computed_drivers": len(slips),
           "dispatchable_drivers": len(dispatchable),
           "feishu_only_dispatch": feishu_only}
    if meta.get("warning"):
        out["warning"] = meta["warning"]
    return out


def cmd_compute(args, cfg):
    result = compute.compute(cfg, args.month)
    audit.log(cfg, args.month, "compute", result_hash=result["result_hash"],
              trips=result["totals"]["trips"], drivers=result["totals"]["drivers"])
    _emit({"command": "compute", **_brief(result), "detail": result})


def cmd_verify(args, cfg):
    result = compute.load_result(cfg, args.month)
    report = verify.verify(cfg, args.month, result)
    audit.log(cfg, args.month, "verify", verdict=report["verdict"],
              fail=report["fail_count"], warn=report["warn_count"])
    _emit({"command": "verify", **_brief(result, report),
           "verification": report,
           "verification_md": os.path.join(cfg["paths"]["out_dir"], args.month,
                                           "verification.md")})


def cmd_preview(args, cfg):
    """定时任务的终点：算完、核完、排好版、生成待确认预览；绝不私发司机。"""
    result = compute.compute(cfg, args.month)
    report = verify.verify(cfg, args.month, result)
    workbook = render.render_workbook(cfg, args.month, result, report)
    slips = render.render_payslips(cfg, args.month, result)
    preview = gate.make_preview(cfg, args.month)
    owner = notify.notify_owner(cfg, args.month, preview,
                                dry_run=not (args.notify_owner and args.no_dry_run))
    audit.log(cfg, args.month, "preview_done", verdict=report["verdict"])
    _emit({"command": "preview", **_brief(result, report),
           "workbook": workbook, "payslip_files": len(slips),
           "verdict": report["verdict"], "roster": _roster_brief(cfg, result),
           "blocking": report["blocking"], "review": report["review"],
           "token": preview["token"], "owner": preview["owner"],
           "expires_at": preview["expires_at"],
           "owner_notified": not owner["dry_run"],
           "next_step": preview["next_step"],
           "note": "本命令绝不向司机发送任何消息；需发起人确认后再 dispatch。"})


def cmd_confirm(args, cfg):
    rec = gate.confirm(cfg, args.month, args.token, args.owner, args.accept_review)
    _emit({"command": "confirm", "status": "confirmed", **rec})


def cmd_dispatch(args, cfg):
    report = notify.dispatch(cfg, args.month, dry_run=not args.no_dry_run,
                             channel=args.channel, force=args.force)
    _emit({"command": "dispatch", **report})


def cmd_sync_roster(args, cfg):
    """输入准备：从名册来源同步司机名册（飞书只读、可离线降级；manual/csv 直接读取）。"""
    import adapters
    source = adapters.get_roster_source(cfg)
    out = source.sync(cfg, cfg["payroll"].get("roster", {}))
    audit.log(cfg, "roster", "sync_roster", ok=out["ok"],
              drivers=out["drivers"], notes=out["notes"])
    _emit({"command": "sync-roster", "source": source.name, **out,
           "note": "只同步名册；飞书失败时保留旧缓存且不伪造名册；"
                   "manual/csv 来源无需联网。"})


def cmd_status(args, cfg):
    out = os.path.join(cfg["paths"]["out_dir"], args.month)
    state = os.path.join(cfg["paths"]["state_dir"], args.month)
    def exists(p):
        return os.path.isfile(p)
    _, conf_path = gate._paths(cfg, args.month)
    rec = None
    if exists(conf_path):
        with open(conf_path, "r", encoding="utf-8") as f:
            rec = json.load(f)
    _emit({"command": "status", "month": args.month,
           "profile": cfg["_meta"]["profile"],
           "payslips": exists(os.path.join(out, "payslips.json")),
           "verification": exists(os.path.join(out, "verification.json")),
           "preview": exists(os.path.join(state, "preview.json")),
           "confirmed": rec,
           "sent": audit.load_sent(cfg, args.month)})


def cmd_extract_style(args, cfg):
    style = render.extract_style(args.template, args.out, args.header_row,
                                 args.data_row, args.total_row)
    _emit({"command": "extract-style", "out": args.out,
           "font": style["font"], "palette": style["palette"],
           "note": "已生成统一模板规范；把该路径设为 config/style.json 即可全表统一。"})


def cmd_learn_rules(args, cfg):
    import learn_rules
    out = learn_rules.learn(args.wage_dir, args.out, args.year, args.month_sheet)
    _emit({"command": "learn-rules", **out})


def cmd_init(args, cfg):
    """引导式接入：把样本文件变成草稿配置 + 人话复核清单。"""
    import init as init_mod
    out_dir = args.out_dir or os.path.join(cfg["_meta"]["root"], "config", "local")
    out = init_mod.run(cfg["_meta"]["root"], args.trip_file, args.wage_dir,
                       args.template, args.roster_file, out_dir, args.year)
    _emit({"command": "init", **out,
           "note": "草稿已生成在 config/local/。请按 REVIEW.md 逐条确认，"
                   "尤其 rules.json 的 confirmed、gate.owner、notify.channel。"})


def cmd_validate(args, cfg):
    """人话配置校验：缺什么、错什么、下一步做什么，而不是抛技术错误。"""
    issues = []
    # 发起人
    owner = cfg["payroll"].get("gate", {}).get("owner", "")
    if not owner or owner.startswith("<"):
        issues.append(("error", "gate.owner 未填写：核对确认闸门与待确认通知都需要明确的发起人。"))
    # 通道
    ch = cfg["payroll"].get("notify", {}).get("channel", "")
    if ch in ("", "dry-run"):
        issues.append(("warn", f"notify.channel={ch!r}：当前不会真正外发，仅生成预览。"
                               "需要真发时改为 csv-export / feishu 并显式 --no-dry-run。"))
    elif ch not in ("dry-run", "stdout", "csv-export", "feishu", "email"):
        issues.append(("error", f"notify.channel={ch!r} 不是已知通道；"
                               "可选 dry-run/stdout/csv-export/feishu/email。"))
    # 名册来源
    rsrc = cfg["payroll"].get("roster", {}).get("source", "manual")
    if rsrc == "feishu":
        fs = cfg["payroll"]["roster"].get("feishu", {})
        if not fs.get("department_query") and not fs.get("chat_id"):
            issues.append(("error", "roster.source=feishu 但未配置 department_query / chat_id，"
                                   "sync-roster 将拉不到任何人。"))
    elif rsrc == "csv" and not cfg["payroll"]["roster"].get("csv", {}).get("path"):
        issues.append(("error", "roster.source=csv 但未配置 roster.csv.path。"))
    # 规则确认
    unconfirmed = [k for k, v in cfg["rules"].get("drivers", {}).items()
                   if not v.get("confirmed")]
    if unconfirmed:
        issues.append(("error", f"有 {len(unconfirmed)} 条计薪规则未确认（{', '.join(unconfirmed[:6])}…），"
                               "核对会 BLOCK、不能发放。"))
    drivers = cfg["roster"].get("drivers", [])
    if not drivers:
        issues.append(("error", "roster.json 没有任何司机；请补充名册或重跑 init --roster-file。"))
    # 汇总
    errs = [m for lv, m in issues if lv == "error"]
    warns = [m for lv, m in issues if lv == "warn"]
    verdict = "OK" if not errs else "NEEDS_FIX"
    _emit({"command": "validate", "verdict": verdict,
           "errors": errs, "warnings": warns,
           "next": ("可直接跑 preview 看核对结论。" if verdict == "OK"
                    else "请先修复 error 项；warn 项不阻断但影响发放。"),
           "hint": "新手可用 `payroll.py init` 指向样本文件自动生成草稿配置。"})


def cmd_demo(args, cfg):
    """脱敏样例端到端自测：不改任何真实数据、不外发任何消息。"""
    month = args.month
    result = compute.compute(cfg, month)
    report = verify.verify(cfg, month, result)
    workbook = render.render_workbook(cfg, month, result, report)
    slips = render.render_payslips(cfg, month, result)
    preview = gate.make_preview(cfg, month)
    conf = gate.confirm(cfg, month, preview["token"],
                        cfg["payroll"]["gate"]["owner"],
                        accept_review=(report["verdict"] == "REVIEW"))
    dry = notify.dispatch(cfg, month, dry_run=True)
    _emit({"command": "demo", **_brief(result, report),
           "workbook": workbook, "payslip_files": len(slips),
           "verdict": report["verdict"], "token": preview["token"],
           "confirmed_by": conf["owner"],
           "dry_dispatch": {"sent": dry["sent"],
                            "missing_open_id": dry["missing_open_id"]},
           "note": "全程 dry-run，未发送任何真实消息。"})


def main():
    ap = argparse.ArgumentParser(description="司机工资：计算 + 核对")
    ap.add_argument("--profile", help="local / example / 配置目录绝对路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, func, month=True):
        p = sub.add_parser(name)
        if month:
            p.add_argument("--month", required=True, help="YYYY-MM")
        p.set_defaults(func=func)
        return p

    add("compute", cmd_compute)
    add("verify", cmd_verify)
    p = add("preview", cmd_preview)
    p.add_argument("--notify-owner", action="store_true", help="同时通知发起人")
    p.add_argument("--no-dry-run", action="store_true", help="允许真实外发（仅发起人通知）")
    p = add("confirm", cmd_confirm)
    p.add_argument("--token", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--accept-review", action="store_true")
    p = add("dispatch", cmd_dispatch)
    p.add_argument("--no-dry-run", action="store_true", help="关闭 dry-run，真实私发")
    p.add_argument("--channel", help="覆盖通道：feishu / stdout / dry-run")
    p.add_argument("--force", action="store_true", help="忽略幂等重发（慎用）")
    add("status", cmd_status)
    add("demo", cmd_demo)

    p = sub.add_parser("validate", help="人话配置校验：缺什么、错什么、下一步做什么")
    p.set_defaults(func=cmd_validate, month=None)

    p = sub.add_parser("sync-roster", help="从飞书同步司机名册（只读飞书）")
    p.set_defaults(func=cmd_sync_roster, month=None)

    p = sub.add_parser("extract-style")
    p.add_argument("--template", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--header-row", type=int, default=1)
    p.add_argument("--data-row", type=int, default=2)
    p.add_argument("--total-row", type=int)
    p.set_defaults(func=cmd_extract_style, month=None)

    p = sub.add_parser("learn-rules")
    p.add_argument("--wage-dir", required=True, help="历史工资表目录")
    p.add_argument("--out", required=True, help="候选规则输出路径（应放 config/local/）")
    p.add_argument("--year", default="2026")
    p.add_argument("--month-sheet", help="指定月份 sheet，默认自动取最近月份")
    p.set_defaults(func=cmd_learn_rules, month=None)

    p = sub.add_parser(
        "init",
        help="引导式接入：给样本文件，自动生成草稿 config/local + 复核清单")
    p.add_argument("--trip-file", help="运输单样本（xlsx/csv），用于探测列结构")
    p.add_argument("--wage-dir", help="历史工资表目录，用于反推测薪规则")
    p.add_argument("--template", help="工资表模板（用于抽取统一样式）")
    p.add_argument("--roster-file", help="名册文件（csv/xlsx）：id,name,open_id,notify")
    p.add_argument("--out-dir", default=None,
                   help="草稿输出目录，默认 {root}/config/local")
    p.add_argument("--year", default="2026")
    p.set_defaults(func=cmd_init, month=None)

    args = ap.parse_args()
    # 飞书机器人配置自检：司机工资以私聊为主，只做核心检测（cli 可用 + bot 已授权）；
    # 未配置时随输出给出提醒 + 教程，不静默、不冒充个人身份。
    global _BOT_SETUP
    try:
        import bot_check
        _BOT_SETUP = bot_check.check([])
    except Exception as _e:  # 检测本身失败不阻断
        _BOT_SETUP = {"bot_setup_required": False, "check_error": str(_e)[:200]}
    try:
        cfg = cfgmod.load_all(args.profile)
        args.func(args, cfg)
    except (cfgmod.ConfigError, gate.GateError, FileNotFoundError) as exc:
        _emit({"error": str(exc), "type": type(exc).__name__})
        sys.exit(2)
    except Exception as exc:
        _emit({"error": str(exc), "type": type(exc).__name__})
        raise


if __name__ == "__main__":
    main()
