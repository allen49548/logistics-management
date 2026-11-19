#!/usr/bin/env python3
"""对外通讯适配器：发起人待确认通知 / 逐司机工资条私发。

安全默认值：
  * dry_run 默认 True。只有显式 --no-dry-run 才会真的外发。
  * 私发前必须通过 gate.assert_dispatch_allowed（已核对 + 发起人已确认 + 指纹未变）。
  * 一人一条、只含本人数据；绝不群发，绝不把个人工资条降级发到群里。
  * 「以飞书司机数为主」：启用飞书名册后，只向飞书在册且有 open_id 的司机私发；
    仅在本地名册里的司机只作信息记录，绝不当异常、待办或催办理由。
  * open_id 必须在名册内、notify=true、且以 ou_ 开头；缺失就跳过并列出名单，绝不猜。
  * 幂等：同一月同一司机已成功发送过就不再发，避免重复打扰或重复打款暗示。
  * 适配器可替换：feishu / stdout / email(占位) / dry-run，不含任何私有 SDK。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import adapters
import audit
import gate
import render


def _roster(cfg: dict) -> dict:
    return {d["id"]: d for d in cfg["roster"].get("drivers", [])}


def notify_owner(cfg: dict, month: str, preview: dict, dry_run: bool = True) -> dict:
    """把待确认摘要 + token 发给发起人。这不是给司机发工资，属于确认流程。"""
    ncfg = cfg["payroll"].get("notify", {})
    owner_target = ncfg.get("owner_user_id") or ncfg.get("owner_chat_id")
    lines = [
        f"【司机工资·待确认】{month}",
        f"核对结论：{preview['verdict']}　人数 {preview['totals']['drivers']}　"
        f"实发合计 ¥{preview['totals']['net']}",
        f"应发 ¥{preview['totals']['gross']}　报销 ¥{preview['totals']['reimbursement']}",
        "",
        "核对报告：" + preview["files"]["verification_md"],
        "统一工资表：" + preview["files"]["workbook"],
        "",
        f"确认口令：{preview['token']}",
        preview["next_step"],
    ]
    if preview["blocking"]:
        lines += ["", "阻断项："] + ["· " + x for x in preview["blocking"][:6]]
    if preview["review"]:
        lines += ["", "需复核项："] + ["· " + x for x in preview["review"][:6]]
    text = "\n".join(lines) + "\n"
    result = {"channel": ncfg.get("channel", "dry-run"), "dry_run": dry_run,
              "target": owner_target, "text": text}
    if not dry_run and ncfg.get("channel") == "feishu" and owner_target:
        key = f"payroll-owner:{month}:{preview['token'][:8]}"
        ch = adapters.get_channel("feishu")
        result["send"] = ch.owner_send(owner_target, text, key, cfg)
    audit.log(cfg, month, "notify_owner", verdict=preview["verdict"],
              dry_run=dry_run, target=owner_target)
    return result


def dispatch(cfg: dict, month: str, dry_run: bool = True,
             channel: str | None = None, force: bool = False) -> dict:
    """确认后逐司机私发。返回逐人结果，绝不因单人失败而中断其他人。"""
    confirmation = gate.assert_dispatch_allowed(cfg, month)
    from compute import load_result
    result = load_result(cfg, month)
    slips = render.render_payslips(cfg, month, result)
    by_id = {s["driver"]: s for s in slips}
    roster = _roster(cfg)
    ncfg = cfg["payroll"].get("notify", {})
    ch = channel or ncfg.get("channel", "dry-run")
    effective_dry = dry_run or ch in ("dry-run", "dry_run")
    # 飞书为主源时，私发名单收敛到飞书在册司机；未启用飞书源的脱敏样例保持旧行为。
    feishu_only = (bool(ncfg.get("feishu_roster_only", True))
                   and bool(cfg.get("_roster_meta", {}).get("feishu_enabled")))

    results, missing, skipped = [], [], []
    for did, spec in sorted(roster.items()):
        slip = by_id.get(did)
        if slip is None:
            skipped.append({"driver": did, "reason": "当月无工资条"})
            continue
        if not spec.get("notify", True):
            skipped.append({"driver": did, "reason": "名册中 notify=false"})
            continue
        if feishu_only and spec.get("feishu") is not True:
            # 弹性名册口径：只记信息，绝不进 missing_open_id、绝不催办、绝不猜 open_id
            skipped.append({"driver": spec.get("name", did),
                            "reason": "不在飞书名册，按弹性口径不私发（仅信息记录）"})
            continue
        open_id = spec.get("open_id", "")
        if not open_id or not str(open_id).startswith("ou_"):
            missing.append({"driver": spec.get("name", did),
                            "reason": "open_id 缺失或格式不对，绝不猜"})
            audit.mark_sent(cfg, month, did, "skipped", "open_id 缺失", ch)
            continue
        if audit.was_sent(cfg, month, did) and not force:
            skipped.append({"driver": did, "reason": "本月已成功发送（幂等）"})
            continue
        key = f"payroll-dm:{month}:{did}:{result['result_hash'][:8]}"
        rec = {"driver": did, "name": spec.get("name", did),
               "open_id": open_id[:8] + "…", "key": key}
        if effective_dry:
            rec.update({"status": "dry-run", "channel": ch,
                        "preview": slip["text"][:400]})
            audit.log(cfg, month, "dispatch_dry", driver=did, key=key)
        else:
            channel = adapters.get_channel(ch)
            sent = channel.send(open_id, slip["text"], key, cfg)
            rec.update({"status": "sent" if sent.get("ok") else "failed",
                        "channel": ch, "detail": sent})
            audit.mark_sent(cfg, month, did, rec["status"],
                            json.dumps(sent, ensure_ascii=False)[:300], ch)
            audit.log(cfg, month, "dispatch_driver", driver=did,
                      status=rec["status"], key=key)
        results.append(rec)

    report = {
        "month": month, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": effective_dry, "channel": ch,
        "confirmed_by": confirmation["owner"],
        "confirmed_at": confirmation["confirmed_at"],
        "result_hash": result["result_hash"],
        "sent": sum(r["status"] in ("sent", "dry-run") for r in results),
        "failed": sum(r["status"] == "failed" for r in results),
        "results": results, "missing_open_id": missing, "skipped": skipped,
        "feishu_only_dispatch": feishu_only,
        "note": "只向名册内、notify=true、open_id 有效"
                + ("且飞书在册" if feishu_only else "")
                + "的司机一人一条直发；不群发、不降级到群、未确认不发送。",
    }
    out_dir = os.path.join(cfg["paths"]["out_dir"], month)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "dispatch.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    audit.log(cfg, month, "dispatch_done", dry_run=effective_dry,
              sent=report["sent"], failed=report["failed"],
              missing=len(missing))
    return report
