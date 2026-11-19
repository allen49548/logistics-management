#!/usr/bin/env python3
"""艾特（@）触发的权限分级 + 调侃回应（2026-11-16 用户定案）。

用户原话（2026-11-16 16:12）：
  1) 负责人艾特机器人，权限是可以处理异常和修改规则；司机艾特只能处理正常报销，
     不能更改规则和修改代码。
  2) 任何人艾特你要先回应一句调侃的收到，再执行任务，回应有温（有温度）。

本脚本把这两条铁律固化成三个可审计子命令，供 Agent 在检测到 @ 后先判定、先回应：

  classify  按发送者判定角色与能力边界（owner=admin / driver=limited / unknown=limited）
  detect    从 lark-cli 拉下来的群消息 JSON 中检测「艾特机器人」事件
  gate      群门槛 + 内容门槛：判断这条 @ 是否落在允许触发的群、是否命中触发词
  ack       渲染「调侃 + 有温度」的收到文案；--send 时按白名单发到主群（先回应，固定本机器人 --as bot，不降级个人身份）

权限分级（mention_policy.roles，config 唯一真源，脚本仅给默认兜底）：
  admin    负责人（owner_open_id / mention_policy.admin.open_ids）：可处理异常、改规则/配置/代码
  limited  司机与未识别者：只能处理正常报销归集；禁止改规则、改代码、处理异常、改配置
  —— 「司机只按既定规则报销」是硬边界；任何改规则/改代码请求，即便司机在群里说，
     也只按正常归集处理，并在回应/负责人私聊里注明「该操作需负责人」，绝不执行。

「先回应再执行」的顺序：detect/classify 之后，必须先 ack（发出调侃收到），
再进入报销归集主流程；ack 幂等键与 run_id 绑定，重跑不重复刷屏。

用法：
  mention.py classify --sender-id ou_xxx [--sender-name 杨海湘]
  mention.py detect --messages msgs.json [--bot-names workbuddy助理 廖浩成's Feishu CLI]
  mention.py ack --role limited --run-id 2026-11-16_1545 [--name 杨海湘] [--out ack.txt]
  mention.py ack --role admin --run-id ... --send --chat-id oc_xxx   # 先回应再执行
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底

_CFG = {}

# —— 默认边界：与 config.mention_policy.roles 一致；config 未配置时仍严守最小权限 ——
_DEFAULT_ROLES = {
    "admin": {
        "label": "负责人",
        "can_handle_exceptions": True,
        "can_change_rules": True,
        "can_change_code": True,
    },
    "limited": {
        "label": "司机/其他",
        "can_handle_exceptions": False,
        "can_change_rules": False,
        "can_change_code": False,
    },
}

# 「先回应」的调侃收到句库（有温度、轻松、不训斥、不用 emoji），按 run_id 日期轮换。
# config.mention_policy.ack.lines.admin / .limited 覆盖；这里是可独立运行的兜底。
_DEFAULT_ACK = {
    "admin": [
        "收到，老板！这事儿我盯住，稳妥给你办了——路上慢点开，安全比什么都重要。",
        "收到！规则这块交给我，改完给你回话；您忙您的，方向盘握稳就行。",
        "收到，领导！异常我来兜底，您放心；高速上悠着点，别让罚单抢了报销的风头。",
        "收到！马上处理，改规则这种精细活儿我来；开慢点，平安到才是省钱王道。",
    ],
    "limited": [
        "收到！票据我这就给你算上，钱的事放心交给我；车开稳点，安全第一哈。",
        "收到啦！马上给你登记报销，别急哈；路上稳着开，人平安比啥都划算。",
        "收到！这就安排上，报销我一条条给你记清楚；慢点开，别抢那几秒钟。",
        "收到！票据收到，马上给你办；钱慢慢到账，车慢慢开，咱图个平平安安。",
    ],
}


def load_config(path=None):
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def roles_cfg(cfg):
    mp = (cfg.get("mention_policy") or {})
    r = mp.get("roles") or {}
    return {
        "admin": {**_DEFAULT_ROLES["admin"], **(r.get("admin") or {})},
        "limited": {**_DEFAULT_ROLES["limited"], **(r.get("limited") or {})},
    }


def _capabilities(role_key, cfg):
    """把角色配置展开成 allowed / denied 动作清单（对外可见、可审计）。"""
    role = roles_cfg(cfg)[role_key]
    allowed = ["normal_reimbursement"]  # 任何人艾特都允许正常报销归集
    denied = []
    if role.get("can_handle_exceptions"):
        allowed.append("handle_exceptions")
    else:
        denied.append("handle_exceptions")
    if role.get("can_change_rules"):
        allowed += ["change_rules", "change_config"]
    else:
        denied += ["change_rules", "change_config"]
    if role.get("can_change_code"):
        allowed.append("change_code")
    else:
        denied.append("change_code")
    return role, allowed, denied


def classify(cfg, sender_id=None, sender_name=None):
    """判定发送者角色与能力边界。owner_open_id 命中 = admin；其余一律 limited。"""
    mp = cfg.get("mention_policy") or {}
    admin_ids = set(mp.get("admin", {}).get("open_ids") or [])
    if not admin_ids:
        # 兜底沿用 config.owner_open_id（唯一负责人）
        owner = cfg.get("owner_open_id")
        if owner:
            admin_ids.add(owner)
    admin_names = set(mp.get("admin", {}).get("names") or [])

    is_admin = bool(sender_id and sender_id in admin_ids) or bool(sender_name and sender_name in admin_names)
    role_key = "admin" if is_admin else "limited"
    role, allowed, denied = _capabilities(role_key, cfg)

    if is_admin:
        matched = "open_id" if (sender_id and sender_id in admin_ids) else "name"
        reason = "负责人（管理员）艾特：可处理异常、改规则/配置/代码"
    elif sender_name:
        matched = "sender_name"
        reason = "非负责人艾特（司机/其他）：仅可正常报销，禁止改规则/改代码/处理异常"
    else:
        matched = "unknown"
        reason = "发送者身份不完整：按最小权限（仅正常报销）处理"

    return {
        "ok": True,
        "role": role_key,
        "label": role.get("label"),
        "sender_id": sender_id,
        "sender_name": sender_name,
        "matched_by": matched,
        "reason": reason,
        "allowed": allowed,
        "denied": denied,
        "capabilities": {k: bool(role.get(k)) for k in
                         ("can_handle_exceptions", "can_change_rules", "can_change_code")},
    }


def _at_text_fields(content):
    """把一条消息的 content 归一成可搜索文本（text / post / 富文本）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = []
        text = content.get("text")
        if isinstance(text, str):
            parts.append(text)
        # post 富文本：递归收集所有 text / 提及的 name / key
        def walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("text", "name", "key", "en_name") and isinstance(v, str):
                        parts.append(v)
                    else:
                        walk(v)
            elif isinstance(obj, list):
                for x in obj:
                    walk(x)
        walk(content)
        return " ".join(parts)
    return ""


