#!/usr/bin/env python3
"""司机名册与归属解析（混合群模式）。

2026-11-14 起所有司机共用一个「物流部」群，「群 = 某司机」的旧假设作废。
每张凭证的司机由**消息发送者**决定：群成员姓名 / open_id ↔ 司机名册精确匹配。
识别不出的绝不猜归属，一律进「待人工确认·未识别司机」。

名册来源（三者合并，精确匹配，不做模糊匹配）：
  1. 报表目录下 {司机}{年份}.xlsx 的文件名前缀；
  2. config.driver_roster.extra_drivers（无报表文件但历史上报销过的司机，按 record_without_write）；
  3. config.driver_roster.members 已登记的姓名→open_id 映射。
群主本人（config.owner_open_id）不算司机。

**弹性司机（2026-11-14 用户定案）**：司机人员会增减，名册不再是前置门槛，
也不要求全员进群或补齐 open_id。名册外的群成员只要发来付款凭证截图，
飞书返回的真实成员姓名即可直接作为 driver（客观发送者身份，不是猜），
标记 is_new_driver=true；因无当年报表一律 record_without_write，绝不新建报表。
未匹配成员、缺 open_id 都只作信息记录，绝不当异常、待办或催办理由。

用法：
  driver_roster.py list [--year 2026]
      列出名册、open_id 映射、是否有报表文件。
  driver_roster.py sync [--chat-id oc_xxx] [--year 2026] [--dry-run]
      从物流部群成员同步 open_id→司机，写回 config.driver_open_ids 与 driver_roster.members。
      仅用于提升归属精度；未匹配名册的成员只作信息记录，不是异常或待办。
  driver_roster.py resolve --sender-id ou_xxx [--sender-name 姓名] [--text "消息文字"]
                           [--receipt-name 票面姓名] [--year 2026]
      解析一条消息的司机归属，输出 JSON。

resolve 输出字段：
  driver          解析出的司机，未识别为 null
  status          ok | unknown | conflict
  action          proceed | manual_review
  matched_by      sender_open_id | sender_name | sender_name_new_driver | receipt_name | null
  is_new_driver   名册外按弹性口径新建的司机（仅发图时产生）
  has_report      是否存在该司机当年报表文件
  report_action   write（存在报表）/ record_without_write（无报表，绝不新建）/ manual_review
  reason          人能看懂的解释，可直接写进清单与通报
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.abspath(os.path.join(HERE, "..", "config.json"))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get, current_year as rc_year  # 配置优先、原值兜底

# 同进程内 report_drivers 结果缓存（key=目录），避免 resolve 里重复 glob 磁盘
_REPORT_DRIVERS_CACHE = {}


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _REPORT_DRIVERS_CACHE.clear()


def report_dir(cfg, year):
    pattern = cfg.get("driver_roster", {}).get(
        "report_dir_pattern", "{report_root}/{year}年/司机工资")
    return pattern.format(report_root=cfg.get("report_root", ""), year=year)


def report_drivers(cfg, year):
    """从报表文件名提取司机名；{司机}{年份}.xlsx。同目录同一进程内缓存结果。"""
    d = report_dir(cfg, year)
    if d in _REPORT_DRIVERS_CACHE:
        return _REPORT_DRIVERS_CACHE[d]
    names = []
    for path in glob.glob(os.path.join(d, "*.xlsx")):
        base = os.path.basename(path)
        if base.startswith("~$"):
            continue
        m = re.match(r"^(.+?)(\d{4})\.xlsx$", base)
        if m:
            names.append(m.group(1))
    result = sorted(set(names))
    _REPORT_DRIVERS_CACHE[d] = result
    return result


def all_drivers(cfg, year):
    roster = cfg.get("driver_roster", {})
    names = set(report_drivers(cfg, year))
    names.update(roster.get("extra_drivers") or [])
    names.update((roster.get("members") or {}).keys())
    names.update((cfg.get("driver_open_ids") or {}).keys())
    # 台账里出现过的司机也算有效（历史数据不能因为没报表就消失）
    for d in ledger_drivers(cfg):
        names.add(d)
    names.discard("")
    return sorted(names)


def ledger_drivers(cfg):
    """台账中出现过的司机名（只读，不修改数据库）。"""
    import sqlite3
    db = os.path.join(cfg.get("state_dir", ""), "ledger.sqlite")
    if not os.path.isfile(db):
        return []
    try:
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT DISTINCT driver FROM entries WHERE driver IS NOT NULL AND driver<>''"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def open_id_map(cfg):
    """姓名 -> open_id，合并 driver_roster.members 与 driver_open_ids。"""
    m = {}
    for name, oid in (cfg.get("driver_open_ids") or {}).items():
        if name and oid:
            m[name] = oid
    for name, oid in (cfg.get("driver_roster", {}).get("members") or {}).items():
        if name and oid:
            m[name] = oid
    return m


def parse_cli_json(raw):
    """lark-cli 输出可能带 [page N] 进度行 / 多个 JSON 块，逐个取完整对象。"""
    dec = json.JSONDecoder()
    objs = []
    i = 0
    while True:
        j = raw.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(raw[j:])
            if isinstance(obj, dict):
                objs.append(obj)
            i = j + end
        except ValueError:
            i = j + 1
    return objs


def primary_chat_id(cfg):
    disc = cfg.get("chat_discovery", {})
    return disc.get("primary_chat_id") or next(
        iter(cfg.get("chat_whitelist", {})), "")


def sync(cfg, chat_id, year, dry_run):
    chat_id = chat_id or primary_chat_id(cfg)
    if not chat_id:
        return {"ok": False, "reason": "找不到主群 chat_id"}
    ident = ((cfg.get("adapters", {}).get("input", {}).get("client", {}) or {}).get("identity")
             or "user")
    cmd = ["lark-cli", "im", "+chat-members-list", "--as", ident,
           "--chat-id", chat_id, "--page-all"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as ex:
        return {"ok": False, "reason": "调用群成员接口失败：%s" % ex}
    if r.returncode != 0:
        return {"ok": False, "reason": "群成员接口返回非 0：%s" % (r.stderr or "")[-400:]}

    users, bots, has_more = [], [], False
    for obj in parse_cli_json(r.stdout or ""):
        data = obj.get("data") or {}
        users.extend(data.get("users") or [])
        bots.extend(data.get("bots") or [])
        has_more = has_more or bool(data.get("has_more"))
    if not users and not bots:
        return {"ok": False, "reason": "未解析到任何群成员",
                "raw_tail": (r.stdout or "")[-300:]}

    drivers = all_drivers(cfg, year)
    owner_oid = cfg.get("owner_open_id", "")
    roster = cfg.setdefault("driver_roster", {})
    members = roster.setdefault("members", {})
    open_ids = cfg.setdefault("driver_open_ids", {})

    matched, unmatched, owner_skipped, added = [], [], [], []
    for u in users:
        name = (u.get("name") or "").strip()
        oid = (u.get("member_id") or u.get("id") or "").strip()
        if oid and oid == owner_oid and roster.get("owner_open_id_is_not_driver", True):
            owner_skipped.append({"name": name, "open_id": oid})
            continue
        if name in drivers:
            rec = {"name": name, "open_id": oid}
            matched.append(rec)
            if oid and (members.get(name) != oid or open_ids.get(name) != oid):
                added.append(rec)
                if not dry_run:
                    members[name] = oid
                    open_ids[name] = oid
        else:
            unmatched.append({"name": name, "open_id": oid,
                              "note": "未匹配既有名册：按弹性口径，该成员若发付款凭证截图会直接按其姓名建立司机；"
                                      "此处只作信息记录，不是异常、不催办、不要求补 open_id"})

    if not dry_run and added:
        save_config(cfg)

    return {
        "ok": True, "dry_run": dry_run, "chat_id": chat_id, "year": year,
        "members_in_chat": len(users), "bots_in_chat": len(bots),
        "bots": [{"name": b.get("name"), "member_id": b.get("member_id"),
                  "app_id": b.get("app_id")} for b in bots],
        "bot_in_chat": bool(bots),
        "has_more": has_more,
        "roster_drivers": drivers,
        "matched": matched, "owner_skipped": owner_skipped,
        "unmatched": unmatched, "added_to_config": added,
        "note": "名册不是前置条件：未匹配成员或缺 open_id 只作信息记录，不构成异常或待办；"
                "司机弹性，只统计发了付款凭证截图的人。"
                + ("群内已有机器人，通报可直发群内。" if bots else
                   "群内暂无机器人，群内通报会降级到负责人私聊，需人工把任务助手机器人拉进群。"),
    }


def names_in_text(text, drivers):
    """返回文字里明确出现的司机姓名（精确子串匹配）。"""
    if not text:
        return []
    return [d for d in drivers if d in text]


def plausible_person_name(name, cfg_override=None):
    """判断飞书返回的 sender_name 是否像一个真实人名（排除机器人/账号/群名）。

    弹性司机口径下，名册外姓名可以直接成为 driver；这里只做最低限度的形态校验，
    避免把「XX's Feishu CLI」「XX门店群」这类字符串误当成司机。
    """
    if not name:
        return False
    s = str(name).strip()
    cfg = cfg_override or {}
    lo = int(rc_get(cfg, "name_len_min", 2))
    hi = int(rc_get(cfg, "name_len_max", 12))
    if not (lo <= len(s) <= hi):
        return False
    forbidden = rc_get(cfg, "name_forbidden_tokens",
                       ["@", "/", "\\", ":", " ", "群", "机器人", "CLI", "Bot", "bot"])
    if any(x in s for x in forbidden):
        return False
    if rc_get(cfg, "name_require_cjk", True):
        return bool(re.search(r"[\u4e00-\u9fa5]", s))
    return True


def resolve(cfg, sender_id, sender_name, text, receipt_name, year):
    drivers = all_drivers(cfg, year)
    oid_map = open_id_map(cfg)
    by_oid = {v: k for k, v in oid_map.items()}

    roster_cfg = cfg.get("driver_roster") or {}
    accept_new = bool(roster_cfg.get("accept_new_driver_by_sender_name"))
    is_owner = bool(sender_id and sender_id == (cfg.get("owner_open_id") or ""))

    driver_by_oid = by_oid.get(sender_id) if sender_id else None
    known_driver_name = sender_name if sender_name in drivers else None
    # 弹性司机：名册外的真实群成员，发图时按发送者姓名直接建立 driver（不是猜）
    new_driver_name = None
    if accept_new and not is_owner and not known_driver_name and plausible_person_name(sender_name, cfg):
        new_driver_name = str(sender_name).strip()
    driver_by_sender_name = known_driver_name or new_driver_name
    text_names = names_in_text(text, drivers)
    receipt_name_hit = receipt_name if receipt_name in drivers else None

    # 群主本人发的凭证不能自动归到某个司机（优先级高于弹性名册，避免负责人被当成新司机）
    if is_owner and not known_driver_name:
        return {
            "ok": True, "driver": None, "status": "unknown", "action": "manual_review",
            "matched_by": None, "reason": "发送者是群主/负责人本人，不是司机；需注明这笔替哪位司机报销",
        }

    candidates = {}
    if driver_by_oid:
        candidates["sender_open_id"] = driver_by_oid
    if driver_by_sender_name:
        candidates["sender_name"] = driver_by_sender_name
    distinct = set(candidates.values())
    if len(distinct) > 1:
        return {
            "ok": True, "driver": None, "status": "conflict", "action": "manual_review",
            "matched_by": None, "candidates": candidates,
            "reason": "发送者 open_id 与姓名解析出不同司机（%s），不猜归属" % " / ".join(sorted(distinct)),
        }

    driver = next(iter(distinct)) if distinct else (
        receipt_name_hit if receipt_name_hit else None)
    matched_by = next((k for k, v in candidates.items() if v == driver), None)
    if driver and matched_by is None and receipt_name_hit:
        matched_by = "receipt_name"
        driver = receipt_name_hit

    # 文字 / 票面姓名交叉校验：出现别的司机姓名即冲突
    cross = set(text_names)
    if receipt_name_hit:
        cross.add(receipt_name_hit)
    cross.discard(driver)
    if driver and cross:
        return {
            "ok": True, "driver": None, "status": "conflict", "action": "manual_review",
            "matched_by": ("sender_name_new_driver"
                           if (new_driver_name and driver == new_driver_name) else matched_by),
            "sender_candidate": driver,
            "conflicting_names": sorted(cross),
            "reason": "发送者解析为 %s，但消息文字/票面出现其他司机姓名（%s），可能是替他人代发；不自动归属"
                      % (driver, "、".join(sorted(cross))),
        }

    if not driver:
        fallback = text_names[0] if len(text_names) == 1 else None
        if fallback:
            return {
                "ok": True, "driver": None, "status": "unknown", "action": "manual_review",
                "matched_by": None, "text_name_candidate": fallback,
                "reason": "发送者未在司机名册中（需先由 driver_roster.py sync 同步群成员）；"
                          "文字提到 %s，仅作线索，不自动归属" % fallback,
            }
        return {
            "ok": True, "driver": None, "status": "unknown", "action": "manual_review",
            "matched_by": None,
            "reason": "发送者 open_id=%s / 姓名=%s 未匹配司机名册，不猜归属、不默认算给某个人"
                      % (sender_id or "空", sender_name or "空"),
        }

    has_report = driver in report_drivers(cfg, year)
    is_new = bool(new_driver_name and driver == new_driver_name)
    write_note = ("报表文件存在，按月份定位写入" if has_report
                  else "无当年报表文件，按 record_without_write 只登记台账、绝不新建报表")
    if is_new:
        return {
            "ok": True, "driver": driver, "status": "ok", "action": "proceed",
            "matched_by": "sender_name_new_driver", "is_new_driver": True,
            "cross_check_text_name": bool(text_names or receipt_name_hit),
            "has_report": has_report,
            "report_action": "write" if has_report else "record_without_write",
            "reason": "发送者 %s 不在既有名册，按弹性司机口径直接以其真实群成员姓名建立司机"
                      "（客观发送者身份，不是猜）；%s。如需写入报表请人工指定目标文件，系统绝不新建。"
                      % (driver, write_note),
        }
    return {
        "ok": True, "driver": driver, "status": "ok", "action": "proceed",
        "matched_by": matched_by, "is_new_driver": False,
        "cross_check_text_name": bool(text_names or receipt_name_hit),
        "has_report": has_report,
        "report_action": "write" if has_report else "record_without_write",
        "reason": "按发送者精确匹配司机名册为 %s；%s" % (driver, write_note),
    }


def listing(cfg, year):
    drivers = all_drivers(cfg, year)
    rep = report_drivers(cfg, year)
    oid_map = open_id_map(cfg)
    return {
        "ok": True, "year": year,
        "report_dir": report_dir(cfg, year),
        "primary_chat_id": primary_chat_id(cfg),
        "primary_chat_name": (cfg.get("chat_whitelist", {})
                              .get(primary_chat_id(cfg), {}).get("name", "")),
        "retired_chats": cfg.get("retired_chats", {}),
        "drivers": [
            {"driver": d, "has_report": d in rep, "open_id": oid_map.get(d, ""),
             "report_action": "write" if d in rep else "record_without_write"}
            for d in drivers
        ],
        "ledger_drivers": ledger_drivers(cfg),
        "note": "月报与统计一律按台账 driver 分组，与群白名单完全解耦；只列实际有凭证的司机，"
                "不铺全员名册、不要求到齐或补齐 open_id；旧群停用不影响历史数据。",
    }


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.add_argument("--year", type=int)

    p = sub.add_parser("sync")
    p.add_argument("--chat-id")
    p.add_argument("--year", type=int)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("resolve")
    p.add_argument("--sender-id", required=True)
    p.add_argument("--sender-name")
    p.add_argument("--text")
    p.add_argument("--receipt-name")
    p.add_argument("--year", type=int)

    args = ap.parse_args()
    cfg = load_config()
    year = args.year or rc_year(cfg)

    if args.cmd == "list":
        out = listing(cfg, year)
    elif args.cmd == "sync":
        out = sync(cfg, args.chat_id, year, args.dry_run)
    else:
        out = resolve(cfg, args.sender_id, args.sender_name,
                      args.text, args.receipt_name, year)

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    if not out.get("ok", False):
        sys.exit(1)


if __name__ == "__main__":
    main()
