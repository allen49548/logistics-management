"""零依赖 MCP stdio 服务器：把通用工具清单暴露为 MCP tools。

仅使用 Python 标准库，通过换行分隔 JSON-RPC 2.0 消息通信。
stdout 只输出 JSON-RPC 响应；所有诊断信息写 stderr。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from agent_runtime import invoke, mcp_tools

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "logistics-management", "version": "1.0.0"}
INSTRUCTIONS = (
    "物流管理系统工具包。先用 tools/list 发现工具；read 工具可直接调用。"
    "本地写入需 LOGISTICS_ALLOW_WRITE=1，外部发送/远程读取需 LOGISTICS_ALLOW_EXTERNAL=1。"
    "图片未转录、工资确认、外发等硬闸门仍由业务脚本执行，Agent 不得绕过。"
)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPServer:
    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, RpcError(-32600, "Invalid Request"))

        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            self._notification(method)
            return None

        try:
            if method == "initialize":
                result = self._initialize(message.get("params") or {})
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": mcp_tools()}
            elif method == "tools/call":
                result = self._tools_call(message.get("params") or {})
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                raise RpcError(-32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            return self._error(request_id, exc)
        except Exception as exc:  # noqa: BLE001
            return self._error(request_id, RpcError(-32603, f"Internal error: {exc}"))

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        return {
            "protocolVersion": requested,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {},
                "prompts": {},
            },
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def _notification(self, method: Any) -> None:
        if method not in ("notifications/initialized", "notifications/cancelled"):
            print(f"[mcp] ignored notification: {method}", file=sys.stderr)

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(-32602, "params.name must be a string")
        result = invoke(name, params.get("arguments") or {})
        text = json.dumps(result, ensure_ascii=False, indent=2)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": not bool(result.get("ok")),
        }

    def _error(self, request_id: Any, exc: RpcError) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _send(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    server = MCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _send(server.handle({}) or {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            })
            continue
        response = server.handle(request)
        if response is not None:
            _send(response)


if __name__ == "__main__":
    main()
