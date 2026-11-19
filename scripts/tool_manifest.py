"""生成通用 AI Agent 工具清单（config/agent_tools.json）。

清单是 CLI / function-calling / MCP 三种接入方式的共同真源。
业务 action 从 config/modules.json 自动读取，避免新增模块后清单过期。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
MODULES_PATH = ROOT / "config" / "modules.json"
OUT_PATH = ROOT / "config" / "agent_tools.json"

GROUP_BY_MODULE = {
    "reimbursement": "reimb",
    "wage": "wage",
    "summary": "summary",
    "monthly_sheet": "sheet",
}

RISK_BY_GROUP_ACTION = {
    ("meta", "list"): "read",
    ("meta", "config"): "read",
    ("meta", "doctor"): "read",
    ("reimb", "sync_roster"): "external",
    ("reimb", "resolve"): "read",
    ("reimb", "dedup_check"): "read",
    ("reimb", "dedup_batch"): "read",
    ("reimb", "ledger_add"): "write",
    ("reimb", "balance"): "read",
    ("reimb", "index_add"): "write",
    ("reimb", "write_detail"): "write",
    ("reimb", "group_task"): "external",
    ("reimb", "notify"): "external",
    ("reimb", "monthly"): "external",
    ("reimb", "mention_gate"): "read",
    ("reimb", "mention_classify"): "read",
    ("reimb", "mention_ack"): "external",
    ("reimb", "bot_check"): "read",
    ("reimb", "driver_copy"): "read",
    ("wage", "compute"): "write",
    ("wage", "verify"): "write",
    ("wage", "preview"): "write",
    ("wage", "confirm"): "write",
    ("wage", "dispatch"): "external",
    ("wage", "sync_roster"): "external",
    ("wage", "status"): "read",
    ("wage", "validate"): "read",
    ("wage", "init"): "write",
    ("summary", "generate"): "write",
    ("summary", "inspect"): "read",
    ("sheet", "inspect"): "read",
    ("sheet", "plan"): "read",
    ("sheet", "run"): "write",
    ("sheet", "selftest"): "read",
    ("intake", "parse"): "read",
    ("intake", "plan"): "read",
    ("intake", "run"): "write",
    ("intake", "selftest"): "read",
}

RISK_LABEL = {"read": "只读", "write": "本地写入", "external": "外部发送/远程读取"}


def _input_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "透传给该 action 的命令行参数，例如 [\"--month\", \"2026-08\"]。",
            },
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "args 的别名。",
            },
            "kwargs": {
                "type": "object",
                "additionalProperties": True,
                "description": "键值参数，自动转成 --key value；true 转成单个 --key 开关。",
            },
            "max_output_chars": {
                "type": "integer",
                "minimum": 1000,
                "description": "最大返回字符数，默认 50000；超出部分截断并标注。",
            },
        },
        "additionalProperties": False,
    }


def _tool(
    name: str,
    group: str,
    action: str,
    purpose: str,
    command: List[str],
) -> Dict[str, Any]:
    risk = RISK_BY_GROUP_ACTION.get((group, action), "write")
    return {
        "name": name,
        "group": group,
        "action": action,
        "purpose": purpose,
        "risk": risk,
        "risk_label": RISK_LABEL[risk],
        "command": command,
        "inputSchema": _input_schema(),
    }


def build_manifest() -> Dict[str, Any]:
    modules = json.loads(MODULES_PATH.read_text(encoding="utf-8"))["modules"]
    tools: List[Dict[str, Any]] = []

    for action in ("list", "config", "doctor"):
        purpose = {"list": "列出模块与动作", "config": "查看生效配置", "doctor": "静态自检"}[action]
        tools.append(_tool(
            f"logistics_{action}", "meta", action, purpose,
            ["python3", "scripts/logistics.py", action],
        ))

    for module_key, module in modules.items():
        group = GROUP_BY_MODULE[module_key]
        for action_key, action in module["delegates"].items():
            tools.append(_tool(
                f"logistics_{group}_{action_key}",
                group,
                action_key,
                f"{module['title']}：{action.get('purpose', action_key)}",
                ["python3", "scripts/logistics.py", group, action_key],
            ))

    for action, purpose in (
        ("parse", "解析运输单文本/附件为结构化明细"),
        ("plan", "只解析并生成执行计划，不写入"),
        ("run", "生成数据源并按配置调用月度制表写入"),
        ("selftest", "报单脱敏自测，不写真表、不连飞书"),
    ):
        tools.append(_tool(
            f"logistics_intake_{action}", "intake", action, purpose,
            ["python3", "scripts/logistics.py", "intake", action],
        ))

    return {
        "schema_version": 1,
        "name": "logistics-management",
        "title": "物流管理系统通用工具清单",
        "description": "供 CLI / function-calling / MCP 共用的工具描述；业务 action 由 modules.json 生成。",
        "runtime": {
            "cli": "python3 scripts/agent.py",
            "mcp": "python3 scripts/agent.py serve",
            "python_min": "3.10",
        },
        "security": {
            "read": "默认允许",
            "write": "需设置环境变量 LOGISTICS_ALLOW_WRITE=1（--help 除外）",
            "external": "需设置环境变量 LOGISTICS_ALLOW_EXTERNAL=1（--help 除外）",
            "note": "所有工具均以 argv 数组执行，不经过 shell；图片未转录、工资确认等硬闸门仍由业务脚本执行。",
        },
        "tools": tools,
    }


def main() -> None:
    manifest = build_manifest()
    OUT_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "path": str(OUT_PATH.relative_to(ROOT)),
        "tool_count": len(manifest["tools"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
