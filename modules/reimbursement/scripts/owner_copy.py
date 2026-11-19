#!/usr/bin/env python3
"""负责人私聊专业版·精简渲染（2026-11-14 23:53 用户新口径）。

用户原话：「给我私聊的内容太多了，精简一点」。

保留（审计必需，只在本通道）：本轮入账按司机金额、重复提交逐项金额+原登记时间+
是否已报销+任务链接、异常与待办、确认任务链接、台账四态、清单/备份路径。
删掉：冗长的口径说明、权限/统计/通报三行套话、29 笔逐笔图片文件名与商户、
重复的完整任务链接、逐节「小计」、停用群说明等模板填充。
逐笔图片级明细仍完整保存在 state/runs/<run>.md 与报表「11月报销明细」sheet。

用法：
  python3 scripts/owner_copy.py render --data <data.json> --out <owner.txt>

data.json 字段（全部可选，缺什么不写什么）：
{
  "run_id":"2026-11-14_2353", "now":"2026-11-14 23:53",
  "window":"10-31 23:53 ~ 11-14 23:53", "due":"11-17",
  "new_entries":[{"driver":"王博成","count":32,"amount":1737.06,
                  "dates":["11-11","11-14"],"report_file":"王博成2026.xlsx",
                  "sheet":"11月报销明细","rows":32,"backup":"state/backups/..."}],
  "duplicates":[{"driver":"贾燕斌","count":20,"amount":742.88,
                 "layer":"L1","status":"已报销","created_at":"11-14 21:35",
                 "task_url":"https://...","items":[{"amount":14.0,"tail":"582172"}]}],
  "exceptions":["..."], "manual":["..."],
  "tasks":[{"driver":"王博成","url":"https://...","due":"11-17","follower":"ok"}],
  "ledger":{"total_count":59,"total_amount":2689.14,"reimbursed_count":27,
            "reimbursed_amount":952.08,"pending_count":32,"pending_amount":1737.06,
            "voided_count":1,"path":"state/ledger.sqlite"},
  "paths":{"run_list":"state/runs/...md"},
  "send":{"group":"sent","owner":"owner_dm"}
}
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底

# render(d, cfg) / main() 设置；纯函数直接调用时回退原硬编码值
_CFG = {}


def money(v):
    tpl = ((_CFG.get("driver_message") or {}).get("amount_format")
           or "¥%.2f")
    try:
        return tpl % float(v or 0)
    except (TypeError, ValueError):
        try:
            return tpl % 0.0
        except Exception:
            return "¥0.00"


def render(d, cfg=None):
    global _CFG
    _CFG = cfg or {}
    L = []
    a = L.append
    a("【报销归集·负责人】%s（run %s）" % (d.get("now") or "", d.get("run_id") or ""))
    window_days = rc_get(_CFG, "run_log_window_days", 14)
    a("窗口 %s（%s 天）｜确认截止 %s" % (d.get("window") or "-", window_days,
                                     d.get("due") or "-"))
    a("")

    news = d.get("new_entries") or []
    total_new = sum(float(x.get("amount") or 0) for x in news)
    count_new = sum(int(x.get("count") or 0) for x in news)
    a("新增 %d 笔 %s" % (count_new, money(total_new)))
    if news:
        for x in news:
            ds = x.get("dates") or []
            dt = ("%s~%s" % (ds[0], ds[-1])) if len(ds) > 1 else (ds[0] if ds else "")
            a("· %s %d 笔 %s%s → %s「%s」%s 行%s" % (
                x.get("driver"), int(x.get("count") or 0), money(x.get("amount")),
                ("（%s）" % dt) if dt else "",
                x.get("report_file") or "报表", x.get("sheet") or "11月报销明细",
                x.get("rows") if x.get("rows") is not None else x.get("count"),
                ("；备份 %s" % x["backup"]) if x.get("backup") else ""))
    else:
        a("· 无（本轮零入账）")
    a("")

    dups = d.get("duplicates") or []
    if dups:
        td = sum(float(x.get("amount") or 0) for x in dups)
        td_count = sum(int(x.get("count") or 0) for x in dups)
        # 2026-11-16 20:21 用户新口径：重复提交的旧凭证不再逐笔推送，只一行极简汇总。
        # 逐项明细（金额/订单号/原登记时间/任务链接）只进运行清单 state/runs/*.md 供审计。
        a("⚠️ 重复提交·未重复计算 %d 笔 %s（本次未加钱，逐项详见运行清单）" % (td_count, money(td)))
        if (cfg or {}).get("owner_message", {}).get("show_duplicate_detail") is True:
            for x in dups:
                a("· %s：%s 命中，%d 笔 %s，原登记均 %s，当前全部「%s」" % (
                    x.get("driver"), x.get("layer") or "四层", int(x.get("count") or 0),
                    money(x.get("amount")), x.get("created_at") or "-", x.get("status") or "待报销"))
                items = x.get("items") or []
                if items:
                    # 逐项金额 + 订单号尾号：铁律要求逐项可核，但压成紧凑行，不再一行一笔带长链接
                    parts = []
                    for it in items:
                        tail = it.get("tail") or ""
                        parts.append("%s(%s)" % (money(it.get("amount")), tail) if tail
                                     else money(it.get("amount")))
                    # 按「项」边界折行，绝不把单个金额或订单号尾号截断到两行
                    cur = ""
                    for item in parts:
                        if cur and len(cur) + 1 + len(item) > 200:
                            a("  " + cur)
                            cur = item
                        else:
                            cur = (cur + " " + item) if cur else item
                    if cur:
                        a("  " + cur)
                if x.get("task_url"):
                    a("  对应任务 %s" % x["task_url"])
        a("")

    if d.get("exceptions") or d.get("manual"):
        a("异常 / 待办")
        for s in (d.get("exceptions") or []) + (d.get("manual") or []):
            a("· " + str(s))
        a("")

    tasks = d.get("tasks") or []
    # 2026-11-16 20:08：不写「谁可看谁可编辑」等权限说明。
    # 2026-11-17 00:12 用户反馈「王博成第二笔没有任务链接」：负责人私聊必须带任务链接（去点完成用），
    # 但只保留「司机 + 链接 + 截止」一行，不加权限措辞；群里仍不贴（reporting.show_task_at_in_group=false）。
    om = (cfg or {}).get("owner_message", {}) or {}
    show_task = om.get("show_task_link")
    if show_task is None:
        show_task = True  # 默认展示；要隐藏显式设 false
    if tasks and show_task:
        a("确认任务")
        # 同一司机多批（多笔任务）时加序号，避免"第二笔不知道是哪条"
        counter = {}
        for t in tasks:
            counter[t.get("driver")] = counter.get(t.get("driver"), 0) + 1
        seen = {}
        for t in tasks:
            driver = t.get("driver") or ""
            url = t.get("url") or ""
            due = t.get("due") or ""
            seen[driver] = seen.get(driver, 0) + 1
            label = driver
            if counter[driver] > 1:
                label = "%s 第%d批" % (driver, seen[driver])
            line = "· %s %s" % (label, url or "（链接待补）")
            if due:
                line += " 截止 %s" % due
            a(line)
        a("")

    led = d.get("ledger") or {}
    if led:
        a("台账 %d 笔 %s（已报销 %d 笔 %s / 待打款 %d 笔 %s）%s" % (
            int(led.get("total_count") or 0), money(led.get("total_amount")),
            int(led.get("reimbursed_count") or 0), money(led.get("reimbursed_amount")),
            int(led.get("pending_count") or 0), money(led.get("pending_amount")),
            "｜作废 %d 笔" % led["voided_count"] if led.get("voided_count") else ""))
        if led.get("path"):
            a("台账 %s" % led["path"])
    if (d.get("paths") or {}).get("run_list"):
        a("清单 %s（逐笔图片明细与两层通报原文在清单）" % d["paths"]["run_list"])
    snd = d.get("send") or {}
    if snd:
        a("通报 群内 %s / 本条 owner_dm" % snd.get("group", "-"))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["render"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()
    with open(args.data, "r", encoding="utf-8") as f:
        d = json.load(f)
    cfg_path = os.path.abspath(os.path.join(HERE, "..", "config.json"))
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    text = render(d, cfg)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(json.dumps({"ok": True, "lines": len(text.splitlines()),
                      "chars": len(text), "out": args.out, "text": text},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
