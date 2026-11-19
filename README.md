# 物流管理系统（logistics-management）

一个**自包含、跨平台**的物流业务工具包：冷链物流车队的 **司机报销、司机工资计算与核对、
月度汇总、月度制表、报单** 全部在本目录内完成，不调用任何外部 skill。

它可由任意支持 shell、function calling 或 MCP stdio 的 AI agent 调用；WorkBuddy 只是可选加载入口，
不是运行前提。可直接复制到任意目录使用。

> 四大原能力的脚本、配置、文档、真实 state 已内置到 `modules/`。
> 旧 skill 目录若仍存在，只是历史副本；本工具包不再读取它们。后续只维护本目录内部副本。

文档导航：

- [通用 Agent 接入](docs/通用Agent接入.md)：CLI / MCP / function calling 的安装与接入示例。
- [功能点清单](docs/功能点清单.md)：38 个功能点的名称、用途、输入/输出、依赖、触发方式。
- [自测报告](docs/自测报告.md)：逐项测试步骤、预期结果、实际结果。
- [模块划分与数据流](docs/模块划分与数据流.md)、[触发与入口](docs/触发与入口.md)、[报单功能](docs/报单功能.md)、[待确认清单](docs/待确认清单.md)。

---

## 1. 系统定位

`一个统一入口 + 一个通用 Agent 适配层 + 四个内置业务模块 + 一个新报单能力`：

- **统一入口**：`scripts/logistics.py` 负责意图路由、状态查询、静态自检。
- **通用适配层**：`scripts/agent.py` 提供工具发现/调用/MCP stdio；`config/agent_tools.json` 是工具清单。
- **内置模块**：`modules/reimbursement`、`modules/driver-wage`、
  `modules/driver-month-report`、`modules/driver-monthly-sheet`。
- **新能力**：`scripts/intake.py` 把飞书群运输单解析成结构化明细，再调用内置月度制表写入。
- **配置真源**：各内置模块自己的配置；`config/modules.json` 只声明内置模块根与 action 命令。

为什么内置而不是仅委托：避免双真源与外部路径依赖。复制时保留了原业务口径、WPS zip 层精确写入、
报销四方池六层去重、工资确认硬闸门等已验证逻辑。

---

## 2. 功能模块

| 模块 | 职责 | 位置 | 触发 |
|---|---|---|---|
| 司机报销 | 付款凭证截图归集、四方池去重、写唯一报销明细 sheet、确认任务、两层通报、月初月报 | `modules/reimbursement/` | 手动 / 群 @ / 自动化 |
| 司机工资 | 按规则计算 + 六类核对 → 待确认预览 → 发起人确认 → 一人一条私发 | `modules/driver-wage/` | 手动 / 每月 10 号自动化 |
| 月度汇总 | 识别运费/工资/支出 → 统一口径净利润 → 只与上月环比 → 三张表 | `modules/driver-month-report/` | 手动 |
| 月度制表 | 按模板在各司机表插入月份 sheet，填运输数据与报销 | `modules/driver-monthly-sheet/` | 手动 / 报单写入复用 |
| **报单** | 飞书运输单解析、字段识别、司机归属、去重、生成数据源 | `scripts/intake.py` | 手动 / 可配置定时 |

边界原则：

- 统一入口只路由；业务脚本全在 `modules/`，不跨模块 import。
- 唯一允许的跨模块数据依赖是工资/制表**只读**报销台账。
- 写入默认 dry-run 或有硬闸门；外发前必须 `bot_check`。
- 每个 action 的详细说明见 [功能点清单](docs/功能点清单.md)。

---

## 3. 司机报销流程（内置 reimbursement）

1. `driver_roster.resolve`：按发送者确定司机，弹性归属，不猜、不催全员。
2. `ledger.check-batch`：四方池六层去重；命中跳过，不重复计算。
3. `reimb_detail.apply`：在已存在司机报表写唯一「N月报销明细」sheet；
   zip 层插入、备份 + 回读校验 + 失败回滚。
4. `group_task.create`：本轮每位司机新入账建确认任务并挂「报销」清单；无任务=未闭环。
5. `notify.py`：物流部群极简通报 + 负责人私聊专业版；无新报销群里一句「暂无新报销」。
6. `monthly_summary.py`：每月 1–4 号月初月报，按司机 + 四态 + 环比。

**四态**：已报销、已登记·待打款（唯一计入未结）、需补材料、重复·未加钱、作废。  
**铁律**：只认付款凭证截图；手写票/纯文本不记录；幂等以订单号 + 支付时间为准。

---

## 4. 司机工资计算与核对（内置 driver-wage）

固定顺序，不可跳步：

