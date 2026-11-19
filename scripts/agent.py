"""通用 AI Agent 入口：工具发现、工具调用、MCP stdio 服务。

用法示例：
  python3 scripts/agent.py list
  python3 scripts/agent.py describe logistics_doctor
  python3 scripts/agent.py call logistics_list
  python3 scripts/agent.py call logistics_summary_inspect --args-json '["--dry-run"]'
  python3 scripts/agent.py serve
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from agent_runtime import find_tool, invoke, list_tools, load_manifest


def _dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_list(_: argparse.Namespace) -> None:
    _dump(load_manifest())


def cmd_describe(args: argparse.Namespace) -> None:
    tool = find_tool(args.tool)
    if not tool:
        _dump({"ok": False, "error": f"未知工具：{args.tool}",
               "available": sorted(t["name"] for t in list_tools())})
        sys.exit(3)
    _dump({"ok": True, "tool": tool})


def _parse_call_arguments(args: argparse.Namespace) -> Dict[str, Any]:
    sources = 0
    payload: Dict[str, Any] = {}
    if args.args_json:
        sources += 1
        parsed = json.loads(args.args_json)
        payload["args" if isinstance(parsed, list) else "kwargs"] = parsed
    if args.kwargs_json:
        sources += 1
        payload["kwargs"] = json.loads(args.kwargs_json)
    if args.stdin_json:
        sources += 1
        payload.update(json.loads(sys.stdin.read()))
    if args.arg:
        sources += 1
        payload["args"] = list(args.arg)
    if sources > 1:
        raise ValueError("--args-json / --kwargs-json / --stdin-json / --arg 只能选一种")
    return payload


def cmd_call(args: argparse.Namespace) -> None:
    try:
        payload = _parse_call_arguments(args)
    except Exception as exc:  # noqa: BLE001
        _dump({"ok": False, "error": f"参数解析失败：{exc}"})
        sys.exit(3)
    result = invoke(args.tool, payload, timeout=args.timeout)
    _dump(result)
    if not result.get("ok"):
        sys.exit(int(result.get("exit_code") or 2))


def cmd_serve(_: argparse.Namespace) -> None:
    from mcp_server import main as mcp_main

    mcp_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logistics-agent",
        description="物流管理系统通用 AI Agent 适配器（CLI + MCP）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="输出完整工具清单 JSON").set_defaults(fn=cmd_list)

    describe = sub.add_parser("describe", help="查看单个工具定义")
    describe.add_argument("tool", help="工具名，如 logistics_doctor")
    describe.set_defaults(fn=cmd_describe)

    call = sub.add_parser("call", help="调用一个工具")
    call.add_argument("tool", help="工具名")
    call.add_argument("--args-json", help="字符串数组 JSON，如 '[\"--month\",\"2026-08\"]'")
    call.add_argument("--kwargs-json", help="对象 JSON，如 '{\"month\":\"2026-08\"}'")
    call.add_argument("--stdin-json", action="store_true", help="从 stdin 读取完整 arguments JSON")
    call.add_argument("--arg", action="append", help="逐个追加参数，可重复使用")
    call.add_argument("--timeout", type=int, default=None, help="超时秒数，默认 600")
    call.set_defaults(fn=cmd_call)

    sub.add_parser("serve", help="启动 MCP stdio 服务器").set_defaults(fn=cmd_serve)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
