#!/usr/bin/env python3
"""飞书机器人配置自检（所有 skill 通用，自包含、零第三方依赖）。

用途：任何 skill 在"发飞书通知之前"调用本脚本，检测通知机器人是否配置妥当。
若未配置，返回 bot_setup_required=true 和一段可直接转述给用户的提醒 + 教程，
绝不静默、绝不冒充个人身份偷偷发送。

检测三项：
  1. lark-cli 是否可用（在 PATH）
  2. 机器人 bot 身份是否已登录授权（auth status → Bot identity: ready）
  3. 若传了 --chat-id，检测机器人是否在目标群里

用法：
    python3 bot_check.py
    python3 bot_check.py --chat-id oc_xxx --chat-id oc_yyy
    python3 bot_check.py --chat-id oc_xxx --json        # 机器可读
    python3 bot_check.py --quiet                        # 只返回退出码，不打印

退出码：0 = 配置正常；2 = 未配置（bot_setup_required=true）；1 = 检测本身出错
输出（默认人类可读；--json 为结构化）：
    {"bot_ready": bool, "cli_ready": bool, "groups_ok": {...},
     "bot_setup_required": bool, "issues": [...], "reminder": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

TUTORIAL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "飞书机器人配置教程.md"))
BOT_NAME = "廖浩成's Feishu CLI"
BOT_APP_ID = "cli_aa7fca0a72ba9bb4"

# 精简版教程（内嵌，保证即使公共教程文件缺失也能给用户完整指引）
TUTORIAL = """【飞书机器人配置教程（通用）】
统一通知机器人：廖浩成's Feishu CLI（cli_aa7fca0a72ba9bb4），所有通知都靠它发。

第 1 步 确认 lark-cli 可用
  方式 A：把 lark-cli 放进 PATH 后运行 `lark-cli --help`
  方式 B：设置 LOGISTICS_LARK_CLI=/absolute/path/to/lark-cli 或 LARK_CLI=...
  WorkBuddy 用户也可启用「飞书」连接器，其 CLI 会自动作为兜底路径。

第 2 步 机器人授权登录（最常见原因，token 会过期）
  lark-cli auth login          # 按提示扫码/确认授权
  lark-cli auth status         # 看到 bot → Bot identity: ready 即成功
  报 unauthorized / no permission → 基本就是这步，重新 auth login。

第 3 步 把机器人拉进目标群（群消息失败/降级私聊多半是这个）
  飞书群 → 群设置 → 添加机器人 → 搜索「廖浩成's Feishu CLI」并添加。
  lark-cli im +chat-list --as bot --page-all   # 能看到目标群即成功

第 4 步 核对群 chat_id 配置（oc_ 开头）
  报销→modules/reimbursement/config.json 的 chat_whitelist(notify:true)；
  司机工资→modules/driver-wage/config/local/payroll.json 的 notify。
  lark-cli im +chat-list --as user   # 查群 id；新群需补配置并设 notify:true

第 5 步 重新运行工具，不再报"机器人未配置"即成功。

完整教程：modules/driver-wage/飞书机器人配置教程.md"""


def _run(argv, timeout=20):
    try:
        p = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _find_lark():
    # 通用优先：显式环境变量 > PATH > WorkBuddy 连接器兜底路径
    for env_name in ("LOGISTICS_LARK_CLI", "LARK_CLI"):
        configured = os.environ.get(env_name, "").strip()
        if configured:
            candidate = os.path.expanduser(configured)
            if os.path.exists(candidate):
                return candidate
    exe = shutil.which("lark-cli")
    if exe:
        return exe
    p = os.path.expanduser("~/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli")
    if os.path.exists(p):
        return p
    return None


def _bot_ready(lark):
    """返回 (ready: bool, detail: str)。"""
    rc, out, err = _run([lark, "auth", "status"])
    blob = (out or "") + "\n" + (err or "")
    if "Bot identity: ready" in blob:
        return True, "bot identity ready"
    # 结构化输出兜底
    try:
        data = json.loads(out)
        bot = ((data.get("identities") or {}).get("bot") or {})
        msg = bot.get("message", "")
        if "ready" in str(msg).lower():
            return True, msg
    except Exception:
        pass
    return False, (err or out or "无法读取 auth status")[:300]


def check(group_ids=None) -> dict:
    """检测机器人配置。

    group_ids: 需要检测的「群」chat_id 列表（仅群，不要传 p2p 私聊）。
               p2p 私聊不参与群成员检测（bot 侧 chat-list 不列出 p2p，会误报）。
    """
    group_ids = [c for c in (group_ids or []) if c]
    issues = []
    groups_ok = {}

    lark = _find_lark()
    cli_ready = bool(lark)
    if not cli_ready:
        issues.append("① lark-cli 不可用——请放进 PATH，或设置 LOGISTICS_LARK_CLI / LARK_CLI；WorkBuddy 用户可启用「飞书」连接器")
        result = {
            "bot_ready": False, "cli_ready": False, "groups_ok": groups_ok,
            "bot_setup_required": True, "issues": issues,
            "reminder": build_reminder(issues),
        }
        return result

    bot_ready, detail = _bot_ready(lark)
    if not bot_ready:
        issues.append("② 机器人未登录/未授权（%s）——需运行 lark-cli auth login 重新授权" % detail)

    if group_ids:
        rc, out, err = _run([lark, "im", "+chat-list", "--as", "bot", "--types", "group", "--page-all"])
        present = set()
        try:
            data = json.loads(out)
            items = (data.get("data") or {}).get("chats") or (data.get("data") or {}).get("items") or []
            present = {c.get("chat_id") for c in items if isinstance(c, dict)}
        except Exception:
            pass
        listed = rc == 0 and bool(present)
        for cid in group_ids:
            in_group = cid in present if listed else None  # None = 无法确定，不误报
            groups_ok[cid] = in_group
            if listed and not in_group:
                issues.append("③ 机器人不在目标群 %s——需在飞书把「%s」拉进该群" % (cid, BOT_NAME))

    setup_required = bool(issues)
    return {
        "bot_ready": bot_ready,
        "cli_ready": cli_ready,
        "groups_ok": groups_ok,
        "bot_setup_required": setup_required,
        "issues": issues,
        "reminder": build_reminder(issues) if setup_required else "",
    }


def build_reminder(issues):
    head = "⚠️ 飞书机器人未配置好，通知可能无法发送（本轮未静默、未用个人身份冒充）。\n\n问题：\n"
    body = "\n".join("  - " + x for x in issues)
    return head + body + "\n\n" + TUTORIAL


def main(argv=None):
    ap = argparse.ArgumentParser(description="飞书机器人配置自检")
    ap.add_argument("--chat-id", action="append", default=[], help="目标「群」chat_id（勿传 p2p 私聊），可多次传入")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    ap.add_argument("--quiet", action="store_true", help="不打印，只返回退出码")
    args = ap.parse_args(argv)

    result = check(args.chat_id)
    if not args.quiet:
        if args.json or result["bot_setup_required"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("✅ 飞书机器人配置正常（lark-cli 可用、bot 已授权%s）。" %
                  ("、已在目标群" if result["groups_ok"] else ""))
    if result["bot_setup_required"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