```
sync-roster → compute → verify → preview → 发起人 confirm → dispatch
                          ↑ BLOCK 即停   ↑ 硬闸门（定时只到 preview）
```

- **compute**：按 `modules/driver-wage/config/local/rules.json` 的路线价目/公式逐行求值，Decimal 精度；
  报销作加项从内置台账读，车贷/油耗/管理费/净利润不读不算。
- **verify**：独立路径复算 + 多类核对（完整性、勾稽、规则合规、报销口径、环比异常等）；
  PASS / REVIEW / BLOCK 三态；BLOCK 禁止确认与私发。
- **confirm**：发起人本人核对后用 token 确认；确认与指纹绑定、有有效期；Agent 不得代确认。
- **dispatch**：默认 dry-run；one-per-driver 只发给飞书在册、`notify=true`、有效 open_id 的司机。
- 名册以飞书为主、本地补充；只算当月实际有单/报销的司机，不铺零值行。

---

## 5. 月度汇总逻辑（内置 driver-month-report）

- 读各司机 `{司机}{年}.xlsx` 的 `{year}.{month}` sheet，识别 **运费收入 / 工资 / 其他支出**；
  同义标签（车贷支出/月供等）归并到同一 key，绝不累加。
- 统一口径：`净利润 = 运费收入 − 工资 − 其他支出`；缺数据标「无法计算/不可比」，不臆造。
- 环比只与配置的上一个月比；按阈值判定可比性；涨红跌绿。
- 生成前先把同月旧表搬进 `.recycle/<时间戳>/`，避免重复数据。
- 输出三张表：月度汇总、项目明细、口径与校验。

---

## 6. 报单功能（新增）

### 输入
- 文本消息：`字段: 值` 或按列顺序分隔。
- 图片消息：标记待转录，需视觉识别后用 `--attachment` 传 xlsx/csv。
- 附件 xlsx/csv：按表头别名映射字段。

### 处理
1. `intake.fields` 定义语义键与别名，最长别名优先；识别不出留空。
2. `intake.driver_from` 决定司机归属（字段/发送者/两者），再过 `drivers.aliases` 归一。
3. `dedup.key_fields` 组合键，批内 + `state/intake/seen.jsonl` 历史池；命中跳过。
4. `data_source.columns` 指定输出列，写出标准 xlsx。
5. 调用 `modules/driver-monthly-sheet` 的 `run --source`，在各司机表写入月份 sheet。默认 dry-run。

### 命令
```bash
python3 scripts/logistics.py intake parse --messages m.json --out parsed.json
python3 scripts/logistics.py intake plan  --messages m.json --year 2026 --month 8
python3 scripts/logistics.py intake run   --messages m.json --attachment trips.xlsx \
       --year 2026 --month 8 [--no-dry-run]
python3 scripts/logistics.py intake selftest
```

---

## 7. 数据流转

```
飞书群凭证截图 ──> [内置报销] ──> modules/reimbursement/state/ledger.sqlite
                                      │(报销加项)
飞书运输单 ──> [报单 intake] ──> 标准数据源 ──> [内置月度制表] ──> 各司机 {司机}{年}.xlsx 月份 sheet
                                                                      │
宝旺趟数/伟豪账单 ──> [内置工资 compute/verify] <── 飞书名册 ─────────┘
                          │(preview→confirm→dispatch)
各司机月份 sheet ──> [内置月度汇总] ──> {年}{月}汇总.xlsx
所有外发 ──> 内置模块的 notify.py（bot 自检 + 白名单 + 去重 + 降级）
```

---

## 8. 关键业务规则

- 不重复计算：报销订单号+支付时间四方池；报单配置组合键 + 历史池；车贷同义标签只取一值。
- 不臆造：缺 sheet / 缺字段 / 图片未转录 / 跨表数值冲突 → 标原因，不填数。
- 闸门：工资核对 BLOCK 即停、确认需发起人；报销每笔新入账必须有确认任务。
- 隐私：群内不 @、不写权限；工资一人一条私发；不重复推送；异常才私聊负责人。
- 幂等与可回滚：写入前备份、写后回读校验、失败回滚；同月重生成先回收旧表。
- 配置化：项目名/字段别名/月份/公式/路径/阈值/文案全在 config，代码不含固定结果。

---

## 9. 环境依赖

这些都是运行环境依赖，不是外部 skill 依赖。

| 依赖 | 用途 | 必需性 |
|---|---|---|
| Python 3.10+ | 运行所有脚本 | 必需 |
| `openpyxl>=3.1` | 读写 xlsx、zip 层精确插入 | 必需 |
| `Pillow>=9.0` | 报销凭证图片感知哈希去重 | 必需 |
| `lark-cli` | 拉飞书群消息、发通知、建任务、同步名册 | 报销/工资/报单必需；纯汇总制表不需要 |
| 任意 AI Agent（可选） | 通过 CLI / MCP / function calling 调用 | 可选；也可纯命令行运行 |