def gate(cfg, chat_id, text=None):
    """@ 触发门槛：指定群 + 指定内容才执行任务（2026-11-16 17:12 用户定案）。

    - 群门槛：chat_id 必须精确命中 mention_policy.trigger.allowed_chat_ids，
      其他群一律 gate_ok=false、不得触发。
    - 内容门槛：trigger.content_keywords 为空 = 不限内容（当前口径）；
      非空时 text 命中任一关键词才算通过。
    """
    trig = (cfg.get("mention_policy") or {}).get("trigger") or {}
    enabled = bool(trig.get("enabled", True))
    allowed_ids = set(trig.get("allowed_chat_ids") or [])
    keywords = [k for k in (trig.get("content_keywords") or []) if k]

    if not enabled:
        return {"ok": True, "gate_ok": False, "reason": "trigger 已禁用", "chat_id": chat_id,
                "chat_ok": None, "content_ok": None}
    chat_ok = bool(chat_id) and chat_id in allowed_ids
    if keywords:
        text_l = (text or "").lower()
        content_ok = any(k.lower() in text_l for k in keywords)
    else:
        content_ok = True  # 空关键词 = 不限内容
    gate_ok = chat_ok and content_ok
    reason = ("允许：群命中白名单" if chat_ok else "拒绝：群不在允许列表") + (
        "" if content_ok else "；内容未命中触发词")
    return {"ok": True, "gate_ok": gate_ok, "reason": reason, "chat_id": chat_id,
            "chat_ok": chat_ok, "content_ok": content_ok,
            "allowed_chat_ids": sorted(allowed_ids), "content_keywords": keywords}


