"""通用 Agent 调用层：发现工具、安全门控、以 argv 方式执行统一 CLI。

设计目标：
1. 任何支持 shell/function calling 的 AI agent 都能调用；
2. MCP 与 CLI 共用同一份工具清单；
3. 不经过 shell，不拼接命令字符串；
4. 本地写入/外部发送必须由环境变量显式开启。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "config" / "agent_tools.json"

TRUTHY = {"1", "true", "yes", "on"}
GATE_ENV = {"write": "LOGISTICS_ALLOW_WRITE", "external": "LOGISTICS_ALLOW_EXTERNAL"}


def _load_fallback_manifest() -> Dict[str, Any]:
    from tool_manifest import build_manifest  # type: ignore

    return build_manifest()


def load_manifest() -> Dict[str, Any]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return _load_fallback_manifest()


def list_tools() -> List[Dict[str, Any]]:
    return load_manifest()["tools"]


def find_tool(name: str) -> Optional[Dict[str, Any]]:
    for tool in list_tools():
        if tool["name"] == name:
            return tool
    return None


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def normalize_arguments(arguments: Any) -> Tuple[List[str], int, Optional[Dict[str, Any]]]:
    """把 MCP/function-call 参数转成 argv 列表。返回 (args, max_chars, error)。"""
    max_chars = 50000
    if arguments is None:
        return [], max_chars, None
    if isinstance(arguments, list):
        return [str(x) for x in arguments], max_chars, None
    if not isinstance(arguments, dict):
        return [], max_chars, {"error": "arguments 必须是 object 或 array"}

    data = dict(arguments)
    if "max_output_chars" in data:
        try:
            max_chars = max(1000, int(data.pop("max_output_chars")))
        except (TypeError, ValueError):
            return [], max_chars, {"error": "max_output_chars 必须是整数"}

    raw_args = data.pop("args", data.pop("argv", None))
    if raw_args is not None:
        if not isinstance(raw_args, list):
            return [], max_chars, {"error": "args/argv 必须是字符串数组"}
        args = [str(x) for x in raw_args]
    else:
        args = []

    kwargs = data.pop("kwargs", None)
    if kwargs is not None:
        if not isinstance(kwargs, dict):
            return [], max_chars, {"error": "kwargs 必须是 object"}
        for key, value in kwargs.items():
            flag = "--" + str(key).replace("_", "-")
            if value is True:
                args.append(flag)
            elif value is False or value is None:
                continue
            elif isinstance(value, (list, tuple)):
                for item in value:
                    args.extend([flag, str(item)])
            else:
                args.extend([flag, str(value)])

    if data:
        return [], max_chars, {"error": f"不支持的参数字段：{sorted(data)}"}
    return args, max_chars, None


def _gate_error(tool: Dict[str, Any], args: List[str]) -> Optional[Dict[str, Any]]:
    if any(a in ("-h", "--help") for a in args):
        return None
    risk = tool.get("risk", "read")
    if risk == "read":
        return None
    env_name = GATE_ENV.get(risk)
    if not env_name:
        return None
    if _truthy(os.environ.get(env_name)):
        return None
    return {
        "ok": False,
        "blocked": True,
        "reason": f"{tool['risk_label']}工具需设置 {env_name}=1 后才能执行",
        "gate_env": env_name,
        "exit_code": 3,
    }


def _build_argv(tool: Dict[str, Any], args: List[str]) -> List[str]:
    group = tool["group"]
    action = tool["action"]
    argv = [sys.executable, str(ROOT / "scripts" / "logistics.py")]
    if group == "meta":
        argv.append(action)
    elif group == "intake":
        argv.extend(["intake", action])
    else:
        argv.extend([group, action])
    argv.extend(args)
    return argv


def _truncate(text: str, max_chars: int) -> Tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n...[TRUNCATED]", True


def invoke(tool_name: str, arguments: Any = None, timeout: Optional[int] = None) -> Dict[str, Any]:
    tool = find_tool(tool_name)
    if not tool:
        return {
            "ok": False,
            "error": f"未知工具：{tool_name}",
            "available": sorted(t["name"] for t in list_tools()),
            "exit_code": 3,
        }

    args, max_chars, arg_error = normalize_arguments(arguments)
    if arg_error:
        return {"ok": False, "tool": tool_name, **arg_error, "exit_code": 3}

    blocked = _gate_error(tool, args)
    if blocked:
        return {"ok": False, "tool": tool_name, "risk": tool["risk"], "args": args, **blocked}

    argv = _build_argv(tool, args)
    base = {
        "tool": tool_name,
        "group": tool["group"],
        "action": tool["action"],
        "risk": tool["risk"],
        "args": args,
        "command": argv,
    }
    try:
        proc = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout or 600,
        )
    except subprocess.TimeoutExpired:
        return {**base, "ok": False, "error": "工具执行超时", "exit_code": 124}
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "error": f"工具执行失败：{exc}", "exit_code": 1}

    stdout, stdout_truncated = _truncate(proc.stdout or "", max_chars)
    stderr, stderr_truncated = _truncate(proc.stderr or "", max_chars)
    data: Optional[Any] = None
    try:
        data = json.loads(proc.stdout)
    except Exception:
        pass

    data_ok = True
    if isinstance(data, dict) and "ok" in data:
        data_ok = bool(data["ok"])
    return {
        **base,
        "ok": proc.returncode == 0 and data_ok,
        "exit_code": proc.returncode,
        "data": data,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
    }


def mcp_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": f"{t['purpose']}（风险：{t['risk_label']}）",
            "inputSchema": t["inputSchema"],
            "_meta": {
                "group": t["group"],
                "action": t["action"],
                "risk": t["risk"],
            },
        }
        for t in list_tools()
    ]