`lark-cli` 只要在 PATH 中即可；也可用 `LOGISTICS_LARK_CLI` 或 `LARK_CLI` 指定绝对路径。
WorkBuddy 不需要安装；如用它加载，只是多一个触发入口。机器人配置见 `modules/reimbursement/飞书机器人配置教程.md`。

---

## 10. 安装步骤

```bash
# 1. 进入工具包目录（可放在任意路径，不要求 WorkBuddy）
cd /path/to/logistics-management

# 2. 安装 Python 依赖
python3 -m pip install -r requirements.txt

# 3. 准备顶层脱敏配置的本地副本
mkdir -p config/local
cp config/config.example.json config/local/config.json

# 4. 填写真实路径、chat_id、open_id、字段别名等
#    编辑 config/local/config.json

# 5. 静态自检
python3 scripts/logistics.py doctor
python3 scripts/agent.py call logistics_doctor

# 6. 模块与适配器自检
python3 scripts/logistics.py intake selftest
python3 tests/test_skill.py
python3 tests/test_agent_adapter.py

# 7. 可选：安装全局命令 logistics / logistics-agent
./install.sh
export PATH="$HOME/.local/bin:$PATH"
```

确认 `doctor` 输出 `ok=true` 且四个模块 root 都在 `modules/` 下，即可开始运行。
其它 AI agent 的接入步骤见 [通用 Agent 接入](docs/通用Agent接入.md)。

---

## 11. 配置说明

| 配置文件 | 用途 | 是否含真实数据 |
|---|---|---|
| `config/modules.json` | 模块根、action 命令、触发模式；执行路由真源 | 否，一般不改 |
| `config/config.example.json` | 报单字段别名、司机归属、去重键、写入策略、触发口径模板 | 否 |
| `config/local/config.json` | 顶层真实配置；由 example 复制 | 是，已 gitignore |
| `modules/reimbursement/config.json` | 报销群、白名单、司机归属、文案、状态口径 | 是，已 gitignore |
| `modules/driver-wage/config/local/*.json` | 工资规则、价目、名册、样式、字段映射 | 是，已 gitignore |
| `modules/driver-month-report/config/report.json` | 月份、输出目录、环比、公式、列与文案 | 含真实路径 |
| `modules/driver-month-report/config/item_rules.json` | 运费/工资/支出识别别名与车贷去重规则 | 否 |
| `modules/driver-monthly-sheet/config/local/config.json` | 模板、数据源、目标表、报销台账路径 | 是，已 gitignore |

运行时 state：

- `modules/reimbursement/state/`：台账、运行清单、去重历史池、bot 缓存
- `modules/reimbursement/lark-im-resources/`：飞书凭证图片
- `modules/driver-wage/state/`：确认密钥、名册缓存、工资状态
- `modules/driver-wage/out/`：真实工资条
- `state/intake/`：报单数据源与去重池

以上路径均已 gitignore。口径变更改配置，不改代码；旧 skill 目录不再是配置真源。

---

## 12. 启动方式

统一命令格式：

```bash
python3 scripts/logistics.py <group> <action> [参数...]
```

| 场景 | 命令 |
|---|---|
| 查看模块 | `python3 scripts/logistics.py list` |
| 静态自检 | `python3 scripts/logistics.py doctor` |
| 报销四态查询 | `python3 scripts/logistics.py reimb balance` |
| 工资计算 | `python3 scripts/logistics.py wage compute --month YYYY-MM` |
| 工资核对 | `python3 scripts/logistics.py wage verify --month YYYY-MM` |
| 工资预览 | `python3 scripts/logistics.py wage preview --month YYYY-MM` |
| 生成月度汇总 | `python3 scripts/logistics.py summary generate` |
| 报单解析 | `python3 scripts/logistics.py intake parse --messages m.json` |
| 报单写入 | `python3 scripts/logistics.py intake run --messages m.json --month 8 --year 2026 --no-dry-run` |

触发方式：

- 手动/对话：用户说「归集报销 / 算工资 / 生成汇总 / 做报单」，任意 Agent 路由到对应命令。
- 群 @：走内置 `reimbursement/scripts/mention.py` 的 gate → classify → ack。
- 定时：调度器由部署环境决定（cron、WorkBuddy 自动化、Airflow 等）；本工具包只提供命令，不擅自注册任务。
- MCP：客户端连接 `logistics-agent serve` 后直接调用 `tools/list` 与 `tools/call`。

---

## 13. 自测验证方法

