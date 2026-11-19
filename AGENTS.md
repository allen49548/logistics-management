# 物流管理系统 · 通用 Agent 使用规则

本目录是一个**自包含物流业务工具包**，不要求运行在某个特定 AI 平台内。WorkBuddy skill 只是可选入口。

## 发现能力

```bash
python3 scripts/agent.py list
python3 scripts/agent.py describe logistics_doctor
```

工具清单真源：`config/agent_tools.json`（由 `scripts/tool_manifest.py` 从 `config/modules.json` 生成）。

## 调用能力

优先使用通用适配器：

```bash
python3 scripts/agent.py call logistics_doctor
python3 scripts/agent.py call logistics_summary_inspect --args-json '["--dry-run"]'
python3 scripts/agent.py call logistics_intake_plan --kwargs-json '{"year":2026,"month":8}'
```

也可直接调用统一 CLI：

```bash
python3 scripts/logistics.py list
python3 scripts/logistics.py reimb balance
python3 scripts/logistics.py wage compute --month 2026-08
```

## MCP 接入

```bash
python3 scripts/agent.py serve
```

使用换行分隔的 JSON-RPC 2.0 stdio 协议，支持 `initialize` / `tools/list` / `tools/call` / `ping`。

## 安全边界

- `read` 工具默认允许。
- `write` 工具必须设置 `LOGISTICS_ALLOW_WRITE=1`，且优先使用 dry-run。
- `external` 工具必须设置 `LOGISTICS_ALLOW_EXTERNAL=1`；发送飞书、建任务、同步名册都属此类。
- `--help` 永远允许，便于任何 agent 检查参数。
- 图片运输单未转录时不得猜金额/里程；工资 `confirm` 必须由发起人完成；`BLOCK` 状态必须停止。
- 不得绕过内置去重、确认任务、bot 自检、备份与回滚机制。

## 运行环境

- Python 3.10+，`openpyxl`、`Pillow`。
- 飞书相关功能需要 `lark-cli` 在 PATH，或设置 `LOGISTICS_LARK_CLI` / `LARK_CLI` 指向它。
- 本工具包不依赖外部 skill；历史 WorkBuddy 自动化只需改指向本目录命令即可继续工作。
