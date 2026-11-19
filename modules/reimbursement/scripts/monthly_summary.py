#!/usr/bin/env python3
"""月初月报：按【司机】分类统计「上个月」报销金额（2026-11-14 改造）。

与旧版的根本区别：统计口径与群白名单**完全解耦**。
旧版遍历 chat_whitelist，靠「一个群 = 一个司机」取 driver；现在所有司机共用一个
「物流部」群，且旧群已停用，若仍按群遍历会漏掉全部司机。因此直接按台账
entries.driver 分组。

**2026-11-14 用户定案（v6）**：
  · 司机是弹性的，只计算发了图片（付款凭证截图）的司机；不铺全员名册、不列 0 笔司机、
    不写「没列到名字的…」，不纠结全员到齐或 open_id 补齐。
  · 不用发私聊给司机，发群里就好：通报只有两层（群内极简总览 + 负责人专业版）。

口径（固定）：
  · 月份归属：pay_time（无则 receipt_date，再无则 created_at）所属月。
  · 统计范围：该月所有未作废（voided=0）报销条目；按 status_model 拆分
    已报销 / 已登记·待打款（唯一计入未结）。
  · 环比：与再上一个月按司机逐一对比，给金额差额与百分比（基数为 0 写「新增」）。
  · 两层通报（金额不保密）+ 极简司机文案（config.driver_message）：
      通道一 群内总览：一份，只列当月有凭证的司机，按司机一行「姓名 ¥金额（日期）」
        + 合计 + 一行小提示 + 一句安全驾驶调侃；不列四态 / 未结 / 环比 / 笔数明细。
      通道二 负责人专业版：**一字不减**，仍含四态、未结、环比、台账/清单路径。
    群内版统一由 scripts/driver_copy.py 渲染，避免口径漂移；
    两层由 notify.py **一次调用**发出；不再生成个人月报/manifest，不再有 missing_driver_dm。
  · 每自然月只在月初窗口内的初次运行真正发送（state/monthly_sent.json 防护）。

用法：
  monthly_summary.py --compute [--month YYYY-MM]           # 仅计算并打印 JSON
  monthly_summary.py --send [--month YYYY-MM] [--run-id X] [--dry-run]
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
STATE_DIR = os.path.join(HERE, "..", "state")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get, state_dir as rc_state_dir  # 配置优先、原值兜底

# main() 读取配置后设置；纯函数/直接调用时回退原值，保证输出不变
_CFG = {}


def _sent_file():
    return os.path.join(rc_state_dir(_CFG or None), "monthly_sent.json")


def _state_dir():
    return rc_state_dir(_CFG or None)

try:
    from driver_roster import report_drivers as roster_report_drivers
    from driver_roster import primary_chat_id as roster_primary_chat_id
except ImportError:  # 兜底：独立运行时也能工作
    roster_report_drivers = None
    roster_primary_chat_id = None

# 极简司机版统一渲染（只讲金额和日期 + 一句小提示 + 一句安全驾驶调侃）
# 司机私聊已取消，只导入群内渲染
from driver_copy import render_group as dc_render_group


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def db_path(cfg):
    state = cfg.get("state_dir") or STATE_DIR
    return os.path.join(state, "ledger.sqlite")


def connect(cfg):
    import sqlite3
    conn = sqlite3.connect(db_path(cfg))
    conn.row_factory = sqlite3.Row
    return conn


def target_month(ref=None, override=None):
    """返回 (YYYY-MM, 标签)。默认上个月；可 override。"""
    if override:
        ym = override
        y, m = ym.split("-")[:2]
        label = "%s年%s月" % (y, int(m))
        return ym, label
    ref = ref or date.today()
    first = ref.replace(day=1)
    last = first - timedelta(days=1)
    return last.strftime("%Y-%m"), "%d年%d月" % (last.year, last.month)


def entry_month(e, cfg=None):
    c = cfg if cfg is not None else _CFG
    priority = (((c.get("adapters", {}).get("input", {}).get("window", {}) or {})
                 .get("period_field_priority"))
                or ("pay_time", "receipt_date", "created_at"))
    for f in priority:
        v = e.get(f)
        if v:
            return str(v)[:7]
    return None


def _month_rows(cfg, months):
    """一次连接、一次全表扫描，返回 {ym: [row_dict,...]}。
    替代原先每个月各开一次连接、各扫一次全表（compute 原本调用两次）。
    """
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT driver, amount, pay_time, receipt_date, created_at, reimbursed, voided "
            "FROM entries WHERE voided=0"
        ).fetchall()
    finally:
        conn.close()
    out = {m: [] for m in months}
    for r in rows:
        e = dict(r)
        m = entry_month(e, cfg)
        if m in out:
            out[m].append(e)
    return out


def _aggregate_month(rows, cfg=None):
    """把一个月的行聚合为 {driver: rec}，逻辑与原 compute_month 完全一致。"""
    c = cfg if cfg is not None else _CFG
    by_driver = {}
    for e in rows:
        d = e["driver"] or rc_get(c, "unknown_driver", "未识别司机")
        rec = by_driver.setdefault(d, {
            "count": 0, "total": 0.0, "reimbursed": 0.0, "pending": 0.0,
            "reimbursed_count": 0, "pending_count": 0, "dates": [],
        })
        rec["count"] += 1
        rec["total"] += float(e["amount"] or 0)
        # 司机版只讲「金额和日期」：收集该司机本月的凭证日期（去重后按升序）
        for f in ("pay_time", "receipt_date", "created_at"):
            if e.get(f):
                rec["dates"].append(str(e[f])[:10])
                break
        if e["reimbursed"]:
            rec["reimbursed"] += float(e["amount"] or 0)
            rec["reimbursed_count"] += 1
        else:
            rec["pending"] += float(e["amount"] or 0)
            rec["pending_count"] += 1
    return by_driver


def compute_month(cfg, ym):
    """兼容入口：单月聚合（历史调用方保留）。compute() 内部已改为一次扫描。"""
    return _aggregate_month(_month_rows(cfg, [ym])[ym], cfg)


def prev_month_ym(ym):
    y, m = ym.split("-")[:2]
    y, m = int(y), int(m)
    if m == 1:
        return "%04d-12" % (y - 1)
    return "%04d-%02d" % (y, m - 1)


def has_report(cfg, driver, ym):
    year = int(ym.split("-")[0])
    if roster_report_drivers:
        return driver in roster_report_drivers(cfg, year)
    return False


def primary_chat(cfg):
    if roster_primary_chat_id:
        cid = roster_primary_chat_id(cfg)
    else:
        cid = (cfg.get("chat_discovery", {}).get("primary_chat_id")
               or next(iter(cfg.get("chat_whitelist", {})), ""))
    entry = cfg.get("chat_whitelist", {}).get(cid, {})
    return {"chat_id": cid, "name": entry.get("name", ""),
            "notify": bool(entry.get("notify")),
            "mode": entry.get("mode", "shared")}


def agg(by):
    acc = {"count": 0, "total": 0.0, "reimbursed": 0.0, "pending": 0.0}
    for rec in by.values():
        acc["count"] += rec["count"]
        acc["total"] += rec["total"]
        acc["reimbursed"] += rec["reimbursed"]
        acc["pending"] += rec["pending"]
    return {k: round(v, 2) for k, v in acc.items()}


def compute(cfg, ym):
    # 一次连接、一次全表扫描取回目标月与对比月的行，再在内存里分别聚合。
    # 原实现调用 compute_month 两次 → 两次连接 + 两次全表扫描。
    prev_ym = prev_month_ym(ym)
    rows_by_month = _month_rows(cfg, [ym, prev_ym])
    by_driver = _aggregate_month(rows_by_month[ym], cfg)
    prev_by_driver = _aggregate_month(rows_by_month[prev_ym], cfg)

    # 只列当月实际有凭证（发了图片并已入账）的司机：司机是弹性的，不铺全员名册、
    # 不列 0 笔司机、不纠结谁没到齐。对比月数据仍单独取，仅供负责人专业版环比使用。
    names = set(by_driver)
    zero = {"count": 0, "total": 0.0, "reimbursed": 0.0, "pending": 0.0,
            "reimbursed_count": 0, "pending_count": 0, "dates": []}
    out_drivers = []
    for d in sorted(names):
        cur = by_driver.get(d, zero)
        prev = prev_by_driver.get(d, zero)
        out_drivers.append({
            "driver": d,
            "count": cur["count"], "total": round(cur["total"], 2),
            "reimbursed": round(cur["reimbursed"], 2),
            "pending": round(cur["pending"], 2),
            "reimbursed_count": cur["reimbursed_count"],
            "pending_count": cur["pending_count"],
            "dates": sorted(set(cur.get("dates") or [])),
            "prev_count": prev["count"], "prev_total": round(prev["total"], 2),
            "has_report": has_report(cfg, d, ym),
        })

    grand = agg(by_driver)
    prev_grand = agg(prev_by_driver)
    grand["prev_count"] = prev_grand["count"]
    grand["prev_total"] = prev_grand["total"]

    return out_drivers, grand, prev_ym


def ring_delta(cur, prev):
    delta = round(cur - prev, 2)
    pct = round(delta / prev * 100, 1) if prev > 0 else None
    return delta, pct


def ring_text_driver(cur, prev):
    """司机版大白话环比。"""
    delta, pct = ring_delta(cur, prev)
    if cur == 0 and prev == 0:
        return "连续两月暂无报销记录"
    if cur == 0 and prev > 0:
        return "本月暂无报销（上月 ¥%.2f）" % prev
    if cur > 0 and prev == 0:
        return "本月新增报销 ¥%.2f（上月无记录）" % cur
    if delta > 0:
        return "比上月多 ¥%.2f（约 +%.0f%%）" % (delta, pct)
    if delta < 0:
        return "比上月少 ¥%.2f（约 -%.0f%%）" % (abs(delta), abs(pct))
    return "与上月持平（均为 ¥%.2f）" % cur


def ring_text_owner(cur, prev):
    """负责人版专业环比（差额 + 百分比）。"""
    delta, pct = ring_delta(cur, prev)
    if prev > 0:
        sign = "+" if delta >= 0 else "-"
        return "环比 %s%.1f%%（%s¥%.2f，上月 ¥%.2f）" % (sign, abs(pct), sign, abs(delta), prev)
    if delta > 0:
        return "环比 新增（+¥%.2f，上月 ¥0.00）" % delta
    return "环比 持平（上月 ¥%.2f）" % prev


def build_driver_data(cfg, drivers, label, generated_at, due, run_id, chat):
    """把按司机统计结果转成 driver_copy 的极简数据：只有金额和日期。

    四态 / 未结 / 环比 / 笔数 / 路径等一律不进司机版，只留负责人专业版。
    """
    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "window": label,
        "due": due,
        "scope_label": "上月报销",
        "group_name": chat.get("name") or rc_get(_CFG, "default_group_name", "物流部"),
        "rows": [{"driver": d["driver"], "amount": d["total"],
                  "dates": d.get("dates") or []} for d in drivers],
        "issues": [],
        "no_change_wording": rc_get(_CFG, "monthly_no_change_wording",
                                    "上月没有报销，之前记过的都算数。"),
    }


def render_group(data, cfg):
    """通道一：极简群内总览（有凭证的司机 + 金额 + 日期 + 小提示 + 安全驾驶调侃）。"""
    return dc_render_group(data, cfg)


def render_owner(drivers, grand, label, prev_label, generated_at,
                 ledger_path, run_log, chat):
    """负责人私聊专业版：按司机分节明细 + 合计 + 环比 + 路径。"""
    lines = []
    for d in drivers:
        lines.append("· %s：%d 笔，¥%.2f（已报销 ¥%.2f / 待报销 ¥%.2f）｜%s%s"
                     % (d["driver"], d["count"], d["total"],
                        d["reimbursed"], d["pending"],
                        ring_text_owner(d["total"], d["prev_total"]),
                        "" if d["has_report"] else "｜无当年报表，record_without_write 待指定"))
    ring_g = ring_text_owner(grand["total"], grand["prev_total"])
    body = (
        "【上月报销汇总·专业版｜按司机分类】%s\n"
        "统计月份：%s（pay_time 所属月，已剔除已作废）\n"
        "对比月份：%s\n"
        "统计口径：直接按台账 driver 分组，与群白名单解耦；旧群停用不影响历史数据\n"
        "统计对象：只列当月实际发了付款凭证截图的司机（司机弹性、不铺 0 笔司机、不催全员到齐）\n"
        "通报群：%s（%s，唯一 notify=true）\n\n"
        "各司机上月报销金额（仅列有凭证者）：\n%s\n"
        "─────────\n"
        "合计：%d 笔，¥%.2f（已报销 ¥%.2f / 待报销 ¥%.2f）｜%s\n\n"
        "台账：%s\n"
        "清单：%s"
    ) % (generated_at, label, prev_label,
         chat["name"] or rc_get(_CFG, "default_group_name", "物流部"), chat["chat_id"],
         "\n".join(lines),
         grand["count"], grand["total"], grand["reimbursed"], grand["pending"], ring_g,
         ledger_path, run_log)
    if grand["count"] == 0:
        body += "\n（上月无报销记录）"
    return body


def load_sent():
    sf = _sent_file()
    if not os.path.isfile(sf):
        return {}
    try:
        return json.load(open(sf, encoding="utf-8"))
    except Exception:
        return {}


def already_sent(ym):
    return load_sent().get("last_month") == ym


def mark_sent(ym):
    d = load_sent()
    d["last_month"] = ym
    d["sent_at"] = datetime.now().isoformat(timespec="seconds")
    sf = _sent_file()
    os.makedirs(os.path.dirname(sf), exist_ok=True)
    json.dump(d, open(sf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def call_notify(args_list):
    return subprocess.run(["python3", os.path.join(HERE, "notify.py")] + args_list,
                          capture_output=True, text=True, timeout=120)


def parse_notify_result(r):
    """返回 notify.py 的完整输出（results / missing_driver_dm / errors）。"""
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return {"raw_stdout": r.stdout[-400:], "raw_stderr": r.stderr[-300:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compute", action="store_true", help="仅计算并打印 JSON")
    ap.add_argument("--send", action="store_true", help="渲染并发送月报")
    ap.add_argument("--month", help="目标月 YYYY-MM（默认上个月）")
    ap.add_argument("--run-id", help="运行 ID（用于幂等键与清单命名）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not (args.compute or args.send):
        ap.error("需指定 --compute 或 --send")

    cfg = load_config()
    global _CFG
    _CFG = cfg
    ym, label = target_month(override=args.month)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    drivers, grand, prev_ym = compute(cfg, ym)
    _, prev_label = target_month(override=prev_ym)
    chat = primary_chat(cfg)
    payload = {
        "ok": True, "target_month": ym, "target_month_label": label,
        "prev_month": prev_ym, "prev_month_label": prev_label,
        "generated_at": generated_at,
        "grouping": "by_driver",
        "active_drivers_only": bool((cfg.get("reporting") or {}).get("active_drivers_only")),
        "notification_channels": (cfg.get("reporting") or {}).get("channels"),
        "driver_message_mode": (cfg.get("driver_message") or {}).get("mode"),
        "primary_chat": chat,
        "active_driver_count": len(drivers),
        "drivers": drivers, "grand": grand,
    }

    if args.compute:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    # ---- --send ----
    run_id = args.run_id or ("monthly-%s-%s" % (ym, datetime.now().strftime("%m%d%H%M")))
    ledger_path = db_path(cfg)
    run_log = os.path.normpath(os.path.join(_state_dir(), "runs", "monthly_%s_%s.md"
                                            % (ym, datetime.now().strftime("%m%d%H%M"))))

    if not args.dry_run and already_sent(ym):
        print(json.dumps({"ok": True, "skipped": True, "reason": "本月已发送过月报",
                          "target_month": ym}, ensure_ascii=False, indent=2))
        return

    due = (date.today() + timedelta(
        days=int((cfg.get("group_task") or {}).get("due_days") or 3))).isoformat()
    # 群内版统一走极简渲染：只有有凭证司机的金额、日期、一句小提示、一句安全驾驶调侃
    driver_data = build_driver_data(cfg, drivers, label, generated_at, due,
                                    run_id, chat)
    group_text = render_group(driver_data, cfg)
    owner_text = render_owner(drivers, grand, label, prev_label, generated_at,
                              ledger_path, run_log, chat)
    tmp_dir = rc_get(_CFG, "tmp_dir", "/tmp")
    group_file = os.path.join(tmp_dir, "monthly_group_%s.txt" % ym)
    owner_file = os.path.join(tmp_dir, "monthly_owner_%s.txt" % ym)
    with open(group_file, "w", encoding="utf-8") as f:
        f.write(group_text)
    with open(owner_file, "w", encoding="utf-8") as f:
        f.write(owner_text)

    # 两层通报：一次 notify.py 调用同时发出（群内极简总览 + 负责人专业完整版）
    # 司机私聊通道已整体取消：不生成 manifest、不传 --driver-dm-manifest、不催 open_id。
    if chat["notify"] and chat["chat_id"]:
        cmd = ["--chat-id", chat["chat_id"], "--text-file", group_file,
               "--owner-text-file", owner_file, "--run-id", run_id]
        if args.dry_run:
            cmd.append("--dry-run")
        out = parse_notify_result(call_notify(cmd))
        results = out.get("results", out) if isinstance(out, dict) else {"raw": out}
        g = results.get("group")
        if isinstance(g, dict):
            g["chat_id"] = chat["chat_id"]
            g["name"] = chat["name"]
    else:
        owner_cmd = ["--owner-only", "--owner-text-file", owner_file, "--run-id", run_id]
        if args.dry_run:
            owner_cmd.append("--dry-run")
        out = parse_notify_result(call_notify(owner_cmd))
        owner_res = (out.get("results", {}).get("owner", {}) if isinstance(out, dict) else out)
        results = {
            "group": {"sent": False, "note": "唯一主群 notify=false 或 chat_id 为空，未发群消息"},
            "owner": owner_res,
        }

    # 写清单（dry-run 也写，便于核对渲染结果）
    os.makedirs(os.path.join(_state_dir(), "runs"), exist_ok=True)
    with open(run_log, "w", encoding="utf-8") as f:
        f.write("# 月初月报（按司机分类）%s\n\n" % label)
        f.write("运行时间：%s\n" % generated_at)
        f.write("司机文案口径：极简（config.driver_message：只讲金额和日期 + 一句小提示 + 一句安全驾驶调侃）\n")
        f.write("负责人专业版：保持四态 / 未结 / 环比 / 路径，一字不减\n")
        f.write("通报通道：两层（群内极简总览 + 负责人私聊专业版）；司机私聊已取消\n")
        f.write("运行模式：%s\n" % ("dry-run（仅渲染，未真实发送、未标记本月已发）"
                                   if args.dry_run else "正式发送"))
        f.write("目标月：%s（pay_time 所属月，已剔除已作废）\n" % label)
        f.write("对比月：%s（环比基准）\n" % prev_label)
        f.write("统计口径：直接按台账 driver 分组，与群白名单解耦\n")
        f.write("统计对象：只列当月实际发了付款凭证截图的司机（弹性名册，不铺 0 笔司机、不催全员到齐）\n")
        f.write("本月有凭证司机数：%d\n" % len(drivers))
        f.write("通报群：%s（%s）\n\n" % (chat["name"], chat["chat_id"]))
        f.write("## 各司机统计\n\n")
        for d in drivers:
            f.write("- %s：%d 笔 ¥%.2f（已报销 ¥%.2f / 待报销 ¥%.2f）｜%s%s\n"
                    % (d["driver"], d["count"], d["total"], d["reimbursed"], d["pending"],
                       ring_text_owner(d["total"], d["prev_total"]),
                       "" if d["has_report"] else "｜无当年报表（record_without_write）"))
        f.write("\n## 合计（按司机汇总）\n\n")
        f.write("- %d 笔，¥%.2f（已报销 ¥%.2f / 待报销 ¥%.2f）\n\n"
                % (grand["count"], grand["total"], grand["reimbursed"], grand["pending"]))
        f.write("## 通道一 群内总览（极简司机版，只列有凭证司机）\n\n```text\n%s\n```\n\n" % group_text)
        f.write("## 通道二 负责人私聊（专业版，一字不减）\n\n```text\n%s\n```\n\n" % owner_text)
        f.write("## 两层发送结果\n\n```json\n%s\n```\n\n"
                % json.dumps(results, ensure_ascii=False, indent=2, default=str))
        f.write("司机私聊：已取消（config.reporting.per_driver_dm.enabled=false）；"
                "不生成 manifest、不催 open_id、不统计谁没到齐。\n")

    if not args.dry_run:
        ok_any = any(isinstance(v, dict) and v.get("sent") is True
                     for v in (results or {}).values())
        if ok_any:
            mark_sent(ym)

    payload["results"] = results
    payload["run_log"] = run_log
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