完整逐项报告见 [docs/自测报告](docs/自测报告.md)。当前结果：**38/38 业务功能点 + 4/4 接入能力通过，56/56 检查项通过，78 项单测通过**。

快速验证：

```bash
cd ~/.workbuddy/skills/logistics-management

# 顶层路由、报单与通用适配器单测
python3 tests/test_skill.py
python3 tests/test_agent_adapter.py

# 四个内置模块单测
for f in $(find modules -path '*/tests/test_*.py' -type f | sort); do
  python3 "$f" || exit 1
done

# 通用 Agent 工具发现与调用
python3 scripts/agent.py list
python3 scripts/agent.py call logistics_doctor

# 元功能与只读数据链路
python3 scripts/logistics.py list
python3 scripts/logistics.py config
python3 scripts/logistics.py doctor
python3 scripts/logistics.py reimb balance
python3 scripts/logistics.py wage validate
python3 scripts/logistics.py summary inspect

# 脱敏自测
python3 scripts/logistics.py sheet selftest
python3 scripts/logistics.py intake selftest

# 任意写/发类功能点的安全入口自检
python3 scripts/logistics.py reimb notify --help
python3 scripts/logistics.py wage dispatch --help
python3 scripts/logistics.py summary generate --help
python3 scripts/logistics.py sheet run --help
python3 scripts/logistics.py intake run --help
```

安全约定：写表/外发类功能默认不真实执行；`--help` 用于验证入口与参数，
真实发送或写表必须由用户显式触发并满足 dry-run/确认闸门。

---

## 14. 常见问题排查

**Q：必须用 WorkBuddy 吗？**  
A：不需要。复制目录后直接用 `python3 scripts/logistics.py` 或 `python3 scripts/agent.py` 即可。WorkBuddy 只是可选入口。

**Q：怎样接入 Claude / Cursor / Dify？**  
A：见 [通用 Agent 接入](docs/通用Agent接入.md)：MCP 用 `scripts/agent.py serve`，function calling 用 `config/agent_tools.json` 的 schema，后端统一调用 `scripts/agent.py call`。

**Q：还要保留旧 skill 吗？**  
A：本工具包不再依赖它们。旧目录可停用；删除前确认自动化/快捷命令没有指向旧路径。

**Q：`doctor` 报脚本不存在？**  
A：检查 `config/modules.json` 的 root 是否都在 `modules/` 下，且内置模块目录完整。

**Q：bot_setup_required？**  
A：按 `modules/reimbursement/飞书机器人配置教程.md` 配置机器人；不得用个人身份冒充发送。

**Q：报单 run 提示「需先转录」？**  
A：图片运输单不猜数。用视觉能力转录成 xlsx/csv 后以 `--attachment` 传入再跑。

**Q：报单没写入任何司机表？**  
A：默认 dry-run，需 `--no-dry-run`；或目标司机报表不存在，按 `on_missing_target` 跳过并报告。

**Q：工资预览后没发给司机？**  
A：设计如此。须 `verify` PASS、发起人 `confirm`、再 `dispatch --no-dry-run`；定时只到 preview。

---

## 15. 目录结构

```
logistics-management/
├── AGENTS.md / CLAUDE.md / .cursorrules   通用 Agent 规则
├── SKILL.md                  WorkBuddy 可选入口
├── README.md
├── requirements.txt
├── install.sh                安装 logistics / logistics-agent 命令
├── bin/logistics, bin/logistics-agent     跨路径包装脚本
├── config/
│   ├── modules.json             内置模块注册与 action 命令
│   ├── agent_tools.json         通用工具清单（38 个）
│   ├── config.example.json      报单/触发脱敏配置模板
│   └── local/config.json        真实顶层配置（gitignore）
├── scripts/
│   ├── logistics.py             统一 CLI
│   ├── agent.py                 通用 Agent 入口（list/call/serve）
│   ├── agent_runtime.py         工具发现、门控、调用
│   ├── mcp_server.py            零依赖 MCP stdio 服务器
│   ├── tool_manifest.py         生成 agent_tools.json
│   ├── common.py                配置/路径/内置模块调用
│   └── intake.py                报单解析
├── modules/
│   ├── reimbursement/           内置报销模块（脚本/配置/state/凭证）
│   ├── driver-wage/             内置工资模块
│   ├── driver-month-report/     内置月度汇总模块
│   └── driver-monthly-sheet/    内置月度制表模块
├── docs/
│   ├── 通用Agent接入.md
│   ├── 功能点清单.md
│   ├── 自测报告.md
│   ├── 模块划分与数据流.md
│   ├── 触发与入口.md
│   ├── 报单功能.md
│   └── 待确认清单.md
├── tests/
│   ├── test_skill.py
│   └── test_agent_adapter.py
└── state/                       报单运行时数据（gitignore）
```
