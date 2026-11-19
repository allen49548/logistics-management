# Claude / Claude Code 接入说明

通用规则见 [AGENTS.md](AGENTS.md)。本项目不是某个平台专有 skill；Claude 可通过 shell 或 MCP 调用。

常用命令：

```bash
python3 scripts/agent.py list
python3 scripts/agent.py call logistics_doctor
python3 scripts/agent.py serve
```

MCP `stdio` 配置示例：

```json
{
  "mcpServers": {
    "logistics-management": {
      "command": "python3",
      "args": ["/absolute/path/to/logistics-management/scripts/agent.py", "serve"],
      "env": {
        "LOGISTICS_ALLOW_WRITE": "0",
        "LOGISTICS_ALLOW_EXTERNAL": "0"
      }
    }
  }
}
```
