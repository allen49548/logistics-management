"""物流管理系统 · 统一入口。

四大业务能力全部内置在 modules/ 下（modules.json 注册），本入口只做路由与聚合：
  - reimb     司机报销（modules/reimbursement）
  - wage      司机工资计算与核对（modules/driver-wage）
  - summary   月度汇总/工资及相关运输数据（modules/driver-month-report）
  - intake    报单：飞书群运输单 -> 各司机报表（本 skill 新增解析，写入调用内置月度制表模块）
  - sheet     月度制表（modules/driver-monthly-sheet，一般无需直接调用）
  - doctor    自检：配置、内置脚本、机器人配置是否就绪
  - config    查看当前生效配置（脱敏来源）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from common import (
    ROOT,
    EXIT_BUSINESS,
    EXIT_CONFIG,
    emit,
    expand,
    load_config,
    run_delegate,
)


def _delegate(argv: List[str], module_key: str, action_key: str) -> int:
    modules, cfg = load_config()
    mod = modules["modules"].get(module_key)
    if not mod:
        emit({"ok": False, "exit_code": EXIT_CONFIG, "error": f"未注册模块：{module_key}"})
        return EXIT_CONFIG
    action = mod["delegates"].get(action_key)
    if not action:
        emit({
            "ok": False, "exit_code": EXIT_CONFIG,
            "error": f"模块 {module_key} 无动作 {action_key}",
            "available": sorted(mod["delegates"]),
        })
        return EXIT_CONFIG
    result = run_delegate(mod, action, argv, timeout=cfg.get("runtime", {}).get("subprocess_timeout_sec"))
    # 子脚本的输出原样留在 stdout（它的 JSON 就是给上层消费的结果）；
    # 路由元数据走 stderr，避免污染子脚本的机器可读输出。
    payload = {
        "ok": result["ok"],
        "module": module_key,
        "action": action_key,
        "purpose": action.get("purpose", ""),
        "flow_doc": str(Path(expand(mod.get("skill_root", ""), ROOT) or "") / mod.get("flow_doc", "SKILL.md")),
        **result,
    }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return int(result.get("exit_code", EXIT_BUSINESS))


def cmd_list(args) -> None:
    modules, cfg = load_config()
    emit({
        "ok": True,
        "skill": "logistics-management（物流管理系统）",
        "system": "logistics-management（物流管理系统）",
        "config_source": cfg.get("_config_source"),
        "modules": {
            key: {
                "title": m["title"],
                "skill_root": m["skill_root"],
                "actions": {k: v.get("purpose", "") for k, v in m["delegates"].items()},
                "trigger_modes": m.get("trigger", {}).get("modes", []),
            }
            for key, m in modules["modules"].items()
        },
        "note": "业务逻辑全部位于本工具包的 modules/ 内置模块；不依赖外部 skill，可从任意 Agent 通过 CLI/MCP 调用。",
    })


def cmd_config(args) -> None:
    modules, cfg = load_config()
    safe = {k: v for k, v in cfg.items() if not k.startswith("_")}
    emit({"ok": True, "config_source": cfg.get("_config_source"), "config": safe,
          "modules_registered": sorted(modules["modules"])})


def cmd_doctor(args) -> None:
    modules, cfg = load_config()
    checks: List[Dict[str, Any]] = []
    for key, mod in modules["modules"].items():
        root = Path(expand(mod["skill_root"], ROOT) or "")  # type: ignore[arg-type]
        row: Dict[str, Any] = {"module": key, "root": str(root), "root_exists": root.exists()}
        missing = []
        for akey, action in mod["delegates"].items():
            script = root / action["script"]
            if not script.exists():
                missing.append(f"{akey}:{action['script']}")
        row["scripts_missing"] = missing
        row["ok"] = row["root_exists"] and not missing
        checks.append(row)
    intake_cfg = cfg.get("intake", {})
    emit({
        "ok": all(c["ok"] for c in checks),
        "config_source": cfg.get("_config_source"),
        "checks": checks,
        "intake_fields_configured": bool(intake_cfg.get("fields")),
        "intake_report_format": intake_cfg.get("report_format", {}),
        "bot_check": cfg.get("feishu", {}).get("bot_check", {}),
        "note": "机器人授权/群成员等动态状态由各内置模块 bot_check 在发送前自检；此处只查静态配置与内置脚本。",
    })


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logistics",
        description="物流管理系统：报销 / 工资 / 月度汇总 / 报单 统一入口（自包含）",
    )
    sub = p.add_subparsers(dest="group", required=True)

    sub.add_parser("list", help="列出模块与动作").set_defaults(fn=cmd_list)
    sub.add_parser("config", help="查看生效配置").set_defaults(fn=cmd_config)
    sub.add_parser("doctor", help="静态自检").set_defaults(fn=cmd_doctor)

    def delegation_group(name: str, module: str, help_text: str) -> None:
        g = sub.add_parser(name, help=help_text)
        gs = g.add_subparsers(dest="action", required=True)
        modules, _ = load_config()
        for akey, action in modules["modules"][module]["delegates"].items():
            sp = gs.add_parser(akey, help=action.get("purpose", ""))
            sp.add_argument("extra", nargs=argparse.REMAINDER, help="透传给目标脚本的参数")
            sp.set_defaults(module=module, action_key=akey)

    delegation_group("reimb", "reimbursement", "司机报销")
    delegation_group("wage", "wage", "司机工资计算与核对")
    delegation_group("summary", "summary", "月度汇总（工资及相关运输数据）")
    delegation_group("sheet", "monthly_sheet", "月度制表（报单写入复用）")

    gi = sub.add_parser("intake", help="报单：飞书群运输单 -> 各司机报表（本 skill 新增）")
    gi.add_argument("rest", nargs=argparse.REMAINDER)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.group == "intake":
        from intake import main as intake_main

        sys.argv = [sys.argv[0]] + list(args.rest or [])
        intake_main()
        return
    if hasattr(args, "module"):
        sys.exit(_delegate(args.extra, args.module, args.action_key))
    args.fn(args)


if __name__ == "__main__":
    main()