def detect(cfg, messages, bot_names=None, bot_open_ids=None, chat_id=None):
    """从 lark-cli 消息列表里检测「艾特机器人」事件，逐条给出角色与建议动作。

    chat_id 传入时会叠加 gate 群/内容门槛，每条 hit 标注 gate_ok；
    不传时 gate_ok=None（仅检测，不判定是否应触发）。
    """
    mp = cfg.get("mention_policy") or {}
    detect_cfg = mp.get("detect") or {}
    names = set(bot_names if bot_names is not None else (detect_cfg.get("bot_names") or []))
    ids = set(bot_open_ids if bot_open_ids is not None else (detect_cfg.get("bot_open_ids") or []))
    # 默认兜底：群里两个机器人的常见名字
    if not names:
        names = {"workbuddy助理", "廖浩成's Feishu CLI"}
    # @ 触发标记：文本里出现 @+bot 名，或 content/mentions 里出现 bot open_id
    hits = []
    for m in messages:
        if m.get("msg_type") == "system" or m.get("deleted"):
            continue
        sender = m.get("sender") or {}
        sid = sender.get("id") or ""
        sname = sender.get("name") or ""
        if sid in ids or sname in names:
            continue  # 机器人自己发的消息不算艾特触发
        text = _at_text_fields(m.get("content"))
        mentioned = []
        for n in names:
            if n and ("@" + n) in text or (n and n in text and "@" in text):
                mentioned.append(n)
        for i in ids:
            if i and i in text:
                mentioned.append(i)
        # 兼容显式 mentions 字段
        for men in (m.get("mentions") or []):
            mid = men.get("id") or men.get("open_id") or ""
            mname = men.get("name") or men.get("key") or ""
            if mid in ids or mname in names:
                mentioned.append(mname or mid)
        if not mentioned:
            continue
        verdict = classify(cfg, sid, sname)
        hit = {
            "message_id": m.get("message_id"),
            "create_time": m.get("create_time"),
            "sender_id": sid,
            "sender_name": sname,
            "mentioned": sorted(set(mentioned)),
            "role": verdict["role"],
            "allowed": verdict["allowed"],
            "denied": verdict["denied"],
            "reason": verdict["reason"],
        }
        if chat_id is not None:
            g = gate(cfg, chat_id, text)
            hit["gate_ok"] = g["gate_ok"]
            hit["gate_reason"] = g["reason"]
        else:
            hit["gate_ok"] = None
        hits.append(hit)
    return {"ok": True, "count": len(hits), "hits": hits}


def pick_ack(cfg, role_key, run_id):
    """按 run_id 里的日期稳定轮换调侃收到句，同一轮同一句。"""
    ack_cfg = (cfg.get("mention_policy") or {}).get("ack") or {}
    lines_cfg = ack_cfg.get("lines") or {}
    pool = lines_cfg.get(role_key) or _DEFAULT_ACK.get(role_key) or _DEFAULT_ACK["limited"]
    if not pool:
        pool = _DEFAULT_ACK["limited"]
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", run_id or "")
    if m:
        idx = (int(m.group(1)) + int(m.group(2)) + int(m.group(3))) % len(pool)
    else:
        idx = (sum(ord(c) for c in (run_id or "")) or 0) % len(pool)
    return pool[idx]


def render_ack(cfg, role_key, run_id, name=None):
    line = pick_ack(cfg, role_key, run_id)
    # 句库可带 {name} 占位；没传名字就去掉占位，避免露出裸花括号
    if "{name}" in line:
        line = line.replace("{name}", name or "")
    prefix = (cfg.get("mention_policy") or {}).get("ack", {}).get("prefix") or ""
    return {
        "ok": True,
        "role": role_key,
        "run_id": run_id,
        "text": (prefix + line) if prefix else line,
    }


