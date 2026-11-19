#!/usr/bin/env python3
"""审计日志与投递幂等状态。所有外部动作留痕，重复运行不会重复私发。"""
from __future__ import annotations

import json
import os
from datetime import datetime


def _state(cfg: dict, month: str) -> str:
    path = os.path.join(cfg["paths"]["state_dir"], month)
    os.makedirs(path, exist_ok=True)
    return path


def log(cfg: dict, month: str, event: str, **fields) -> None:
    rec = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "event": event, **fields}
    with open(os.path.join(_state(cfg, month), "audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _sent_path(cfg: dict, month: str) -> str:
    return os.path.join(_state(cfg, month), "sent.json")


def load_sent(cfg: dict, month: str) -> dict:
    p = _sent_path(cfg, month)
    if not os.path.isfile(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def was_sent(cfg: dict, month: str, driver: str) -> bool:
    rec = load_sent(cfg, month).get(driver)
    return bool(rec and rec.get("status") == "sent")


def mark_sent(cfg: dict, month: str, driver: str, status: str,
              detail: str = "", channel: str = "", message_id: str = "") -> None:
    data = load_sent(cfg, month)
    data[driver] = {"status": status, "detail": detail, "channel": channel,
                    "message_id": message_id,
                    "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(_sent_path(cfg, month)), exist_ok=True)
    with open(_sent_path(cfg, month), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
