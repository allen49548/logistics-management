#!/usr/bin/env python3
"""极简司机版通报渲染（2026-11-14 用户口径，config.driver_message）。

用户要求：报销统计完，**只告诉司机本次报销金额和日期**；异常尽量简单描述；
结尾送一句安全驾驶主题的调侃话。四态、未结、环比、订单号、任务链接、路径等
一律只保留在负责人专业版（owner_professional_unchanged=true）。

日常归集与月初月报共用本模块，保证两处文案一字不差：
  · 群内总览  render_group(data) —— 按司机一行「姓名 ¥金额（日期）」+ 合计

**只列本轮/本月实际发了付款凭证截图的司机**（用户 2026-11-14 定案：司机是弹性的、
只计算发了图片的司机、不纠结全员到齐）。0 笔司机不出现、不写「没列到名字的…」零值提示。
群内版带一行「统计 … ｜打款截止 …」（旧铁律）、可选一行小提示、以及必带的调侃句。

`render_personal` / `--kind personal` 仅为兼容旧调用保留（已标记 deprecated）：
司机私聊通道已整体取消，当前流程绝不调用它，notify.py 也会硬拦截任何 --driver-dm-manifest。

数据结构（data，JSON 文件）：
{
  "run_id": "20261114_2023",
  "generated_at": "2026-11-14 20:23",
  "window": "10-31 09:15 ~ 11-14 09:15",
  "due": "11-17",
  "scope_label": "本次报销",              // 月报可传「上月报销」
  "group_name": "物流部",
  "rows": [
    {"driver": "陈华胜", "amount": 209.20,
     "dates": ["2026-11-12", "2026-11-13"], "issue": ""}
  ],
  "issues": ["2 张手写单请改发付款截图", "重复发的之前记过、没多算"],
  "personal_issues": {"陈华胜": "2 张手写单请改发付款截图"}
}

用法：
  python3 scripts/driver_copy.py joke --run-id <运行ID>
  python3 scripts/driver_copy.py render --data <data.json> --kind group --out <群版.txt>
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def dm_cfg(cfg):
    return cfg.get("driver_message") or {}


def pick_joke(cfg, run_id=None):
    """按日期顺序轮换：同一轮全队同一句，连续 N 天不重复，跨天自动换句。

    run_id 里含 YYYYMMDD 时按当天序号取模（7 天一循环，不会短期撞句）；
    否则回退到稳定哈希，保证同一 run_id 始终同一句。
    """
    import re
    joke = dm_cfg(cfg).get("safety_joke") or {}
    lines = joke.get("lines") or [rc_get(cfg, "joke_fallback", "慢点开，平安到，比什么报销都划算。")]
    seed = str(run_id or date.today().isoformat())
    m = re.search(r"(\d{4})(\d{2})(\d{2})", seed)
    if m:
        try:
            idx = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).toordinal() % len(lines)
            return lines[idx]
        except ValueError:
            pass
    idx = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(lines)
    return lines[idx]


def fmt_amount(v, cfg=None):
    tpl = (dm_cfg(cfg).get("amount_format") if cfg else None) or "¥%.2f"
    try:
        return tpl % float(v or 0)
    except (TypeError, ValueError):
        try:
            return tpl % 0.0
        except Exception:
            return "¥0.00"


def clean_dates(dates):
    """把 YYYY-MM-DD[ HH:MM:SS] 归一成 M-DD，去重保序，绝不编造日期。"""
    out = []
    for v in dates or []:
        s = str(v).strip()
        if not s:
            continue
        if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
            try:
                out.append("%d-%d" % (int(s[5:7]), int(s[8:10])))
            except ValueError:
                continue
        else:
            out.append(s)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def fmt_dates(dates, max_show=None, cfg=None):
    if max_show is None:
        max_show = int(rc_get(cfg, "date_max_show", 4)) if cfg else 4
    ds = clean_dates(dates)
    if not ds:
        return ""
    if len(ds) > max_show:
        return "、".join(ds[:max_show]) + "、等 %d 天" % len(ds)
    return "、".join(ds)


def time_line(cfg, d):
    """只保留一行时间边界（用户旧铁律），不展开解释。"""
    if not dm_cfg(cfg).get("keep_time_boundary_line", True):
        return None
    window, due = d.get("window"), d.get("due")
    if window and due:
        return "统计：%s｜打款截止：%s" % (window, due)
    if window:
        return "统计：%s" % window
    if due:
        return "打款截止：%s" % due
    return None


def issue_line(cfg, issues):
    """异常合并成一行小提示，最多 2 项、40 字，不列文件名/金额/订单号。"""
    dm = dm_cfg(cfg)
    max_items = int(dm.get("issue_max_items") or 2)
    max_chars = int(dm.get("issue_max_chars") or 40)
    if isinstance(issues, str):
        issues = [issues]
    items = [str(x).strip() for x in (issues or []) if str(x).strip()][:max_items]
    if not items:
        return None
    s = "；".join(items)
    if len(s) > max_chars:
        s = s[:max_chars - 1] + "…"
    return rc_get(cfg, "issue_prefix", "小提示：") + s


def _header(d, cfg, title):
    ts = d.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [rc_get(cfg, "header_template", "【报销通知｜%s】%s") % (title, ts)]
    tl = time_line(cfg, d)
    if tl:
        lines.append(tl)
    lines.append("")
    return lines


def _footer(d, cfg, lines, issue):
    il = issue_line(cfg, issue)
    if il:
        lines.append(il)
    lines += ["", pick_joke(cfg, d.get("run_id"))]
    return "\n".join(lines)


def render_group(d, cfg=None):
    """唯一司机通道：物流部群一份总览，只列实际有凭证的司机。

    2026-11-17 00:13 用户定案（第三次收敛，最终口径）：
      群里只保留三样东西——① 本次报销（司机 / 金额 / 日期）
                            ② 确认任务链接（逐笔贴在对应报销下面，不 @ 任何人）
                            ③ 一句提醒开车注意安全的话
      「统计：窗口｜打款截止」那一行、合计、以及「谁可看谁可编辑」等权限说明全部去掉。
      司机很忙，扫一眼就知道自己这笔报了多少、点链接能看任务，剩下的只有安全提醒。

    不再输出「没列到名字的…」零值提示——司机是弹性的，没发图就不必出现在通报里。
    """
    cfg = cfg or load_config()
    rcfg = cfg.get("reporting") or {}
    lines = _header(d, cfg, d.get("group_name") or rc_get(cfg, "default_group_name", "物流部"))
    label = d.get("scope_label") or "本次报销"
    rows = [r for r in (d.get("rows") or []) if float(r.get("amount") or 0) > 0]
    show_task = rcfg.get("show_task_at_in_group") is True
    show_total = rcfg.get("show_total_in_group", False) is True
    mention = rcfg.get("group_task_mention", False) is True
    link_prefix = rcfg.get("group_task_link_prefix") or "任务"
    linked = False
    if rows:
        lines.append("%s：" % label)
        for r in rows:
            amt = fmt_amount(r.get("amount"), cfg)
            ds = fmt_dates(r.get("dates"), cfg=cfg)
            lines.append("· %s %s%s" % (r.get("driver"), amt,
                                        "（%s）" % ds if ds else ""))
            # 逐笔贴链接：金额与任务在同一屏，不用来回找
            url = r.get("url") or ""
            if show_task and url:
                lines.append("　%s：%s" % (link_prefix, url))
                linked = True
        if show_total:
            lines.append("合计 %s" % fmt_amount(sum(float(r.get("amount") or 0)
                                                  for r in rows), cfg))
    else:
        lines.append(d.get("no_change_wording")
                     or dm_cfg(cfg).get("no_change_wording")
                     or rc_get(cfg, "no_change_wording", "暂无新报销"))
    # 行里没带 url（旧调用只传 tasks）时兜底：按 tasks 列链接；默认不 @ 任何人。
    if show_task and not linked:
        for t in (d.get("tasks") or []):
            url = t.get("url") or ""
            if not url:
                continue
            name = t.get("driver") or ""
            open_id = t.get("open_id") or ""
            if mention and name:
                who = ('<at user_id="%s">%s</at>' % (open_id, name)) if open_id else ("@" + name)
                lines.append("%s %s：%s" % (who, link_prefix, url))
            else:
                lines.append("%s：%s" % (link_prefix, url))
    return _footer(d, cfg, lines, d.get("issues"))


def render_personal(d, driver, cfg=None):
    """[DEPRECATED] 逐司机个人版：司机私聊已取消，仅保留渲染能力供兼容，流程绝不调用。"""
    cfg = cfg or load_config()
    lines = _header(d, cfg, driver)
    label = d.get("scope_label") or "本次报销"
    row = next((r for r in (d.get("rows") or [])
                if r.get("driver") == driver), None)
    amount = float((row or {}).get("amount") or 0)
    if amount > 0:
        ds = fmt_dates((row or {}).get("dates"), cfg=cfg)
        lines.append("%s %s%s" % (label, fmt_amount(amount, cfg),
                                  "（%s）" % ds if ds else ""))
    else:
        lines.append(d.get("no_change_wording")
                     or dm_cfg(cfg).get("no_change_wording")
                     or rc_get(cfg, "no_change_wording", "本次没有新的报销，之前记过的都算数。"))
    personal = (d.get("personal_issues") or {}).get(driver)
    if isinstance(personal, str):
        personal = [personal]
    if not personal and row:
        personal = [row.get("issue")] if row.get("issue") else None
    return _footer(d, cfg, lines, personal)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    j = sub.add_parser("joke", help="输出本轮安全驾驶调侃句")
    j.add_argument("--run-id")

    r = sub.add_parser("render", help="渲染极简司机版")
    r.add_argument("--data", required=True, help="数据 JSON 文件")
    r.add_argument("--kind", required=True, choices=["group", "personal"])
    r.add_argument("--driver", help="kind=personal 必填，司机姓名")
    r.add_argument("--out", help="写入文本文件（不传则只输出到 JSON）")
    args = ap.parse_args()

    cfg = load_config()
    if args.cmd == "joke":
        print(json.dumps({"ok": True, "run_id": args.run_id or date.today().isoformat(),
                          "joke": pick_joke(cfg, args.run_id)}, ensure_ascii=False))
        return
    if args.cmd == "render":
        with open(args.data, "r", encoding="utf-8") as f:
            d = json.load(f)
        if args.kind == "personal" and not args.driver:
            ap.error("kind=personal 需 --driver")
        text = (render_group(d, cfg) if args.kind == "group"
                else render_personal(d, args.driver, cfg))
        out = {"ok": True, "kind": args.kind, "driver": args.driver,
               "run_id": d.get("run_id"), "joke": pick_joke(cfg, d.get("run_id")),
               "text": text}
        if args.kind == "personal":
            out["deprecated"] = True
            out["note"] = ("司机私聊通道已取消（config.reporting.per_driver_dm.enabled=false）；"
                           "当前流程只发群内总览 + 负责人专业版，notify.py 会拦截任何个人版 manifest。")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            out["out"] = args.out
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
