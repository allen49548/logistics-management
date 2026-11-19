---
name: logistics-management
description: 物流管理系统（自包含通用工具包，兼容 WorkBuddy skill 与任意 AI Agent）。统一承载司机报销、司机工资计算与核对、月度汇总（工资及相关运输数据）、月度制表和报单（飞书群运输单生成司机报表），并通过 CLI、function calling 与 MCP stdio 暴露。四大原能力与真实配置/state 已内置到 modules/，不依赖任何外部 skill。触发词：物流管理系统、司机报销、报销归集、算工资、工资核对、月度汇总、月度报表、报单、运输单生成报表、飞书运输单、司机报表、汇总工资运输数据。
agent_created: true
---

# 物流管理系统（logistics-management）

一个自包含、跨平台的物流业务工具包：把冷链物流车队的 **报销 / 工资 / 汇总 / 制表 / 报单** 全放在本目录内。
WorkBuddy 可加载本目录作为 skill；Claude/Cursor/OpenAI Agents/LangChain/Dify 等可通过 CLI、function calling 或 MCP 调用。
`modules/` 下是四个内置业务模块；不再调用、不依赖 `~/.workbuddy/skills/` 里的其他 skill。

## 一、边界（最高优先级）

1. **自包含**：报销、工资、汇总、制表的脚本、配置、文档、state 均已复制到 `modules/`；
   本 skill 的 `config/modules.json` 只指向 `modules/` 内置路径。
2. **唯一新增能力**是 `scripts/intake.py`：飞书运输单 → 结构化明细 → 按司机分组 → 标准数据源。
   写入调用内置 `monthly_sheet` 模块，保证与现有司机报表格式一致。
3. **不猜数**：图片运输单、低置信度、缺字段一律标待确认/待转录，不臆造金额、里程或司机。
4. **配置化**：字段别名、司机归属、去重键、月份、路径、阈值、写入策略全在 `config/`；
   代码里没有真实司机名、金额、群 chat_id 或固定结果。
5. **真实数据只维护本 skill 内部副本**：旧 skill 目录若仍存在，只是历史副本；本 skill 不再读取它们。
   后续口径变更应改 `modules/` 内对应模块与配置，避免双真源。
6. **飞书规则沿用内置实现**：bot 自检、无数据不发群、不重复推送、异常才私聊、@ 先回应再执行、
   权限分级，全部走内置 `modules/reimbursement/scripts/mention.py` 与各模块 `bot_check.py`。
7. **平台无关**：`scripts/agent.py` 是通用入口；`config/agent_tools.json` 是工具清单真源，
   MCP 只做协议适配，所有 agent 共用同一业务脚本与安全门控。WorkBuddy 仅是可选加载方式。

## 二、内置模块

| 模块 key | 中文 | 内置路径 | 入口命令 |
|---|---|---|---|
| `reimbursement` | 司机报销 | `modules/reimbursement/` | `logistics reimb <action>` |
| `wage` | 司机工资计算与核对 | `modules/driver-wage/` | `logistics wage <action>` |
| `summary` | 月度汇总 | `modules/driver-month-report/` | `logistics summary <action>` |
| `monthly_sheet` | 月度制表 | `modules/driver-monthly-sheet/` | `logistics sheet <action>` |
| `intake` | 报单（新增） | `scripts/intake.py` + 内置 monthly_sheet | `logistics intake parse\|plan\|run\|selftest` |
| `agent` | 通用 Agent 适配 | `scripts/agent.py`、`scripts/mcp_server.py` | `agent list\|describe\|call\|serve` |

命令模板见 `config/modules.json`；工具清单见 `config/agent_tools.json`；可用 action 用 `logistics list` 或 `agent list` 查看。

## 三、统一入口

