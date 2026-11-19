# 通用 AI Agent 接入指南

本工具包可在任意支持 **shell 命令**或 **MCP stdio** 的 AI agent 中运行。WorkBuddy 只是可选加载入口，不是运行前提。

---

## 1. 三种接入方式

| 方式 | 适用 agent | 入口 |
|---|---|---|
| 直接 CLI | Claude Code、Cursor、OpenAI Codex、终端型 agent | `logistics` / `logistics-agent` |
| MCP stdio | Claude Desktop、Cursor、Windsurf、Cline 等 MCP 客户端 | `python3 scripts/agent.py serve` |
| Function calling | Dify、Coze、LangChain、自建 agent | 把 `config/agent_tools.json` 的 `inputSchema` 注册为工具，后端调用 `scripts/agent.py call` |

---

## 2. 安装

假设工具包目录为 `<ROOT>`：

```bash
cd <ROOT>
python3 -m pip install -r requirements.txt
cp config/config.example.json config/local/config.json   # 如尚未创建
python3 scripts/logistics.py doctor
./install.sh                                            # 可选：安装 logistics / logistics-agent 到 ~/.local/bin
export PATH="$HOME/.local/bin:$PATH"                    # 如未加入 PATH
```

纯汇总/制表不需要飞书；报销、工资名册、通知、报单群消息需要 `lark-cli`：

```bash
# 任意一种即可
export LOGISTICS_LARK_CLI=/path/to/lark-cli
export LARK_CLI=/path/to/lark-cli
# 或者把 lark-cli 放进 PATH
```

不需要把目录放在 `~/.workbuddy/skills/`；复制到任意路径后，`scripts/` 内路径均相对项目根解析。

---

## 3. CLI 发现与调用

```bash
# 发现全部工具（38 个）
python3 scripts/agent.py list

# 查看单个工具
python3 scripts/agent.py describe logistics_summary_generate

# 调用只读工具
python3 scripts/agent.py call logistics_doctor

# 透传参数
python3 scripts/agent.py call logistics_summary_inspect --args-json '["--dry-run"]'

# 用键值对自动转 --key value
python3 scripts/agent.py call logistics_intake_plan --kwargs-json '{"year":2026,"month":8}'

# 直接统一 CLI 亦可
python3 scripts/logistics.py reimb balance
```

`agent.py call` 返回**单一 JSON**，包含 `ok`、`exit_code`、`data`、`stdout`、`stderr`，方便 agent 解析。

安全开关：

```bash
LOGISTICS_ALLOW_WRITE=1 python3 scripts/agent.py call logistics_summary_generate --args-json '["--dry-run"]'
LOGISTICS_ALLOW_EXTERNAL=1 python3 scripts/agent.py call logistics_reimb_bot_check
```

`--help` 不受安全开关限制，可用于检查任何工具参数。

---

## 4. MCP stdio 接入

启动命令：

```bash
python3 <ROOT>/scripts/agent.py serve
```

协议：换行分隔的 JSON-RPC 2.0，支持：

- `initialize`
- `ping`
- `tools/list`
- `tools/call`
- `resources/list`（空）
- `prompts/list`（空）

### Claude Desktop / Cursor / Windsurf 配置

把 `<ROOT>` 换成实际绝对路径：

```json
{
  "mcpServers": {
    "logistics-management": {
      "command": "python3",
      "args": ["/absolute/path/to/logistics-management/scripts/agent.py", "serve"],
      "env": {
        "LOGISTICS_ALLOW_WRITE": "0",
        "LOGISTICS_ALLOW_EXTERNAL": "0",
        "LOGISTICS_LARK_CLI": "/absolute/path/to/lark-cli"
      }
    }
  }
}
```

如需开放本地写入，把 `LOGISTICS_ALLOW_WRITE` 设为 `1`；如需发送飞书/建任务，再把 `LOGISTICS_ALLOW_EXTERNAL` 设为 `1`。默认双关是安全值。

### tools/call 参数

```json
{
  "name": "logistics_wage_preview",
  "arguments": {
    "args": ["--month", "2026-08"]
  }
}
```

也支持：

```json
{"kwargs": {"month": "2026-08", "dry_run": true}}
```

`true` 会转成单个开关 `--dry-run`，`false` 会忽略。

---

## 5. Function calling / LangChain / Dify / Coze

`config/agent_tools.json` 中每个 `tools[]` 都有标准 JSON Schema `inputSchema`。注册工具名和 schema 后，后端统一执行：

```bash
python3 <ROOT>/scripts/agent.py call <tool_name> --stdin-json
```

stdin 示例：

```json
{"args": ["--month", "2026-08"]}
```

LangChain 伪代码：

```python
result = subprocess.run(
    ["python3", ROOT + "/scripts/agent.py", "call", tool_name],
    input=json.dumps(arguments),
    text=True, capture_output=True,
)
payload = json.loads(result.stdout)
```

Dify/Coze/n8n 如果没有内置 shell 节点，可用一个最小 HTTP 包装服务调用同一命令；业务逻辑不要重写。

---

## 6. 安全模型

| risk | 含义 | 默认 | 开启变量 |
|---|---|---|---|
| `read` | 查询、自检、解析、计划 | 允许 | 无 |
| `write` | 本地写表、生成结果、入账 | 拦截 | `LOGISTICS_ALLOW_WRITE=1` |
| `external` | 飞书发送、建任务、名册同步 | 拦截 | `LOGISTICS_ALLOW_EXTERNAL=1` |

仍由业务脚本执行的硬闸门：

- 图片运输单未转录 → 不猜数。
- 工资 `verify` 为 BLOCK → 停止；`confirm` 只能由发起人完成。
- 外发前 `bot_check`；无数据不重复推送。
- 写入前备份，写后回读校验，失败回滚。

Agent 不得通过改配置文件、直接调用内部脚本或伪造参数绕过这些闸门。

---

## 7. 验证接入是否成功

```bash
python3 scripts/agent.py list
python3 scripts/agent.py call logistics_list
python3 scripts/agent.py call logistics_doctor
python3 scripts/agent.py call logistics_intake_selftest
python3 tests/test_agent_adapter.py
```

预期：全部返回 `ok=true`；MCP `tools/list` 返回 38 个工具；`tools/call logistics_doctor` 返回 `ok=true`。

---

## 8. 常见问题

**Q：MCP 客户端连不上？**  
A：确认使用绝对路径，`python3` 可执行，且没有把日志打到 stdout。本服务 stdout 只输出 JSON-RPC。

**Q：调用写工具返回 blocked？**  
A：这是设计行为。确认用户意图后设置对应环境变量，优先 dry-run。

**Q：飞书工具说 lark-cli 不可用？**  
A：把 `lark-cli` 放进 PATH，或设置 `LOGISTICS_LARK_CLI`。WorkBuddy 用户也可启用飞书连接器作为 CLI 来源。

**Q：能不能只搬走这个目录？**  
A：可以。`modules/` 内包含业务脚本、配置和必要 state；无需外部 skill。复制后重跑 `doctor`。
