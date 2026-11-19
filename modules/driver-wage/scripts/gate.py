#!/usr/bin/env python3
"""发起人确认硬闸门。

安全性质（对应"需要发起人确认后私发"）：
  * 计算/核对完成后只生成**待确认预览**，绝不自动发送任何司机消息。
  * 定时任务只能跑到 preview，确认必须由发起人本人显式给出 token。
  * 确认与结果指纹、输入指纹绑定：确认后任何数据或规则改动都会使确认失效。
  * REVIEW 结论需要发起人额外显式接受复核项；BLOCK 一律拒绝。
  * 确认有有效期，过期必须重新核算与确认。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta

import audit


class GateError(RuntimeError):
    pass


def _secret(cfg: dict) -> bytes:
    env = os.environ.get("PAYROLL_GATE_SECRET")
    if env:
        return env.encode("utf-8")
    path = os.path.join(cfg["paths"]["state_dir"], "gate_secret")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read().strip()
    secret = hashlib.sha256(os.urandom(32)).hexdigest().encode("ascii")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(secret)
    os.chmod(path, 0o600)
    return secret


def token_for(cfg: dict, month: str, result_hash: str, verdict: str) -> str:
    msg = f"{month}|{result_hash}|{verdict}".encode("utf-8")
    return hmac.new(_secret(cfg), msg, hashlib.sha256).hexdigest()[:20]


def _paths(cfg: dict, month: str):
    d = os.path.join(cfg["paths"]["state_dir"], month)
    os.makedirs(d, exist_ok=True)
    return d, os.path.join(d, "confirmation.json")


def load_result_and_verification(cfg: dict, month: str):
    from compute import load_result
    vpath = os.path.join(cfg["paths"]["out_dir"], month, "verification.json")
    if not os.path.isfile(vpath):
        raise GateError("尚未核对（缺 verification.json）：必须先 compute 再 verify")
    with open(vpath, "r", encoding="utf-8") as f:
        return load_result(cfg, month), json.load(f)


def make_preview(cfg: dict, month: str) -> dict:
    result, verification = load_result_and_verification(cfg, month)
    gate_cfg = cfg["payroll"].get("gate", {})
    verdict = verification["verdict"]
    token = token_for(cfg, month, result["result_hash"], verdict)
    ttl = int(gate_cfg.get("confirm_ttl_hours", 72))
    preview = {
        "month": month, "verdict": verdict, "token": token,
        "result_hash": result["result_hash"], "input_hash": result["input_hash"],
        "owner": gate_cfg.get("owner", "发起人"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "expires_at": (datetime.now().astimezone() + timedelta(hours=ttl))
        .isoformat(timespec="seconds"),
        "totals": result["totals"],
        "per_driver": [{"driver": s["display_name"], "trips": s["trip_count"],
                        "gross": s["gross"], "reimbursement": s["reimbursement"],
                        "net": s["net"]} for s in result["payslips"]],
        "blocking": verification["blocking"], "review": verification["review"],
        "files": {
            "payslips": os.path.join(cfg["paths"]["out_dir"], month, "payslips.json"),
            "verification_md": os.path.join(cfg["paths"]["out_dir"], month, "verification.md"),
            "workbook": os.path.join(cfg["paths"]["out_dir"], month, f"工资表_{month}.xlsx"),
        },
        "next_step": ("核对结论 BLOCK：先修正输入并重新 compute/verify，当前不可确认"
                      if verdict == "BLOCK" else
                      f"请发起人核对后执行 confirm --token {token}"
                      + (" --accept-review" if verdict == "REVIEW" else "")),
    }
    d, _ = _paths(cfg, month)
    with open(os.path.join(d, "preview.json"), "w", encoding="utf-8") as f:
        json.dump(preview, f, ensure_ascii=False, indent=2)
        f.write("\n")
    audit.log(cfg, month, "preview", verdict=verdict,
              result_hash=result["result_hash"], token=token[:8])
    return preview


def confirm(cfg: dict, month: str, token: str, owner: str,
            accept_review: bool = False) -> dict:
    result, verification = load_result_and_verification(cfg, month)
    gate_cfg = cfg["payroll"].get("gate", {})
    expected_owner = gate_cfg.get("owner", "发起人")
    if owner != expected_owner:
        raise GateError(f"确认人 {owner!r} 不是配置的发起人 {expected_owner!r}")
    if verification["verdict"] == "BLOCK":
        raise GateError("核对结论为 BLOCK，禁止确认；请先修正并重算")
    expected = token_for(cfg, month, result["result_hash"], verification["verdict"])
    if not hmac.compare_digest(token, expected):
        raise GateError("确认 token 无效（结果可能已变化），请重新生成预览")
    if verification["verdict"] == "REVIEW" and not accept_review:
        raise GateError("核对存在需复核项，须显式 --accept-review 才能确认")
    ttl = int(gate_cfg.get("confirm_ttl_hours", 72))
    rec = {
        "month": month, "owner": owner, "token": token,
        "result_hash": result["result_hash"], "input_hash": result["input_hash"],
        "verdict": verification["verdict"], "accept_review": accept_review,
        "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "expires_at": (datetime.now().astimezone() + timedelta(hours=ttl))
        .isoformat(timespec="seconds"),
    }
    _, path = _paths(cfg, month)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
        f.write("\n")
    audit.log(cfg, month, "confirm", owner=owner, verdict=verification["verdict"],
              result_hash=result["result_hash"], accept_review=accept_review)
    return rec


def assert_dispatch_allowed(cfg: dict, month: str) -> dict:
    """dispatch 前置硬校验。任何一条不满足都必须抛错，绝不降级、绝不代确认。"""
    result, verification = load_result_and_verification(cfg, month)
    # 内容级防篡改：不能只比对文件里写着的 result_hash 字段（字段本身也可能被改）
    from config import canonical_hash
    actual = canonical_hash({k: result[k] for k in
                             ("month", "payslips", "totals", "input_hash")})
    if actual != result.get("result_hash"):
        raise GateError("结果文件内容与指纹不符，可能已被改动，禁止私发")
    if verification["verdict"] == "BLOCK":
        raise GateError("核对结论 BLOCK，禁止私发")
    _, path = _paths(cfg, month)
    if not os.path.isfile(path):
        raise GateError("发起人尚未确认（无 confirmation.json），禁止私发")
    with open(path, "r", encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("owner") != cfg["payroll"].get("gate", {}).get("owner", "发起人"):
        raise GateError("确认人不是当前配置的发起人")
    if rec.get("result_hash") != result["result_hash"]:
        raise GateError("确认后结果已变化，确认失效，须重新核算与确认")
    if rec.get("input_hash") != result.get("input_hash"):
        raise GateError("确认后输入数据已变化，确认失效")
    if verification.get("result_hash") != result.get("result_hash"):
        raise GateError("核对报告与计算结果不匹配，须重新核对")
    if rec.get("verdict") != verification["verdict"]:
        raise GateError("核对结论已变化，须重新确认")
    try:
        if datetime.now().astimezone() > datetime.fromisoformat(rec["expires_at"]):
            raise GateError("确认已过期，须重新核算与确认")
    except (KeyError, ValueError):
        raise GateError("确认记录缺少有效时间")
    return rec
