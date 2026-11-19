#!/usr/bin/env python3
"""按白名单精确校验后发送飞书通报，失败按身份降级，绝不发错群 / 发错人。

两层通报（2026-11-14 用户定案；金额不保密；司机弹性、只统计发了图片的司机）：
  通道一 物流部群（--chat-id 白名单精确校验，--text / --text-file 传「群内总览」）
          一份极简总览：只列本轮实际有付款凭证的司机，按司机一行「姓名 ¥金额（日期）」
          ＋合计＋一行小提示＋一句安全驾驶调侃。
  通道二 负责人私聊（固定 owner_dm，--owner-text / --owner-text-file 传「专业完整版」）
          订单号尾号、原登记时间、任务链接、台账/清单路径、未写报表原因等全部明细。

**司机私聊已整体取消**（config.reporting.per_driver_dm.enabled=false）：
  为兼容可能仍带 --driver-dm-manifest 的旧自动化 prompt，本脚本会做硬拦截——
  返回 blocked=true，绝不调用 --user-id 发任何私聊，也不产生 missing_driver_dm 待办名单。
  司机是弹性的，不要求全员进群、不要求 open_id 补齐，缺 open_id 不再是异常或催办。

用法（一次调用同时发出两层）：
  notify.py --chat-id oc_xxx --text-file /tmp/overview.txt \
            --owner-text-file /tmp/owner.txt --run-id 20261114_0915
  notify.py --chat-id oc_xxx --text "..." --owner-text "..." --run-id ... --dry-run
  notify.py --owner-only --owner-text-file /tmp/monthly_owner.txt --run-id ...
  notify.py --chat-id oc_xxx --skip-group --owner-text-file /tmp/owner.txt --run-id ...
    （本轮无新数据：不发群，只发负责人私聊；group.skipped=true 记入清单）

输出 JSON：{"results": {"group": {...}, "driver_dm": {...}, "owner": {...}},
           "missing_driver_dm": [], "errors": [...]}
（driver_dm 恒为 blocked / 未提供，仅为兼容旧调用方保留字段。）
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# 由 main() 在读取配置后设置；缺省 {} 时全部回退原硬编码值，保证纯函数直接调用行为不变
_CFG = {}
# 由 main() 的 bot_check 自检结果设置；_emit 输出时统一注入，供会话转述给用户
_BOT_SETUP = None


def _emit(obj):
    """统一输出：附带飞书机器人配置自检结果（未配置时含提醒 + 教程）。"""
    global _BOT_SETUP
    if _BOT_SETUP is not None:
        obj = dict(obj)
        obj["bot_setup"] = _BOT_SETUP
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def call_send(chat_id, text, identity, run_id, dry_run=False):
    prefix = rc_get(_CFG, "idempotency_prefix", "reimb")
    klen = int(rc_get(_CFG, "chat_id_key_len", 8))
    key = "%s-%s-%s" % (prefix, run_id, chat_id[:klen])
    argv = ["lark-cli", "im", "+messages-send",
            "--as", identity,
            "--chat-id", chat_id,
            "--text", "-",
            "--idempotency-key", key,
            "--format", "json"]
    return _run_send(argv, text, dry_run)


def call_send_user(open_id, text, identity, key, dry_run=False):
    argv = ["lark-cli", "im", "+messages-send",
            "--as", identity,
            "--user-id", open_id,
            "--text", "-",
            "--idempotency-key", key[:int(rc_get(_CFG, "idempotency_max_len", 50))],
            "--format", "json"]
    return _run_send(argv, text, dry_run)


def _run_send(argv, text, dry_run):
    if dry_run:
        return {"ok": False, "dry_run": True, "argv": argv}
    try:
        p = subprocess.run(argv, input=text, text=True, capture_output=True, timeout=60)
        stdout = p.stdout.strip()
        # lark-cli 成功信封是 ok==true；不要把老式 code==0 当成功
        ok = False
        try:
            parsed = json.loads(stdout.split("\n", 1)[-1])
            ok = parsed.get("ok") is True
        except (ValueError, IndexError):
            ok = p.returncode == 0 and '"ok": true' in stdout
        return {"ok": ok, "returncode": p.returncode,
                "stdout": stdout[-800:], "stderr": p.stderr.strip()[-400:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_with_fallback(chat_id, text, run_id, dry_run):
    """bot -> user 顺序降级，每级只试一次。返回 (result, errors)。"""
    errors = []
    for identity in rc_get(_CFG, "lark_identity_fallback", ["bot", "user"]):
        res = call_send(chat_id, text, identity, run_id, dry_run)
        if res.get("dry_run"):
            return {"ok": False, "dry_run": True, "argv": res["argv"]}, errors
        if res.get("ok"):
            return {"ok": True, "identity": identity}, errors
        errors.append("identity=%s: %s" % (identity, res.get("error") or res.get("stderr") or res.get("stdout")))
    return {"ok": False}, errors


def roster_names(cfg):
    """司机白名单：名册内才允许私聊，绝不发给陌生人。"""
    names = set((cfg.get("driver_open_ids") or {}).keys())
    roster = cfg.get("driver_roster") or {}
    names |= set((roster.get("members") or {}).keys())
    names |= set(roster.get("extra_drivers") or [])
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from driver_roster import all_drivers
        from datetime import datetime
        names |= set(all_drivers(cfg, datetime.now().year))
    except Exception:
        pass
    return names


def resolve_driver_open_id(cfg, driver):
    ids = cfg.get("driver_open_ids") or {}
    members = (cfg.get("driver_roster") or {}).get("members") or {}
    return ids.get(driver) or members.get(driver) or ""


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("drivers") or []
    return []


def send_driver_dms(cfg, manifest_path, run_id, dry_run):
    """已停用的逐司机私聊通道：配置禁用时硬拦截，绝不发出任何 --user-id 私聊。"""
    pcfg = (cfg.get("reporting") or {}).get("per_driver_dm") or {}
    if pcfg.get("enabled") is False:
        return {
            "enabled": False, "blocked": True, "sent": [], "skipped": [], "failed": [],
            "reason": "用户 2026-11-14 明确『不用发私聊给司机，发群里就好』；"
                      "config.reporting.per_driver_dm.enabled=false，已拦截 --driver-dm-manifest，"
                      "未发任何司机私聊，也不生成 missing_driver_dm 待办名单。",
        }
    try:
        items = load_manifest(manifest_path)
    except Exception as e:
        return {"enabled": True, "sent": [], "skipped": [],
                "failed": [{"driver": None, "reason": "manifest 读取失败：%s" % e}]}

    names = roster_names(cfg)
    max_n = int(pcfg.get("max_drivers_per_run") or 50)
    if len(items) > max_n:
        items = items[:max_n]
    out = {"enabled": True, "dry_run": bool(dry_run),
           "sent": [], "skipped": [], "failed": []}
    for item in items:
        driver = str(item.get("driver") or "").strip()
        text = item.get("text") or ""
        if item.get("file"):
            try:
                with open(item["file"], "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception as e:
                out["failed"].append({"driver": driver, "reason": "个人账本文件读取失败：%s" % e})
                continue
        if not driver or driver not in names:
            out["failed"].append({"driver": driver, "reason": "司机不在名册，拒绝发送（防发错人）"})
            continue
        if not text.strip():
            out["skipped"].append({"driver": driver, "reason": "个人账本内容为空"})
            continue
        open_id = resolve_driver_open_id(cfg, driver)
        if not open_id:
            out["skipped"].append({"driver": driver, "reason": "缺 open_id：跳过私聊并记原因（司机进群后 sync 自动补）"})
            continue
        prefix = rc_get(_CFG, "open_id_prefix", "ou_")
        if not open_id.startswith(prefix):
            out["failed"].append({"driver": driver, "reason": "open_id 格式非 %s 开头，拒绝发送（疑似填成 chat_id）" % prefix})
            continue

        key = "%s-dm:%s:%s" % (rc_get(_CFG, "idempotency_prefix", "reimb"), run_id, driver)
        errs = []
        done = False
        for identity in (pcfg.get("identity_order")
                         or rc_get(_CFG, "lark_identity_fallback", ["bot", "user"])):
            res = call_send_user(open_id, text, identity, key, dry_run)
            if res.get("dry_run"):
                out["sent"].append({"driver": driver, "dry_run": True,
                                    "would_run": res["argv"]})
                done = True
                break
            if res.get("ok"):
                out["sent"].append({"driver": driver, "identity": identity,
                                    "open_id_prefix": open_id[:8]})
                done = True
                break
            errs.append("identity=%s: %s" % (identity,
                        res.get("error") or res.get("stderr") or res.get("stdout")))
        if not done:
            out["failed"].append({"driver": driver, "reason": "两种身份均发送失败", "errors": errs})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=False)
    ap.add_argument("--text", help="群内全队总览内容（兼容旧调用方式）")
    ap.add_argument("--text-file", help="群内全队总览内容文件")
    ap.add_argument("--owner-text", help="负责人私聊专业版内容")
    ap.add_argument("--owner-text-file", help="负责人私聊专业版内容文件")
    ap.add_argument("--driver-dm-manifest",
                    help="逐司机个人账本清单 JSON：[{driver, file|text}, ...]")
    ap.add_argument("--owner-only", action="store_true",
                    help="仅发送给负责人私聊（月报合计用）：跳过白名单校验，不发群、不发个人账本")
    ap.add_argument("--skip-group", action="store_true",
                    help="[旧规则] 本轮无新数据时跳过群消息、只发负责人私聊（仍记录 group.skipped 便于审计）；"
                         "与 --owner-only 的区别：本参数保留群白名单上下文与跳过原因，供清单核对")
    ap.add_argument("--no-new", action="store_true",
                    help="[2026-11-16 20:08 新规则] 本轮无新报销：群内只发一句极简『暂无新报销』、"
                         "不发负责人私聊长文。司机很忙没时间看，只在有新数据时才发详细通报。"
                         "群内文案取 config.reporting.no_new_data_wording")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    global _CFG
    _CFG = cfg

    # ---- 飞书机器人配置自检（2026-11-16 用户定案）----
    # 未配置时不静默、不冒充个人身份：在输出里带醒目提醒 + 教程，交给会话转述给用户。
    global _BOT_SETUP
    try:
        import bot_check
        # 只传「群」id 做在群检测；owner_dm 是 p2p 私聊，不参与群检测（会误报）
        _BOT_SETUP = bot_check.check([args.chat_id] if args.chat_id else [])
    except Exception as _e:  # 检测本身失败不阻断发送
        _BOT_SETUP = {"bot_setup_required": False, "check_error": str(_e)[:200]}

    group_text = args.text
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            group_text = f.read()
    owner_text = args.owner_text
    if args.owner_text_file:
        with open(args.owner_text_file, "r", encoding="utf-8") as f:
            owner_text = f.read()

    # ---- 仅发负责人私聊模式（月报合计：所有金额只发一次给负责人） ----
    if args.owner_only:
        results = {}
        errors = []
        if owner_text and owner_text.strip():
            owner_dm = cfg.get("owner_dm_chat_id") or cfg.get("fallback_dm_chat_id")
            if not owner_dm:
                results["owner"] = {"sent": False, "channel": "none",
                                    "error": "config 缺少 owner_dm_chat_id / fallback_dm_chat_id"}
            else:
                res, oerr = send_with_fallback(owner_dm, owner_text, args.run_id, args.dry_run)
                if res.get("dry_run"):
                    results["owner"] = {"sent": False, "dry_run": True, "would_run": res["argv"]}
                elif res.get("ok"):
                    results["owner"] = {"sent": True, "channel": "owner_dm", "identity": res["identity"],
                                        "chat_id": owner_dm}
                else:
                    errors.append("owner_dm: %s" % (" | ".join(oerr)[:300]))
                    results["owner"] = {"sent": False, "channel": "none", "errors": oerr}
        else:
            results["owner"] = {"sent": False, "channel": "none", "note": "未提供负责人专业版内容"}
        out = {"results": results}
        if errors:
            out["errors"] = errors
        _emit(out)
        return

    results = {}
    errors = []

    if args.no_new:
        # ---- 2026-11-16 20:08 新规则：无新报销，群里只发一句极简提示，不发私聊长文 ----
        # 仍需白名单精确校验（防串群）
        entry = (cfg.get("chat_whitelist", {}) or {}).get(args.chat_id)
        if not args.chat_id or entry is None or entry.get("notify") is not True:
            print(json.dumps({"results": {"group": {"sent": False, "channel": "none",
                              "error": "拒绝发送：无新报销提示也需命中白名单 notify=true 的群",
                              "chat_id": args.chat_id}}}, ensure_ascii=False, indent=2))
            sys.exit(2)
        wording = (cfg.get("reporting") or {}).get("no_new_data_wording") or "暂无新报销"
        res, gerr = send_with_fallback(args.chat_id, wording, args.run_id, args.dry_run)
        if res.get("dry_run"):
            results["group"] = {"sent": False, "dry_run": True, "would_run": res["argv"],
                                "text": wording, "name": entry.get("name")}
        elif res.get("ok"):
            results["group"] = {"sent": True, "channel": "group", "brief": True,
                                "identity": res["identity"], "text": wording,
                                "chat_id": args.chat_id, "name": entry.get("name")}
        else:
            errors.append("no_new_group: %s" % (" | ".join(gerr)[:300]))
            results["group"] = {"sent": False, "channel": "none", "errors": gerr}
        results["owner"] = {"sent": False, "skipped": True, "channel": "none",
                            "reason": "本轮无新报销：按新规则只发群内一句提示，不发负责人私聊长文"}
        out = {"results": results, "mode": "no_new"}
        if errors:
            out["errors"] = errors
        _emit(out)
        return

    if args.skip_group:
        # ---- 本轮无新数据：按用户规则跳过群消息，只发负责人私聊（透明仍保留在私聊与清单） ----
        entry0 = (cfg.get("chat_whitelist", {}) or {}).get(args.chat_id) or {}
        results["group"] = {"sent": False, "skipped": True, "channel": "none",
                            "reason": "本轮无新数据：按配置跳过群消息，仅发送负责人私聊",
                            "chat_id": args.chat_id, "name": entry0.get("name")}
    else:
        whitelist = cfg.get("chat_whitelist", {})
        entry = whitelist.get(args.chat_id)
        if not args.chat_id:
            print(json.dumps({"results": {"group": {"sent": False, "channel": "none",
                              "error": "拒绝发送：未提供 --chat-id（且非 --owner-only / --skip-group 模式）"}}},
                             ensure_ascii=False, indent=2))
            sys.exit(2)
        if entry is None:
            print(json.dumps({"results": {"group": {"sent": False, "channel": "none",
                              "error": "拒绝发送：chat_id 不在白名单（防串群）", "chat_id": args.chat_id}}},
                             ensure_ascii=False, indent=2))
            sys.exit(2)
        if entry.get("notify") is not True:
            print(json.dumps({"results": {"group": {"sent": False, "channel": "none",
                              "error": "拒绝发送：该群 notify=false（新群需人工确认）",
                              "chat_id": args.chat_id, "name": entry.get("name")}}},
                             ensure_ascii=False, indent=2))
            sys.exit(2)

        # ---- 通道一：物流部群（极简总览·金额公开·只列本轮有凭证的司机） ----
        if group_text and group_text.strip():
            res, gerr = send_with_fallback(args.chat_id, group_text, args.run_id, args.dry_run)
            if res.get("dry_run"):
                results["group"] = {"sent": False, "dry_run": True, "would_run": res["argv"],
                                    "name": entry.get("name")}
            elif res.get("ok"):
                results["group"] = {"sent": True, "channel": "group", "identity": res["identity"],
                                    "chat_id": args.chat_id, "name": entry.get("name")}
            else:
                # 群内两种身份都失败 -> 降级私聊（总览照发到负责人，确保不漏）
                dm = cfg.get("fallback_dm_chat_id")
                fb = "⚠ 群内通报失败，降级私聊\n目标群：%s（%s）\n失败：%s\n\n%s" % (
                    entry.get("name"), args.chat_id, " | ".join(gerr)[:500], group_text)
                dres, derr = send_with_fallback(dm, fb, args.run_id, args.dry_run)
                if dres.get("ok"):
                    results["group"] = {"sent": True, "channel": "dm_fallback", "identity": dres["identity"],
                                        "chat_id": dm, "original_group": args.chat_id}
                else:
                    errors.append("group_dm_fallback: %s" % (" | ".join(derr)[:300]))
                    results["group"] = {"sent": False, "channel": "none", "errors": gerr}
        else:
            results["group"] = {"sent": False, "channel": "none", "error": "群内总览内容为空"}

    # ---- 已停用的司机私聊通道：配置禁用时硬拦截，兼容旧 prompt 也不发 ----
    pcfg = (cfg.get("reporting") or {}).get("per_driver_dm") or {}
    if args.driver_dm_manifest:
        results["driver_dm"] = send_driver_dms(cfg, args.driver_dm_manifest,
                                               args.run_id, args.dry_run)
        if pcfg.get("enabled") is not False:
            for x in results["driver_dm"].get("failed", []):
                errors.append("driver_dm[%s]: %s" % (x.get("driver"), x.get("reason")))
    else:
        results["driver_dm"] = {"enabled": bool(pcfg.get("enabled")), "sent": [],
                                "note": "本轮未提供 --driver-dm-manifest；司机私聊已取消，只发群内与负责人私聊"}

    # ---- 通道二：负责人私聊（专业完整版） ----
    if owner_text and owner_text.strip():
        owner_dm = cfg.get("owner_dm_chat_id") or cfg.get("fallback_dm_chat_id")
        if not owner_dm:
            results["owner"] = {"sent": False, "channel": "none",
                                "error": "config 缺少 owner_dm_chat_id / fallback_dm_chat_id"}
        else:
            res, oerr = send_with_fallback(owner_dm, owner_text, args.run_id, args.dry_run)
            if res.get("dry_run"):
                results["owner"] = {"sent": False, "dry_run": True, "would_run": res["argv"]}
            elif res.get("ok"):
                results["owner"] = {"sent": True, "channel": "owner_dm", "identity": res["identity"],
                                    "chat_id": owner_dm}
            else:
                errors.append("owner_dm: %s" % (" | ".join(oerr)[:300]))
                results["owner"] = {"sent": False, "channel": "none", "errors": oerr}
    else:
        results["owner"] = {"sent": False, "channel": "none", "note": "未提供负责人专业版内容"}

    # 司机私聊已取消：恒为空，不再产生「谁没到齐 / 谁缺 open_id」的待办名单。
    # 保留字段只为兼容旧版运行清单与月报解析。
    out = {"results": results, "missing_driver_dm": []}
    if errors:
        out["errors"] = errors
    _emit(out)


if __name__ == "__main__":
    main()
