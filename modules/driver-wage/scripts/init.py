#!/usr/bin/env python3
"""引导式接入：把「手改 5 个 JSON」变成「指一下文件」。

面向任何有此类需求的人，无论用什么 IM、什么表格布局：
  payroll.py init --trip-file 运输单.xlsx --wage-dir 历史工资表/ \
                  --template 工资表模板.xlsx [--roster-file 名册.csv] \
                  --out-dir config/local

它会自动：
  1. 从模板抽取统一表格样式（字体/背景/颜色/列宽）-> style.json
  2. 从历史工资表反推每位司机的计薪规则候选           -> rules.candidates.json
  3. 从名册文件（可选）建立司机名册                   -> roster.json
  4. 生成草稿主配置 payroll.json / field_map.json
  5. 输出一份人话的 REVIEW.md，列清楚「已自动推断」与「你必须确认」的项

生成的规则全部 confirmed=false —— 设计上绝不自动放行，必须人核对后才算数。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

import adapters
import render


def _write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _build_roster(roster_file: str | None, out_dir: str) -> tuple[dict, str]:
    if roster_file:
        rows = adapters._read_table(roster_file)
        drivers = []
        for r in rows:
            did = str(r.get("id") or r.get("司机id") or r.get("工号") or "").strip()
            if not did:
                continue
            drivers.append({
                "id": did,
                "name": str(r.get("name") or r.get("姓名") or did).strip(),
                "open_id": str(r.get("open_id") or r.get("openid") or "").strip(),
                "notify": str(r.get("notify", "true") or "true").lower() != "false",
            })
        roster = {"drivers": drivers,
                  "note": f"由 init 从名册文件自动建立：{os.path.basename(roster_file)}（已脱敏）。"}
        src_kind = "csv"
        src_note = (f'名册来源已设为 csv，文件指向 {roster_file}；'
                    '若你用飞书，把 source 改为 feishu 并填 department_query / chat_id。')
    else:
        roster = {"drivers": [],
                  "note": "init 未收到名册文件，先留空。请补充 roster.json 的 drivers，"
                          "或重跑 init 带上 --roster-file。"}
        src_kind = "manual"
        src_note = "当前为 manual 来源（只读本文件）。可改成 csv（给一份名册文件）或 feishu。"
    _write(os.path.join(out_dir, "roster.json"), roster)
    return roster, src_note


def _build_rules(candidates_path: str, out_dir: str) -> tuple[dict, list[str]]:
    """把反推候选转成草稿 rules.json：保留算法、强制未确认。"""
    with open(candidates_path, "r", encoding="utf-8") as f:
        cand = json.load(f)
    drivers = cand.get("drivers", cand.get("rules", {}))
    out_drivers = {}
    notes = []
    for did, spec in drivers.items():
        spec = dict(spec)
        spec["confirmed"] = False
        spec.pop("confirmed_by", None)
        spec.pop("confirmed_at", None)
        spec.setdefault("source", "历史工资表（init 反推候选，待你确认）")
        out_drivers[did] = spec
    rules = {
        "version": 1, "precision": 2, "currency": "CNY",
        "note": "init 自动反推的计薪规则草稿（全部 confirmed=false）。请逐条核对后改 true。",
        "tables": cand.get("tables", {}),
        "drivers": out_drivers,
    }
    _write(os.path.join(out_dir, "rules.json"), rules)
    if out_drivers:
        notes.append(f"已反推 {len(out_drivers)} 位司机的计薪规则（候选，待确认）")
    else:
        notes.append("未能从工资表反推出任何规则；请手动填写 rules.json 的 drivers")
    return rules, notes


def run(root: str, trip_file: str | None, wage_dir: str | None,
        template: str | None, roster_file: str | None,
        out_dir: str, year: str = "2026") -> dict:
    os.makedirs(out_dir, exist_ok=True)
    steps: list[str] = []
    review_lines: list[str] = []

    # 1) 样式
    style_note = "未提供模板，将使用内置默认样式（黑字/白底/合计黄底）。"
    if template:
        style_path = os.path.join(out_dir, "style.json")
        render.extract_style(template, style_path, 1, 2, None)
        style_note = f"已从模板抽取统一样式 -> {os.path.basename(style_path)}"
    else:
        shutil.copyfile(os.path.join(root, "config", "local", "style.json")
                        if os.path.isfile(os.path.join(root, "config", "local", "style.json"))
                        else os.path.join(root, "examples", "config", "style.json"),
                        os.path.join(out_dir, "style.json"))
    steps.append(style_note)
    review_lines.append(f"- [x] 表格样式：{style_note}")

    # 2) 规则反推
    rules_cand = os.path.join(out_dir, "rules.candidates.json")
    if wage_dir:
        import learn_rules
        learn_rules.learn(wage_dir, rules_cand, year, None)
        rules, rn = _build_rules(rules_cand, out_dir)
        steps.extend(rn)
        review_lines.append(f"- [x] 计薪规则：{rn[0]}")
    else:
        _write(os.path.join(out_dir, "rules.json"),
               {"version": 1, "drivers": {},
                "note": "init 未收到历史工资表，请手动填写 rules.json。"})
        review_lines.append("- [ ] 计薪规则：未提供历史工资表，需你手动填写 rules.json")

    # 3) 名册
    roster, src_note = _build_roster(roster_file, out_dir)
    steps.append(src_note)
    review_lines.append(f"- [x] 司机名册：{len(roster['drivers'])} 位（{src_note}）")

    # 4) field_map（空别名，等用户补充报表写法差异）
    field_map = {
        "note": "把报表/文件名/表内出现的不同姓名写法映射成本地 id，例如 "
                "{\"幸荣超\": \"辛荣超\"}。init 无法自动判断，请按需补充。",
        "driver_aliases": {},
        "trip_columns": {},
        "reimbursement_columns": {},
    }
    _write(os.path.join(out_dir, "field_map.json"), field_map)

    # 5) payroll 主配置（草稿，channel 默认 dry-run，gate owner 待填）
    src_kind = "csv" if roster_file else "manual"
    payroll = {
        "version": 1, "precision": 2, "currency": "CNY",
        "paths": {"data_dir": "{root}/examples/data",
                  "out_dir": "{root}/out", "state_dir": "{root}/state"},
        "checks": {
            "amount_tolerance": "0.01", "variance_warn_pct": 40,
            "variance_block_pct": 90, "max_km": 3000,
            "duplicate_key": ["driver", "date", "route", "km", "trip_no"],
            "require_all_rules_confirmed": True,
        },
        "gate": {"owner": "<填写发起人姓名或飞书 open_id>",
                 "confirm_ttl_hours": 72,
                 "note": "确认是硬闸门：定时任务只跑到 preview，绝不代替发起人确认。"},
        "notify": {
            "channel": "dry-run",
            "owner_user_id": "",
            "message_template": "【工资条｜{month}】{name}\n应发 ¥{gross}（{trips} 趟 / {km} 公里）\n报销 ¥{reimbursement}　扣款 ¥{deductions}\n实发 ¥{net}\n明细见附件，如有疑问请回复核对。",
            "timeout": 30,
            "note": "channel 可选 dry-run / stdout / csv-export / feishu。默认 dry-run，"
                    "只有显式 --no-dry-run 才真发；csv-export 会导出每条工资条成文件。",
            "feishu_roster_only": True,
        },
        "schedule": {
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=10;BYHOUR=9;BYMINUTE=0",
            "timezone": "Asia/Shanghai",
            "scope": "只发起计算+核对+待确认预览，绝不自动私发",
        },
        "roster": {
            "source": src_kind,
            "merge": "union",
            "active_only": True,
            "note": src_note,
            "csv": {"path": roster_file} if roster_file else {},
            "feishu": {"cli": "lark-cli", "department_query": "",
                       "department_contains": "", "chat_id": "",
                       "exclude_names": [], "exclude_open_ids": [],
                       "exclude_external_users": True,
                       "cache_path": "{root}/state/feishu_roster.json",
                       "max_age_hours": 24, "timeout": 30, "page_size": 30},
        },
    }
    _write(os.path.join(out_dir, "payroll.json"), payroll)
    review_lines.append("- [ ] 主配置 payroll.json：请填 gate.owner（发起人）与 "
                        "notify.channel（csv-export / feishu 等）、roster 来源参数")

    # 6) REVIEW.md
    review = ["# 接入复核清单（init 自动生成）", "",
              f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}", "",
              "## 已自动完成", ""] + review_lines + [
        "", "## 你必须确认/补充（否则不会发放）", "",
        "1. **逐条核对计薪规则**：打开 rules.json，把每条 `confirmed` 改为 true 并填 "
        "`confirmed_by` / `confirmed_at`。未确认的司机当月会被核对阻断。",
        "2. **填发起人**：payroll.json 的 `gate.owner`（用于确认闸门与待确认通知）。",
        "3. **选通讯通道**：notify.channel —— 没有 IM 工具就选 `csv-export`（导出成文件手动发）；"
        "用飞书就选 `feishu` 并补 roster.feishu 参数。",
        "4. **别名与列映射**：field_map.json 里把报表里不同的姓名/列名写法映射过来。",
        "5. **跑一遍**：`python3 scripts/payroll.py --profile local demo` 看核对结论，"
        "再 `preview --month YYYY-MM` 确认无误后 `confirm` + `dispatch --no-dry-run`。",
        "", "## 设计边界", "",
        "本技能只做两件事：计算工资、核对工资。init 只是输入准备，不改任何历史工资表，"
        "生成的草稿全部在 config/local/（已被 .gitignore 排除），可安全开源。",
    ]
    review_path = os.path.join(out_dir, "REVIEW.md")
    with open(review_path, "w", encoding="utf-8") as f:
        f.write("\n".join(review) + "\n")

    return {"ok": True, "out_dir": out_dir, "steps": steps,
            "review": review_path, "files": [
                "payroll.json", "rules.json", "roster.json",
                "style.json", "field_map.json", "REVIEW.md"]}