```bash
cd /path/to/logistics-management

python3 scripts/agent.py list                      # 通用 Agent 工具清单
python3 scripts/agent.py call logistics_doctor     # 通用工具调用
python3 scripts/agent.py serve                     # MCP stdio 服务
python3 scripts/logistics.py list                  # 列模块/动作/触发方式
python3 scripts/logistics.py config                # 查看生效配置来源
python3 scripts/logistics.py doctor                # 静态自检（内置脚本/配置是否齐全）
python3 scripts/logistics.py reimb balance         # 报销未结
python3 scripts/logistics.py wage preview --month 2026-08
python3 scripts/logistics.py summary generate      # 按配置重生成月度汇总
python3 scripts/logistics.py sheet plan --year 2026 --month 8
python3 scripts/logistics.py intake selftest       # 报单脱敏自测
```

约定：内置脚本的 stdout 原样透传（它就是结果 JSON）；路由元数据写 stderr。

## 四、触发方式

- **命令行/对话**：用户说「报销归集 / 算工资 / 生成月度汇总 / 把群里运输单做成司机报表」等，
  由任意 Agent 解析意图后调用 `agent call <tool>` 或 `logistics <group> <action>`。
- **MCP / function calling**：客户端连接 `agent serve` 或注册 `config/agent_tools.json` 的 schema，
  由工具名路由到同一业务脚本。
- **群 @**：唯一门槛与权限真源是内置 `modules/reimbursement/scripts/mention.py`
  （gate/classify/ack）。先过群/内容门槛、判定角色、发一句调侃「收到」，再执行。
- **定时**：报销月初月报、工资月结沿用内置模块口径；调度器由部署环境决定（cron、WorkBuddy、Airflow 等），
  本工具包只提供命令，不擅自新建自动化。
- 飞书消息读取固定 `--as user`，发送统一用配置指定的 bot；发送前跑 bot 自检。

## 五、报单流程（新增能力）

```
飞书群运输单（文本/图片转录/附件表）
   → intake parse/plan：字段别名识别 → 司机归属 → 三方去重（批内+历史池）
   → 生成标准数据源 xlsx（列/表头来自配置）
   → 调用内置 monthly_sheet.run --source <数据源> --year --month
   → 各司机 {司机}{年}.xlsx 内新增/填入 {year}年{month}月 sheet
```

- 默认 dry-run；`--no-dry-run` 才真写。
- 写入复用内置月度制表的 zip 层精确插入，自带备份 + 回读校验 + 失败回滚，保护 WPS 特有部件。
- 图片/低置信度记录先转录为表格，用 `--attachment` 传入；未转录时 `run` 拒绝猜数并列出条数。
- 司机无目标报表文件时按内置月度制表的 `on_missing_target` 处理，不新建文件。

## 六、输入输出与数据依赖

- 输入：飞书群消息、运输单附件、各司机 `{司机}{年}.xlsx`、
  内置报销台账 `modules/reimbursement/state/ledger.sqlite`、
  内置工资配置 `modules/driver-wage/config/local/`、
  内置汇总配置 `modules/driver-month-report/config/`、内置制表配置。
- 输出：报单数据源 `state/intake/{year}年{month}月运输单.xlsx`；各司机报表新月份 sheet；
  月度汇总表；所有飞书通报由内置模块的通知脚本发出。
- 依赖链：报单写入前需目标司机报表已存在；工资的报销加项读内置台账；
  汇总读各司机月份 sheet。

## 七、验收清单

- [ ] `python3 tests/test_skill.py` 与 `python3 tests/test_agent_adapter.py` 全绿。
- [ ] `python3 scripts/logistics.py doctor` 全 ok，且所有 root 在 `modules/` 下。
- [ ] `python3 scripts/agent.py list` 输出 38 个工具；MCP `tools/list` 可发现同一批工具。
- [ ] `python3 scripts/logistics.py intake selftest` 通过，不写真表、不连飞书。
- [ ] 静态搜索无 `~/.workbuddy/skills/<旧 skill>` 执行路径。
- [ ] 真实配置与 state 已 gitignore；配置模板中无真实姓名/金额/chat_id。

## 八、待确认

见 `docs/待确认清单.md`。重点是：运输单真实格式与字段顺序、报表样张、司机归属优先级、
车贷 10500/11000 口径、报销明细 sheet 名固定问题、报单是否需要确认任务与通报。

详细规则见 `README.md` 与 `docs/`；跨平台接入见 `docs/通用Agent接入.md`。
