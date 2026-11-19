#!/usr/bin/env python3
"""飞书司机名册同步（主数据源，可离线降级）。

用户定案（2026-11-14）：司机名册「以飞书司机数为主」，「有几个司机计算几个」，
不要求全员到齐、不凑人数、不把缺 open_id 当成异常或待办。

本模块只做一件事：把飞书侧**真实存在**的司机拉成一份缓存（姓名 + open_id）。
两种来源可同时启用，取并集：
  * 部门模式  contact +search-user --query <部门关键词> --as user
  * 群模式    im +chat-members-list --chat-id <群 id> --as user --page-all
群主、机器人、外部成员、未激活用户一律排除（全部由配置指定，代码不硬编码人名）。

安全与工程约定：
  * 只调用 read 级 lark-cli 命令，绝不发任何消息。
  * 绝不猜 open_id：拿不到有效 ou_ id 就不写进缓存。
  * 飞书不可用 / 未同步时不伪造名册，返回 ok=false 交调用方降级。
  * 缓存落在 state/（gitignore）；compute / verify 只读缓存，不联网、不依赖飞书在线。
  * 本模块不含任何真实姓名、群 id、open_id —— 全部来自 profile 配置，可安全开源。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

from config import ConfigError, expand


def _json_objects(raw: str) -> list[dict]:
    """lark-cli 输出可能夹带 `[page 1] fetching...` 等进度行，逐个取完整 JSON 对象。"""
    dec = json.JSONDecoder()
    objs, i = [], 0
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


def _users_from(raw: str) -> list[dict]:
    for obj in _json_objects(raw):
        data = obj.get("data")
        if isinstance(data, dict) and isinstance(data.get("users"), list):
            return [u for u in data["users"] if isinstance(u, dict)]
    return []


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return cp.returncode, cp.stdout, cp.stderr
    except FileNotFoundError:
        return 127, "", "lark-cli 未安装或不在 PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "调用飞书超时"


def _valid_open_id(oid: str) -> bool:
    return isinstance(oid, str) and oid.startswith("ou_") and len(oid) > 8


def _fetch(cfg: dict, rcfg: dict) -> tuple[list[dict], list[str]]:
    fcfg = rcfg.get("feishu", {})
    cli = fcfg.get("cli", "lark-cli")
    timeout = int(fcfg.get("timeout", 60))
    exclude_names = set(fcfg.get("exclude_names", []))
    exclude_ids = set(fcfg.get("exclude_open_ids", []))
    dept_contains = fcfg.get("department_contains", "")
    exclude_external = bool(fcfg.get("exclude_external_users", True))
    found: dict[str, dict] = {}
    notes: list[str] = []

    sources: list[tuple[str, list[str]]] = []
    query = fcfg.get("department_query", "")
    if query:
        cmd = [cli, "contact", "+search-user", "--query", query,
               "--as", "user", "--format", "json",
               "--page-size", str(int(fcfg.get("page_size", 30)))]
        if exclude_external:
            cmd.append("--exclude-external-users")
        sources.append(("department", cmd))
    chat_id = fcfg.get("chat_id", "")
    if chat_id:
        sources.append(("chat", [cli, "im", "+chat-members-list", "--as", "user",
                                 "--chat-id", chat_id, "--page-all",
                                 "--format", "json"]))

    if not sources:
        return [], ["未配置任何飞书名册来源（department_query / chat_id 均为空）"]

    for kind, cmd in sources:
        code, out, err = _run(cmd, timeout)
        if code != 0:
            notes.append(f"{kind} 来源读取失败（exit {code}）: {err[-200:] or out[-200:]}")
            continue
        users = _users_from(out)
        notes.append(f"{kind} 来源返回 {len(users)} 位成员")
        for u in users:
            name = str(u.get("name") or u.get("localized_name") or "").strip()
            oid = str(u.get("open_id") or u.get("member_id") or "").strip()
            if not name or not _valid_open_id(oid):
                continue
            if name in exclude_names or oid in exclude_ids:
                continue
            if u.get("is_activated") is False:
                continue
            if exclude_external and u.get("is_cross_tenant") is True:
                continue
            dept = str(u.get("department") or "")
            if kind == "department" and dept_contains and dept_contains not in dept:
                continue
            found[oid] = {"name": name, "open_id": oid,
                          "department": dept, "via": kind}
    return sorted(found.values(), key=lambda x: x["name"]), notes


def cache_path(cfg: dict) -> str:
    rcfg = cfg["payroll"].get("roster", {})
    raw = rcfg.get("feishu", {}).get(
        "cache_path", "{root}/state/feishu_roster.json")
    return expand(raw, cfg["payroll"], cfg["_meta"])


def sync(cfg: dict) -> dict:
    """从飞书拉取名册并写缓存。返回摘要；失败时不覆盖已有缓存，绝不伪造。"""
    rcfg = cfg["payroll"].get("roster", {})
    if rcfg.get("source") != "feishu":
        raise ConfigError("payroll.roster.source 不是 feishu，无需同步")
    drivers, notes = _fetch(cfg, rcfg)
    ok = bool(drivers)
    payload = {
        "schema": 1, "ok": ok,
        "synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": rcfg.get("feishu", {}).get("department_query", "")
                  or rcfg.get("feishu", {}).get("chat_id", ""),
        "drivers": drivers, "notes": notes,
    }
    result = {"ok": ok, "drivers": len(drivers), "notes": notes,
              "names": [d["name"] for d in drivers], "cache": ""}
    if not ok:
        result["reason"] = "; ".join(notes) or "飞书未返回任何有效司机"
        return result
    path = cache_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    result["cache"] = path
    result["synced_at"] = payload["synced_at"]
    return result


def load_cached(cfg: dict) -> dict | None:
    path = cache_path(cfg)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("drivers"), list):
            return doc
    except (OSError, ValueError):
        pass
    return None


def merge_roster(local_drivers: list[dict], cache: dict | None,
                 aliases: dict, rcfg: dict) -> tuple[list[dict], dict]:
    """飞书优先 + 本地补充（union）。

    飞书在册司机：覆盖 open_id、标记 feishu=true（唯一可私发对象）。
    本地名册里不在飞书的司机：保留，标记 feishu=false，只用于计算归属与报表定位，
    绝不因「不在飞书」被当成异常、待办或催办理由。
    飞书新增、本地没有的司机：直接纳入（弹性名册，有几个算几个）。
    """
    cache = cache or {}
    feishu: dict[str, dict] = {}
    for u in cache.get("drivers", []):
        name = str(u.get("name", "")).strip()
        oid = str(u.get("open_id", "")).strip()
        if not name or not _valid_open_id(oid):
            continue
        did = aliases.get(name, name)
        feishu[did] = {"id": did, "name": name, "open_id": oid,
                       "feishu": True, "source": "feishu", "notify": True}

    out: list[dict] = []
    seen: set[str] = set()
    for d in local_drivers:
        if not isinstance(d, dict) or "id" not in d:
            continue
        did = d["id"]
        x = dict(d)
        hit = feishu.get(did)
        if hit:
            x["open_id"] = hit["open_id"]
            x["feishu"] = True
            x["source"] = "feishu"
            # notify=false 是发起人显式关闭，飞书名册不能覆盖这个决定
            if not d.get("notify", True):
                x["notify"] = False
        else:
            x["feishu"] = False
            x["source"] = "local"
        out.append(x)
        seen.add(did)
    for did, x in feishu.items():
        if did not in seen:
            out.append(x)

    out.sort(key=lambda d: (not d.get("feishu", False), str(d["id"])))
    max_age = int(rcfg.get("feishu", {}).get("max_age_hours", 24))
    stale = False
    synced_at = cache.get("synced_at", "")
    if synced_at:
        try:
            age = (datetime.now().astimezone()
                   - datetime.fromisoformat(synced_at)).total_seconds() / 3600
            stale = age > max_age
        except ValueError:
            stale = True
    meta = {
        "feishu_enabled": True,
        "cache_valid": bool(feishu),
        "synced_at": synced_at,
        "stale": stale or not feishu,
        "feishu_drivers": sorted(d for d, x in feishu.items()),
        "local_only_drivers": sorted(x["id"] for x in out
                                     if not x.get("feishu")),
        "total_drivers": len(out),
    }
    return out, meta
