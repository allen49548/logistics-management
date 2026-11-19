#!/usr/bin/env python3
"""可插拔适配器层：让技能不绑定任何一家 IM / 任何一种名册来源。

设计目标（回答「如何适配所有人」）：
  * 司机名册来源（payroll.roster.source）可插拔：manual / csv / excel / feishu。
    - manual ：只读 roster.json（本地名册，默认；脱敏样例用这个，零外部依赖）。
    - csv    ：从一份 CSV 名册文件读取（id,name,open_id,notify...）。
    - excel  ：从一份 Excel 名册文件读取（同上列）。
    - feishu ：从飞书通讯录/群同步（复用 feishu_roster，只读、可离线降级）。
  * 通知通道（payroll.notify.channel）可插拔：dry-run / stdout / csv-export / feishu / email(占位)。
    - csv-export 让「没有对接 IM 工具」的用户也能拿到可投递产物（每条一个文件）。

每个适配器实现统一接口；config / notify 通过名字从注册表取用，不硬编码任何厂商。
本文件不含任何真实姓名、open_id、群 id、私有 SDK —— 全部来自 profile 配置，可安全开源。
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import datetime

from config import ConfigError, expand


# ============================================================================
# 一、司机名册来源（RosterSource）
# ============================================================================

def _read_table(path: str) -> list[dict]:
    """读 CSV / Excel 名册文件，首行作表头，返回行 dict 列表。"""
    if not os.path.isfile(path):
        raise ConfigError(f"名册文件不存在: {path}")
    if path.lower().endswith((".xlsx", ".xlsm", ".xls")):
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return []
        headers = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
        out = []
        for r in rows[1:]:
            if all(c is None for c in r):
                continue
            out.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _generic_merge(local: list[dict], cache_drivers: list[dict],
                   aliases: dict, rcfg: dict, feishu_flag: bool) -> tuple[list[dict], dict]:
    """来源无关的并集合并：来源提供「权威名单 + open_id」，本地名册提供补充（别名、notify 覆盖）。

    - 来源里的司机：用来源的 open_id，标记 feishu=feishu_flag。
    - 本地名册里来源没有的司机：保留，feishu=feishu_flag，仅用于计算归属与报表定位。
    - 绝不因「不在来源里」被当异常、待办或催办理由（弹性名册）。
    """
    cache = {d.get("id"): d for d in cache_drivers if isinstance(d, dict) and d.get("id")}
    out: list[dict] = []
    seen: set[str] = set()
    for d in local:
        if not isinstance(d, dict) or "id" not in d:
            continue
        did = d["id"]
        x = dict(d)
        hit = cache.get(did)
        if hit:
            x["open_id"] = hit.get("open_id", x.get("open_id"))
            x["source"] = hit.get("source", x.get("source", "source"))
            if feishu_flag:
                x["feishu"] = bool(hit.get("feishu", feishu_flag))
            # 发起人显式 notify=false 不可被来源覆盖
            if not d.get("notify", True):
                x["notify"] = False
        else:
            # 非飞书来源且不命中缓存时，不注入 feishu 键（保持 legacy 行为）
            if feishu_flag:
                x.setdefault("feishu", False)
            x.setdefault("source", "local")
        out.append(x)
        seen.add(did)
    for did, x in cache.items():
        if did not in seen:
            y = dict(x)
            if feishu_flag:
                y["feishu"] = feishu_flag
            y.setdefault("notify", True)
            out.append(y)

    out.sort(key=lambda d: (not d.get("feishu", False), str(d["id"])))
    meta = {
        "feishu_enabled": feishu_flag,
        "cache_valid": bool(cache_drivers),
        "synced_at": "", "stale": False,
        "feishu_drivers": [d["id"] for d in out if d.get("feishu")],
        "local_only_drivers": [d["id"] for d in out if not d.get("feishu")],
        "total_drivers": len(out),
    }
    return out, meta


class RosterSource:
    """名册来源基类。子类实现 sync() 返回 {ok, drivers, notes}。"""

    name = "base"

    def sync(self, cfg: dict, rcfg: dict) -> dict:
        raise NotImplementedError

    def load_cached(self, cfg: dict) -> dict | None:
        path = self._cache_path(cfg)
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

    def _cache_path(self, cfg: dict) -> str:
        raw = cfg["payroll"].get("roster", {}).get("cache_path",
                                                    "{state_dir}/roster_cache.json")
        return expand(raw, cfg["payroll"], cfg["_meta"])

    def merge(self, local: list[dict], cache: dict | None,
              aliases: dict, rcfg: dict) -> tuple[list[dict], dict]:
        drivers = (cache or {}).get("drivers", [])
        return _generic_merge(local, drivers, aliases, rcfg, feishu_flag=False)


class ManualRosterSource(RosterSource):
    """默认来源：名册即 roster.json 本地名册，无需任何外部系统。"""

    name = "manual"

    def sync(self, cfg: dict, rcfg: dict) -> dict:
        local = cfg["roster"].get("drivers", [])
        drivers = [{"id": d["id"], "name": d.get("name", d["id"]),
                    "open_id": d.get("open_id", ""), "feishu": False,
                    "source": "manual", "notify": d.get("notify", True)}
                   for d in local if isinstance(d, dict) and "id" in d]
        return {"ok": True, "drivers": drivers,
                "notes": ["manual 来源：名册来自 roster.json，未连接任何外部系统"]}

    def load_cached(self, cfg: dict) -> dict | None:
        # manual 来源无网络缓存，直接由 sync 产出
        return None


class CsvRosterSource(RosterSource):
    """从一份 CSV/Excel 名册文件读取权威名单（id,name,open_id,notify）。"""

    name = "csv"

    def sync(self, cfg: dict, rcfg: dict) -> dict:
        path = rcfg.get("csv", {}).get("path") or rcfg.get("path")
        if not path:
            return {"ok": False, "drivers": [],
                    "notes": ["csv 来源未配置 roster.csv.path"]}
        try:
            rows = _read_table(expand(path, cfg["payroll"], cfg["_meta"]))
        except ConfigError as exc:
            return {"ok": False, "drivers": [], "notes": [str(exc)]}
        drivers = []
        for r in rows:
            did = str(r.get("id") or r.get("司机id") or r.get("工号") or "").strip()
            if not did:
                continue
            drivers.append({
                "id": did,
                "name": str(r.get("name") or r.get("姓名") or did).strip(),
                "open_id": str(r.get("open_id") or r.get("openid") or "").strip(),
                "feishu": False, "source": "csv",
                "notify": str(r.get("notify", "true") or "true").lower() != "false",
            })
        return {"ok": bool(drivers), "drivers": drivers,
                "notes": [f"csv 来源读取 {len(drivers)} 位司机"]}

    def merge(self, local, cache, aliases, rcfg):
        # csv 来源的 open_id 即权威，feishu 标记为 False
        return _generic_merge(local, (cache or {}).get("drivers", []),
                              aliases, rcfg, feishu_flag=False)


class FeishuRosterSource(RosterSource):
    """飞书来源：复用既有 feishu_roster（只读、可离线降级）。"""

    name = "feishu"

    def sync(self, cfg: dict, rcfg: dict) -> dict:
        import feishu_roster
        return feishu_roster.sync(cfg)

    def load_cached(self, cfg: dict) -> dict | None:
        import feishu_roster
        return feishu_roster.load_cached(cfg)

    def merge(self, local, cache, aliases, rcfg):
        import feishu_roster
        return feishu_roster.merge_roster(local, cache, aliases, rcfg)


ROSTER_SOURCES = {
    "manual": ManualRosterSource,
    "csv": CsvRosterSource,
    "excel": CsvRosterSource,  # Excel 名册走同一读取器
    "feishu": FeishuRosterSource,
}


def get_roster_source(cfg: dict) -> RosterSource:
    src = cfg["payroll"].get("roster", {}).get("source", "manual")
    src = "manual" if src in ("local", "manual") else src  # local 为旧别名
    cls = ROSTER_SOURCES.get(src)
    if cls is None:
        raise ConfigError(f"未知名册来源 payroll.roster.source={src!r}；"
                          f"可选: {', '.join(ROSTER_SOURCES)}")
    return cls()


def apply_roster_source(cfg: dict) -> None:
    """config.load_all 调用：按 source 选适配器并合并名册。

    未启用飞书源时完全保持旧行为（不标记 feishu），保证脱敏样例与离线环境可跑。
    """
    rcfg = cfg["payroll"].get("roster", {})
    local = cfg["roster"].get("drivers", [])
    source = get_roster_source(cfg)
    if rcfg.get("source") != "feishu":
        # 非飞书来源：无缓存，直接合并
        drivers, meta = source.merge(local, source.sync(cfg, rcfg) or {"drivers": []},
                                     dict(cfg["field_map"].get("driver_aliases", {})),
                                     rcfg)
        cfg["roster"]["drivers"] = drivers
        cfg["_roster_meta"] = meta
        return
    # 飞书来源：复用其缓存与降级逻辑
    cache = source.load_cached(cfg)
    aliases = dict(cfg["field_map"].get("driver_aliases", {}))
    drivers, meta = source.merge(local, cache, aliases, rcfg)
    cfg["roster"]["drivers"] = drivers
    if not meta["cache_valid"]:
        meta["warning"] = ("飞书名册缓存不存在或为空；计算照常进行，"
                           "但按飞书口径当前没有可私发对象。请先执行 sync-roster。")
    elif meta["stale"]:
        meta["warning"] = "飞书名册缓存已过期，建议执行 sync-roster 重新同步。"
    cfg["_roster_meta"] = meta


# ============================================================================
# 二、通知通道（Channel）
# ============================================================================

class Channel:
    """通道基类。send() 负责把一条工资条文本投递给一位司机。"""

    name = "base"

    def send(self, open_id: str, text: str, key: str, cfg: dict) -> dict:
        raise NotImplementedError

    def owner_send(self, target: str, text: str, key: str, cfg: dict) -> dict:
        # 默认 owner 通知沿用 send；飞书单独实现 chat/私聊分支
        return self.send(target, text, key, cfg)


class DryRunChannel(Channel):
    """默认：不投递，只回显，绝不外发。"""

    name = "dry-run"

    def send(self, open_id, text, key, cfg):
        return {"ok": True, "status": "dry-run",
                "detail": f"[dry-run] 将发给 {open_id[:8]}… 共 {len(text)} 字（未真正发送）"}


class StdoutChannel(Channel):
    """把每条工资条打印到 stdout（便于本地验证）。"""

    name = "stdout"

    def send(self, open_id, text, key, cfg):
        print(f"\n----- 工资条 -> {open_id[:8]}… (key={key[:24]}) -----")
        print(text)
        return {"ok": True, "status": "sent", "channel": "stdout"}


class CsvExportChannel(Channel):
    """无 IM 工具的用户：把每条工资条导出成一个 .txt 文件，可手动发邮件/微信。

    这把「通讯」从「必须对接某家 IM」解耦成「产出可投递文件」，适配任何组织。
    """

    name = "csv-export"

    def send(self, open_id, text, key, cfg):
        out_dir = os.path.join(cfg["paths"]["out_dir"], "_export")
        os.makedirs(out_dir, exist_ok=True)
        safe = (open_id[:8] or "unknown").replace("/", "_")
        path = os.path.join(out_dir, f"{safe}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return {"ok": True, "status": "exported", "channel": "csv-export",
                "file": path, "note": "已导出为文件，可手动发送"}


class FeishuChannel(Channel):
    """飞书逐人私聊（复用既有 lark-cli 约定，含 bot->user 身份降级）。"""

    name = "feishu"

    def send(self, open_id, text, key, cfg):
        return _send_feishu_dm(open_id, text, key, cfg)

    def owner_send(self, target, text, key, cfg):
        ncfg = cfg["payroll"].get("notify", {})
        if target.startswith("ou_"):
            return _send_feishu_dm(target, text, key, cfg)
        cp = subprocess.run(
            ["lark-cli", "im", "+messages-send", "--as", "bot",
             "--chat-id", target, "--text", "-",
             "--idempotency-key", key, "--format", "json"],
            input=text, text=True, capture_output=True, timeout=30)
        return {"ok": cp.returncode == 0, "stderr": cp.stderr[-500:]}


def _send_feishu_dm(open_id: str, text: str, key: str, cfg: dict) -> dict:
    timeout = int(cfg["payroll"].get("notify", {}).get("timeout", 30))
    attempts = []
    for identity in ("bot", "user"):
        cmd = ["lark-cli", "im", "+messages-send", "--as", identity,
               "--user-id", open_id, "--text", "-",
               "--idempotency-key", key[:50], "--format", "json"]
        try:
            cp = subprocess.run(cmd, input=text, text=True, capture_output=True,
                                timeout=timeout)
            ok = cp.returncode == 0
            attempt = {"as": identity, "ok": ok, "stdout": cp.stdout[-500:],
                       "stderr": cp.stderr[-500:]}
        except FileNotFoundError:
            attempt = {"as": identity, "ok": False, "stderr": "lark-cli 未安装或不在 PATH"}
        except subprocess.TimeoutExpired:
            attempt = {"as": identity, "ok": False, "stderr": "发送超时"}
        attempts.append(attempt)
        if attempt["ok"]:
            return {"ok": True, "identity": identity, "attempts": attempts,
                    "stdout": attempt["stdout"], "stderr": attempt["stderr"]}
    return {"ok": False, "identity": None, "attempts": attempts,
            "stdout": "", "stderr": attempts[-1].get("stderr", "")}


class EmailChannel(Channel):
    """占位通道：演示通道可插拔，未实现真实 SMTP（避免在仓库里放凭据）。"""

    name = "email"

    def send(self, open_id, text, key, cfg):
        return {"ok": False, "status": "not-implemented",
                "detail": "email 通道为占位；请改用 csv-export 导出后手动发送，"
                          "或实现 scripts/adapters.py 的 EmailChannel.send()"}


CHANNELS = {
    "dry-run": DryRunChannel,
    "stdout": StdoutChannel,
    "csv-export": CsvExportChannel,
    "feishu": FeishuChannel,
    "email": EmailChannel,
}


def get_channel(name: str) -> Channel:
    cls = CHANNELS.get(name)
    if cls is None:
        raise ConfigError(f"未知通知通道 {name!r}；可选: {', '.join(CHANNELS)}")
    return cls()