def send_group(cfg, text, run_id, chat_id=None, dry_run=False):
    """把 ack 发到主群（白名单精确校验 + 固定本机器人身份 + 幂等键）。

    用户 2026-11-16 16:27 定案：「回复用本机器人回复就行」。「收到」回应固定用
    机器人自身身份（--as bot）发，绝不降级到负责人个人 user 身份。
    身份由 mention_policy.ack.identity 决定，默认且仅 ["bot"]。
    """
    group = chat_id or (cfg.get("chat_discovery") or {}).get("primary_chat_id")
    wl = (cfg.get("chat_whitelist") or {}).get(group)
    if not wl or not wl.get("notify"):
        return {"ok": False, "error": "目标群不在白名单或 notify=false，拒绝发送", "chat_id": group}
    prefix = rc_get(cfg, "idempotency_prefix", "reimb")
    klen = int(rc_get(cfg, "chat_id_key_len", 8))
    key = "%s-ack-%s-%s" % (prefix, run_id, (group or "")[:klen])
    # 固定用本机器人发「收到」；不读通用 fallback，绝不降级到 user 个人身份。
    # 即便配置被误填成 ["bot","user"]，这里也强制只保留 bot。
    ack_cfg = (cfg.get("mention_policy") or {}).get("ack") or {}
    configured = ack_cfg.get("identity") or ["bot"]
    identities = [x for x in configured if x == "bot"] or ["bot"]
    errors = []
    for identity in identities:
        argv = ["lark-cli", "im", "+messages-send", "--as", identity,
                "--chat-id", group, "--text", "-", "--idempotency-key", key, "--format", "json"]
        if dry_run:
            return {"ok": False, "dry_run": True, "argv": argv}
        p = subprocess.run(argv, input=text, text=True, capture_output=True, timeout=60)
        ok = False
        try:
            ok = json.loads(p.stdout.strip().split("\n", 1)[-1]).get("ok") is True
        except (ValueError, IndexError):
            ok = p.returncode == 0 and '"ok": true' in p.stdout
        if ok:
            return {"ok": True, "identity": identity, "chat_id": group}
        errors.append("identity=%s: %s" % (identity, p.stderr.strip()[-200:] or p.stdout.strip()[-200:]))
    return {"ok": False, "error": " | ".join(errors), "chat_id": group}


def main():
    ap = argparse.ArgumentParser(description="艾特触发权限分级 + 调侃回应")
    sub = ap.add_subparsers(dest="cmd")

    p_cls = sub.add_parser("classify", help="判定艾特者角色与能力边界")
    p_cls.add_argument("--sender-id")
    p_cls.add_argument("--sender-name")
    p_cls.add_argument("--config")

    p_det = sub.add_parser("detect", help="从群消息 JSON 检测艾特机器人事件")
    p_det.add_argument("--messages", required=True, help="lark-cli +chat-messages-list --format json 的输出")
    p_det.add_argument("--chat-id", help="消息来源群；传入则叠加群/内容门槛并标注 gate_ok")
    p_det.add_argument("--bot-names", nargs="*")
    p_det.add_argument("--bot-open-ids", nargs="*", help="机器人 bot_open_id（不是 app_id）")
    p_det.add_argument("--config")

    p_gate = sub.add_parser("gate", help="群门槛+内容门槛：这条@是否允许触发任务")
    p_gate.add_argument("--chat-id", required=True, help="消息来源群 chat_id")
    p_gate.add_argument("--text", default="", help="消息正文（内容关键词匹配用）")
    p_gate.add_argument("--config")

    p_ack = sub.add_parser("ack", help="渲染调侃收到文案，可先发群再执行")
    p_ack.add_argument("--role", choices=["admin", "limited"], required=True)
    p_ack.add_argument("--run-id", required=True)
    p_ack.add_argument("--name")
    p_ack.add_argument("--out")
    p_ack.add_argument("--send", action="store_true", help="按白名单发到主群")
    p_ack.add_argument("--chat-id")
    p_ack.add_argument("--dry-run", action="store_true")
    p_ack.add_argument("--config")

    args = ap.parse_args()
    cfg = load_config(args.config)
    global _CFG
    _CFG = cfg

    if args.cmd == "classify":
        result = classify(cfg, args.sender_id, args.sender_name)
    elif args.cmd == "detect":
        with open(args.messages, "r", encoding="utf-8") as f:
            payload = json.load(f)
        msgs = payload.get("data", {}).get("messages", []) if isinstance(payload, dict) else payload
        result = detect(cfg, msgs, args.bot_names, args.bot_open_ids, args.chat_id)
    elif args.cmd == "gate":
        result = gate(cfg, args.chat_id, args.text)
    elif args.cmd == "ack":
        result = render_ack(cfg, args.role, args.run_id, args.name)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(result["text"])
            result["out"] = args.out
        if args.send:
            result["send"] = send_group(cfg, result["text"], args.run_id, args.chat_id, args.dry_run)
    else:
        ap.print_help()
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
