#!/usr/bin/env python3
"""报销确认群任务：真实报销后负责人点击完成任务，形成「已报销/未报销」闭环。

用法：
  group_task.py create --driver 周小毛 --run-id 20261112_0900 \
      [--title "..."] [--amount-total 123.45] [--entry-count 3] \
      [--detail-file /path/to/md] [--entry-ids 1,2,3] [--due-days 3] [--dry-run]
  group_task.py check            # 查询台账中未完成任务状态，已完成则标记已报销
  group_task.py pending          # 列出未真实报销的批次（按任务聚合）

设计：
- 一个司机一轮同步的新入账条目 = 一个批次 = 一个飞书任务（负责人 = config.group_task.assignee_open_id）。
- 任务标题带司机、批次、金额与笔数；描述含逐笔明细。
- 任务完成 → 本脚本 check 命令把该批次台账条目标记 reimbursed=1。
- --idempotency-key = "reimb:{run_id}:{driver}"，同一批次重复执行不会建出第二个任务。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
LEDGER = os.path.join(HERE, "ledger.py")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底

# main() 读取配置后设置；纯函数直接调用时回退原硬编码值
_CFG = {}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def run_cli(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = p.stdout.strip()
    try:
        return p.returncode, json.loads(out) if out else None, p.stderr.strip()
    except json.JSONDecodeError:
        return p.returncode, out, p.stderr.strip()


def _add_driver_viewer(task_id, driver_open_id, as_identity="user"):
    """把报销人作为 follower 加入任务：可查看/评论，但不可编辑核心字段（非负责人）。"""
    if not task_id or not driver_open_id:
        return None
    cmd = [
        "lark-cli", "task", "members", "add", "--as", as_identity,
        "--task-guid", str(task_id),
        "--data", json.dumps({"members": [{"id": driver_open_id, "role": "follower"}]}),
        "--format", "json",
    ]
    code, data, err = run_cli(cmd)
    return {"ok": code == 0, "code": code, "data": data, "error": err}


def _add_to_tasklist(task_id, tasklist_guid, as_identity="user"):
    """把任务挂进指定飞书任务清单（默认「报销」）。
    2026-11-16 22:50 用户口径：每轮报销都必须有一个任务、且要能在清单里看到，
    否则无法确认是否真实报销。任务不归清单时在飞书默认视图里翻不到。"""
    if not task_id or not tasklist_guid:
        return None
    cmd = [
        "lark-cli", "task", "+tasklist-task-add", "--as", as_identity,
        "--tasklist-id", str(tasklist_guid),
        "--task-id", str(task_id),
        "--format", "json",
    ]
    code, data, err = run_cli(cmd)
    return {"ok": code == 0, "code": code, "tasklist_id": tasklist_guid,
            "data": data, "error": err}


def cmd_create(args, cfg):
    gt = cfg.get("group_task", {})
    assignee = args.assignee or gt.get("assignee_open_id") or cfg.get("owner_open_id")
    if not assignee:
        return {"ok": False, "error": "缺少负责人 open_id（config.group_task.assignee_open_id）"}
    due_days = args.due_days if args.due_days is not None else gt.get("due_days", 3)
    due = (date.today() + timedelta(days=due_days)).isoformat()
    amount = ("¥%.2f" % args.amount_total) if args.amount_total is not None else "金额待定"
    title = args.title or "报销确认｜%s %d 笔 %s｜截止 %s" % (
        args.driver, args.entry_count or 0, amount, due)
    assignee_name = gt.get("assignee_name") or "负责人"
    # 2026-11-14 23:53 用户新口径：「任务内容也要精简」。
    # 逐笔明细统一放报表「11月报销明细」sheet 与运行清单，任务里不再重复铺开。
    # --detail-file 保留兼容但不再注入描述（避免任务又被撑成长文）。
    window = "%s ~ %s" % ((args.window_start or "")[:16], (args.window_end or "")[:16]) \
        if (args.window_start and args.window_end) else "-"
    desc_lines = [
        "%s 本批次 %d 笔 %s｜统计窗口（截止）%s" % (
            args.driver, args.entry_count or 0, amount, window),
        "报销确认截止：%s（负责人 %s 打款后点完成；请勿重复打款）" % (due, assignee_name),
        "逐笔明细：报表「11月报销明细」sheet / 运行清单。",
        "权限：负责人可编辑；报销人仅查看与评论，不能点完成。",
    ]
    desc_max = int(gt.get("description_max_chars")
                   or rc_get(_CFG, "description_max_chars", 1000))
    description = "\n".join(desc_lines)[:desc_max]

    cmd = [
        "lark-cli", "task", "+create", "--as", "user",
        "--summary", title,
        "--description", description,
        "--due", due,
        "--assignee", assignee,
        "--idempotency-key", "%s:%s:%s" % (
            rc_get(_CFG, "idempotency_prefix", "reimb"), args.run_id, args.driver),
        "--format", "json",
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    code, data, err = run_cli(cmd)
    result = {"ok": code == 0 and data is not None, "task": data, "stderr": err}
    if not result["ok"]:
        result["error"] = "创建飞书任务失败: %s" % (err or data)
        return result

    # CLI 返回结构兼容：{"data": {"guid":..., "url":..., "status":...}} 或嵌套 task
    if isinstance(data, dict):
        task = data.get("data") if isinstance(data.get("data"), dict) else {}
        inner = task.get("task") if isinstance(task.get("task"), dict) else None
        if inner:
            task = inner
    else:
        task = {}
    task_id = task.get("task_guid") or task.get("guid") or task.get("id") or task.get("task_id")
    task_url = task.get("url") or task.get("applink_url")
    if task_id and not task_url:
        task_url = rc_get(_CFG, "task_url_template",
                          "https://applink.feishu.cn/client/todo?guid=%s") % task_id
    result["task_id"] = task_id
    result["task_url"] = task_url

    if args.entry_ids and task_id and not args.dry_run:
        code2, data2, _ = run_cli([
            sys.executable, LEDGER, "set-task",
            "--ids", args.entry_ids, "--task-id", str(task_id),
        ] + (["--task-url", task_url] if task_url else []))
        result["ledger_bind"] = data2

    # 报销人加为查看人（follower：仅查看/评论，不可编辑）：需要司机 open_id
    # 混合群下按【司机名】解析，不再依赖「一个群 = 一个司机」：
    # 命令行参数 → config.driver_open_ids → driver_roster.members → 旧白名单结构兜底
    roster_members = cfg.get("driver_roster", {}).get("members") or {}
    driver_open_id = (args.driver_open_id
                      or cfg.get("driver_open_ids", {}).get(args.driver)
                      or roster_members.get(args.driver)
                      or next((w.get("driver_open_id") for w in cfg.get("chat_whitelist", {}).values()
                               if w.get("driver") == args.driver), None))
    if driver_open_id and task_id and not args.dry_run:
        result["driver_viewer"] = _add_driver_viewer(task_id, driver_open_id)
    elif not driver_open_id:
        result["driver_viewer"] = {"ok": False, "skipped": True,
                                   "reason": "缺少司机 open_id（config.driver_open_ids[%s] / driver_roster.members 均为空；司机需先进入「物流部」群并跑 driver_roster.py sync，或直接提供 open_id）" % args.driver}

    # 任务必须归进「报销」清单，否则用户在飞书默认视图里找不到（无法确认是否真实报销）
    tasklist_guid = args.tasklist_id or gt.get("tasklist_guid")
    if tasklist_guid and task_id and not args.dry_run:
        result["tasklist"] = _add_to_tasklist(task_id, tasklist_guid)
    elif not tasklist_guid:
        result["tasklist"] = {"ok": False, "skipped": True,
                              "reason": "缺少 config.group_task.tasklist_guid，任务不会归清单"}
    return result


def _task_completed(task_obj):
    """宽容解析任务是否已完成（不同 API 版本字段不同）。
    注意：未完成任务的 completed_at 可能是 "0"（字符串零），必须排除。"""
    if not isinstance(task_obj, dict):
        return False
    for k in ("completed_at", "complete_time", "completed_time"):
        v = task_obj.get(k)
        if v is None or v == "" or v == "0" or v == 0:
            continue
        try:
            if isinstance(v, (int, float)) and v <= 0:
                continue
            if isinstance(v, str) and float(v) <= 0:
                continue
        except (ValueError, TypeError):
            pass
        return True
    st = task_obj.get("status")
    if isinstance(st, str):
        return st.lower() in ("completed", "done", "complete")
    if isinstance(st, dict):
        return str(st.get("status", "")).lower() in ("completed", "done")
    return False


def _notify_completion(cfg, driver, count, amount, task_id, dry_run=False):
    """[默认停用] 真实报销后点完成任务 → 群内 @报销人（2026-11-14 23:53 用户旧口径）。

    2026-11-16 20:08 新口径：已生成任务的数据不再 @ 相关人，通知简洁。
    默认 `group_task.notify_on_complete=false`，本函数直接 skipped、不再群内 @ 任何人。
    保留本函数仅为兼容旧配置（设 true 可恢复）；同一 task_id 幂等，只 @ 一次。
    """
    gt = cfg.get("group_task", {})
    # 2026-11-16 20:08 新口径默认不再 @：已生成任务的数据不再艾特相关人。
    # 默认 False；如需恢复旧行为，把 config.group_task.notify_on_complete 设为 true。
    if not gt.get("notify_on_complete", False):
        return {"ok": False, "skipped": True, "reason": "notify_on_complete=false（默认不再群内@报销人）"}
    chat_id = (cfg.get("chat_discovery", {}).get("primary_chat_id")
               or next((k for k, v in cfg.get("chat_whitelist", {}).items()
                        if v.get("notify")), None))
    if not chat_id:
        return {"ok": False, "error": "找不到主群 chat_id"}
    store_path = os.path.join(cfg["state_dir"], "task_completion_notified.json")
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        store = []
    if task_id in store:
        return {"ok": True, "skipped": True, "reason": "该任务已通知过（幂等）"}
    open_id = (cfg.get("driver_open_ids", {}).get(driver)
               or (cfg.get("driver_roster", {}).get("members") or {}).get(driver))
    at = ('<at user_id="%s">%s</at>' % (open_id, driver)) if open_id else ("@" + driver)
    text = "%s 你的报销 %d 笔 ¥%.2f 已打款，确认任务已完成，不用再重复发凭证。" % (
        at, count, amount)
    key = ("reimb-done-%s-%s" % (str(task_id)[:20], driver))[:50]
    cmd = ["lark-cli", "im", "+messages-send", "--as", "bot",
           "--chat-id", chat_id, "--text", "-", "--idempotency-key", key, "--format", "json"]
    if dry_run:
        return {"ok": True, "dry_run": True, "argv": cmd, "text": text}
    try:
        p = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=60)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        ok = json.loads(p.stdout.strip().split("\n", 1)[-1]).get("ok") is True
    except Exception:
        ok = p.returncode == 0 and '"ok": true' in p.stdout
    if ok:
        store.append(task_id)
        with open(store_path, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    return {"ok": ok, "chat_id": chat_id, "at_native": bool(open_id),
            "stdout": p.stdout.strip()[-300:], "stderr": p.stderr.strip()[-200:]}


def cmd_check(args, cfg):
    """轮询台账中 task_status='pending' 的任务；完成的批次标记已报销（默认不再群内 @）。"""
    code, pending, _ = run_cli([sys.executable, LEDGER, "unreimbursed"])
    if not pending or not pending.get("ok"):
        return {"ok": False, "error": "读取台账失败", "raw": pending}
    tasks = {}
    for e in pending.get("entries", []):
        tid = e.get("task_id")
        if tid:
            tasks.setdefault(tid, []).append(e)
    confirmed, still, errors = [], [], []
    for tid, entries in tasks.items():
        code, data, err = run_cli([
            "lark-cli", "task", "tasks", "get", "--as", "user",
            "--task-guid", tid, "--format", "json",
        ])
        if code != 0 or not isinstance(data, dict):
            errors.append({"task_id": tid, "error": err or "查询失败"})
            continue
        task_obj = data.get("data", {}).get("task") or data.get("task") or data.get("data", {})
        if _task_completed(task_obj):
            run_cli([sys.executable, LEDGER, "mark-reimbursed", "--task-id", tid])
            item = {
                "task_id": tid, "driver": entries[0].get("driver"),
                "count": len(entries), "amount": round(sum(x.get("amount") or 0 for x in entries), 2),
            }
            if not getattr(args, "no_notify", False):
                item["completion_notice"] = _notify_completion(
                    cfg, item["driver"], item["count"], item["amount"], tid,
                    dry_run=bool(getattr(args, "dry_run", False)))
            confirmed.append(item)
        else:
            still.append({"task_id": tid, "driver": entries[0].get("driver"), "count": len(entries)})
    # 无任务绑定的条目（早期数据或建任务失败）单独列出
    no_task = [e for e in pending.get("entries", []) if not e.get("task_id")]
    return {
        "ok": True, "confirmed_this_run": confirmed,
        "awaiting_completion": still, "no_task_bound": {
            "count": len(no_task),
            "amount": round(sum(x.get("amount") or 0 for x in no_task), 2),
        }, "query_errors": errors,
    }


def cmd_pending(args, cfg):
    code, pending, _ = run_cli([sys.executable, LEDGER, "unreimbursed"])
    if not pending or not pending.get("ok"):
        return {"ok": False, "error": "读取台账失败"}
    by_task = {}
    for e in pending.get("entries", []):
        tid = e.get("task_id") or "(未绑定任务)"
        by_task.setdefault(tid, {"task_id": tid, "task_url": e.get("task_url"),
                                 "driver": e.get("driver"), "entries": [], "amount": 0.0})
        by_task[tid]["entries"].append({"id": e["id"], "date": e.get("receipt_date"),
                                        "amount": e.get("amount")})
        by_task[tid]["amount"] = round(by_task[tid]["amount"] + (e.get("amount") or 0), 2)
    return {"ok": True, "batches": list(by_task.values()),
            "total_unreimbursed": round(sum(b["amount"] for b in by_task.values()), 2)}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--driver", required=True)
    c.add_argument("--run-id", required=True)
    c.add_argument("--title")
    c.add_argument("--amount-total", type=float)
    c.add_argument("--entry-count", type=int)
    c.add_argument("--detail-file")
    c.add_argument("--entry-ids", help="逗号分隔的台账 id，绑定任务")
    c.add_argument("--assignee", help="覆盖负责人 open_id")
    c.add_argument("--due-days", type=int)
    c.add_argument("--driver-open-id", help="报销人 open_id，加为任务查看人（仅查看/评论）")
    c.add_argument("--window-start", help="统计窗口起始时间（ISO），写入任务描述")
    c.add_argument("--window-end", help="统计窗口截止时间=本次运行时间（ISO），写入任务描述")
    c.add_argument("--window-days", type=int, help="统计窗口天数，用于描述")
    c.add_argument("--tasklist-id", help="飞书任务清单 guid，默认取 config.group_task.tasklist_guid（报销清单）")
    c.add_argument("--dry-run", action="store_true")
    ck = sub.add_parser("check")
    ck.add_argument("--no-notify", action="store_true", help="轮询完成状态但不在群内 @（默认已不 @，此参数保留兼容）")
    ck.add_argument("--dry-run", action="store_true", help="只生成 @ 文案，不真发")
    sub.add_parser("pending")
    args = p.parse_args()
    cfg = load_config()
    global _CFG
    _CFG = cfg
    out = {"create": cmd_create, "check": cmd_check, "pending": cmd_pending}[args.cmd](args, cfg)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
