# 司机业务管理系统

统一说明冷链物流司机业务的三个**已实现**子系统：月度净利润报表、司机报销归集、司机工资计算与核对。

本仓库承载「月度报表」引擎代码与全套系统文档；报销、工资两个子系统以独立 skill 安装在 `~/.workbuddy/skills/`，代码与配置留在各自原处。本次整合**只统一文档入口，不改运行路径、不新增业务功能或依赖**。本文所有内容均来自各子系统现有 `SKILL.md` / `README.md` / `config` / `references`。

---

## 1. 系统定位

把司机相关的三件事放在一个视角下管理：**凭证入账（报销）→ 工资核算（计算与核对）→ 经营结果（月度净利润与环比）**。三者共用同一批司机年份报表（`<报表根>/<年份>年/司机工资/{司机}{年份}.xlsx`），但**不互相自动调用**，各自独立触发、独立产出。

| 目标 | 由谁完成 |
|---|---|
| 把司机发的付款凭证截图归集成报销明细、建确认任务、发通报 | 报销归集 skill |
| 按各司机规则算工资，独立复算核对，经发起人确认后逐人私发工资条 | 工资计算与核对 skill |
| 从司机报表动态取数，按统一口径算月度净利润，只与上月环比，出管理者报表 | 本仓库月度报表引擎 |

---

## 2. 整体功能模块

| 模块 | 代码位置 | 核心职责 | 主要产物 | 触发方式 |
|---|---|---|---|---|
| 月度净利润报表 | 本仓库 `scripts/` + `config/` | 识别运费/工资/支出 → 统一口径净利润 → 只与上月环比 | `<年>年<月>月汇总.xlsx`（月度汇总 / 项目明细 / 口径与校验） | 命令行 `cli.py generate` |
| 司机报销归集 | `modules/reimbursement/` | 飞书群付款凭证截图 → 司机+月份归类 → 去重 → 写明细 → 通报+确认任务 | 司机报表「11月报销明细」sheet、`state/ledger.sqlite` 台账、`state/runs/*.md` 运行清单、飞书通报 | 定时/手动/@机器人（详见模块文档） |
| 司机工资计算与核对 | `modules/driver-wage/` | `compute` 计算 + `verify` 独立复算核对 → 预览 → 发起人确认 → 逐人私发 | 工资表、逐司机工资条、`verification.json/.md`、`dispatch.json` | 每月 10 号定时/手动命令 |

三个模块的关系与边界见 `docs/司机报销系统.md`、`docs/司机工资计算与核对.md`。

---

## 3. 司机报销流程

依据 `reimbursement/SKILL.md`，一轮归集的固定顺序：

1. **读配置、定窗口**：读 `config.json`（唯一真源）；用系统 `date` 计算最近 `lookback_days`（默认 14）天窗口，不凭记忆推日期。
2. **发现群、拉消息**：动态发现群并与白名单合并；只从「物流部」主群拉消息（`lark-cli im +chat-messages-list --as user --page-all --download-resources`）；停用群不读不发；忽略 system 与已撤回消息。
3. **司机归属**：由**消息发送者**决定（open_id/姓名精确匹配，名册外真实成员按弹性口径建司机），只统计发了付款凭证截图的司机；文字/票面姓名与发送者冲突、群主本人发、姓名形态异常或解析不出 → 进「待人工确认·未识别司机」，不猜、不写、不登记。不催全员进群、不催 open_id。
4. **凭证判定**：只认机打付款凭证截图（含金额与支付时间）；手写票一律不记录（只发补录提醒），纯文本报销跳过；低置信度/无日期/涂改遮挡进待人工确认；ETC「充值成功」机打截图按正常报销结算，类别 `ETC充值`。
5. **三方池去重**：`ledger.py check` / `check-batch` 查 ①台账 entries ②群历史池 voucher_seen ③各司机报表「报销明细」既有行，共六层；批量时自动比对**批内**新增凭证。以订单号 + 支付时间为唯一性依据，相同金额不是重复，不同司机同金额同日期不判重复。**命中一律跳过**（含当时未入账的历史票），只在运行清单写明命中池、原出现时间与当前状态。每轮用 `index-add` 把拉到的每张票落池；换机/首次启用先 `index-backfill` + `index-stats`。
6. **定月份、写明细**：按凭证日期定年月，定位 `{报表根}/{年份}年/司机工资/{司机}{年份}.xlsx`；在**已存在**报表内写唯一一个「11月报销明细」sheet（旧名「报销明细」就地重命名），可见列仅「日期 / 订单号 / 类型 / 金额(元)」，第 5 列隐藏存 `message_id` 幂等键，按月份升序分节。zip 层精确插入：写前备份、写后回读校验、失败回滚。无报表文件 → `record_without_write`（只登记台账、不新建文件）。
7. **登记台账**：回读校验成功后 `ledger.py add`，driver 写解析出的司机名。
8. **确认任务闭环**：本轮只要有**新入账**，必须为每位司机建一个飞书确认任务并挂进「报销」任务清单；建任务/挂清单失败须进异常并补建。无新入账（含全重复）不建任务。负责人点完成 → `group_task.py check` 标记 `reimbursed=1`，这是「真实报销」唯一凭证。
9. **通报**：有新数据 → 物流部群发极简通报（逐笔金额+日期、该笔任务链接、一句安全驾驶提醒；不 @ 人、无统计合计/权限说明）+ 负责人私聊精简专业版；无新数据（含纯重复）→ 群里只发一句「暂无新报销」，不发负责人长文。重复提交的旧凭证不进任何通报，只在运行清单留痕。**无司机私聊通道**（发送端硬拦截）。
10. **落运行清单**：`state/runs/<YYYY-MM-DD_HHMM>.md` 追加逐条识别、去重判定、司机/月份、文件+sheet、备份路径、任务链接、跳过/待确认/补录提醒、异常与统计窗口。

月初月报（独立自动化，每月 1–4 号 09:00）按台账 `entries.driver` 分组、按四态拆分、只列当月实际有凭证的司机、与上月做环比，群内极简 + 负责人专业版两层发送，自带「每自然月只真发一次」防护。

---

## 4. 司机计算与核对逻辑

依据 `driver-wage/SKILL.md` 与 `config/README.md`。**只有两个业务能力：`compute` 与 `verify`**；输入准备与交付编排不算第三个能力。

### 4.1 固定动作顺序（不可跳步、不可换序）

```
输入准备 → compute → verify → preview → 发起人确认 → dispatch
                      ↑ BLOCK 即停      ↑ 硬闸门，定时不得代劳
```

- **compute**：只读输入，按已确认规则逐行求值，输出逐行明细 + 每人汇总 + 输入指纹。金额用 `Decimal` 按精度取整后累加。
- **verify**：独立路径重算，并执行核对（数据完整性、勾稽、规则合规、报销口径、环比异常、外部账单等，详见 `references/VERIFICATION.md`）。结论 `BLOCK` 禁止确认与私发；`REVIEW` 需发起人显式 `--accept-review`；`PASS` 才进入确认闸门。
- **preview**：一次完成计算、核对、统一表格渲染、工资条生成、待确认预览与发起人通知；**绝不向司机发消息**。定时任务的终点。
- **发起人确认**：`confirm --token --owner`，仅 `gate.owner` 指定的人可确认；核对结论与输入/规则/结果指纹绑定，变化即失效，有有效期（默认 72 小时）。Agent 不得代确认、不得编 token。
- **dispatch**：默认 dry-run；只有「已核对 + 已确认 + 显式 `--no-dry-run`」才真发。按司机一人一条，只含本人数据；只发飞书在册、`notify=true`、有效 `open_id` 的司机，绝不群发。同月同司机成功发送后不重发（幂等键含月份、司机、结果指纹）。

### 4.2 口径要点

- 计薪规则从历史表反推一律 `confirmed=false`，未确认即 `BLOCK`；不 `eval` 任意公式。`wage` 支持 `tier / lookup / const / formula / sum`。
- 名册以飞书为主（通讯录部门 + 群成员，排除群主/负责人），本地名册只补充别名与报表定位；只算当月实际有运输单或报销的司机，有几个算几个，不铺零值行。
- 历史工资表里的报销/车贷/油耗/管理费/净利润汇总块：只把「报销」作为工资加项读取，其余不读不算不写；`rules.json` 中 `source: "reimbursement"` 的调整项不参与计算，避免与报销数据源双记。
- 同一趟运输只能进入一个启用的来源，否则核对判重复并阻断；图片运输单先转录成 CSV/JSON 再配来源。
- 绝不修改历史工资表、报销台账或任何原始输入文件；所有产物只写 `out_dir`。

---

## 5. 数据流转关系

```
飞书「物流部」群付款凭证截图
        │ 报销归集 skill（司机归属 → 凭证判定 → 三方池去重 → 月份归属）
        ▼
司机报表「11月报销明细」sheet  ←── 台账 sqlite（幂等键 message_id、四态、任务状态）
        │                                      │
        │ 月度报表读取同一工作簿的「月份 sheet」 │ 报销数据源（sqlite 只读）
        ▼                                      ▼
月度报表引擎 → 2026年8月汇总.xlsx        工资 compute/verify ← 运输单明细 + 飞书/本地名册 + 计薪规则
                                               │ preview → 确认 → dispatch
                                               ▼ 工资表 / 工资条
```

- **共享点**：三个模块共用 `<报表根>/<年份>年/司机工资/{司机}{年份}.xlsx` 这一组司机年份报表；报销写明细 sheet，月度报表读月份 sheet，工资的报销加项来自报销数据源而非历史汇总块。
- **不共享**：没有跨模块自动调用链。报销完成不会自动触发工资计算；月度报表不读取报销确认任务状态；工资不写回司机报表。
- **财务口径衔接**：月度净利润 = 运费收入 − 工资 − 其他支出（报销、油费、车贷、管理费等按配置归到支出角色），与工资模块的「工资」口径各自按本模块配置判定。

---

## 6. 关键业务规则

| 规则 | 口径 |
|---|---|
| 报销唯一性 | 订单号 + 支付时间；三方池六层去重，命中一律跳过；相同金额不算重复 |
| 报销状态 | 只有「已登记·待打款」计入未结；已报销/需补材料/重复/作废不计入 |
| 报销闭环 | 有新入账必有确认任务且挂清单；无任务 = 未闭环 |
| 报销可见性 | 金额不保密、群内按司机列；订单号尾号/凭证文件名/任务链接/台账路径只进负责人私聊 |
| @ 触发 | 仅物流部主群；先调侃回应（固定 bot 身份），再按 open_id 分 admin/limited 权限 |
| 工资闸门 | 规则须人工确认；核对不通过不发；确认与指纹绑定；默认 dry-run；一人一条 |
| 月度取数 | 项目名/别名/金额/月份/公式全部配置化；同义字段只取一个值、禁止累加；重叠科目用源表净利润反推 |
| 月度环比 | 只与上一个月比，配置不自洽报错；上月缺失/为负/样本不全标「不可比」，不硬算 |
| 月度清理 | 生成前先回收同月旧表到 `.recycle/<时间戳>/`，避免重复数据 |
| 跨模块共性 | 不臆造：缺失、冲突、可疑一律标注原因并写清单；涨红跌绿；飞书通知前做机器人配置自检 |

---

## 7. 安装部署

### 7.1 环境依赖

| 依赖 | 用途 | 来源 |
|---|---|---|
| Python 3.9+ | 三个模块的脚本运行 | 本机 |
| `openpyxl` | 月度报表读写 xlsx；报销 zip 层精确写 sheet；工资渲染与读取 xlsx | `pip`，各模块 requirements 版本：月度 `>=3.1`、报销 `>=3.0`、工资 `>=3.1,<4` |
| `Pillow>=9.0` | 报销感知哈希去重（dHash 读像素算汉明距离） | `pip`（仅报销） |
| `lark-cli` | 报销拉群消息/发通报/建任务；工资飞书名册与私发 | WorkBuddy 飞书连接器提供，路径 `~/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli`，需 `auth login` |
| WPS / Excel | 打开保存源表以写入公式缓存值；月度报表读不到未缓存公式值 | 本机 |
| WorkBuddy 自动化 | 月初月报、每月 10 号工资等定时任务 | WorkBuddy（注意：报销「每天 09:00」在 `config.schedule` 中有描述，但 `SKILL.md` 记载当前自动化系统并无此任务，需以实际自动化配置为准） |

### 7.2 安装步骤

三个 skill 已安装在 `~/.workbuddy/skills/`；本仓库是当前工作副本，无远程地址。从零部署时：

```bash
# 1) 放置 skill 目录（三个模块各自独立，勿改目录名）
#    modules/driver-month-report   （本仓库）
#    modules/reimbursement
#    modules/driver-wage

# 2) 安装 Python 依赖（按需）
pip install -r modules/driver-month-report/requirements.txt
pip install -r modules/reimbursement/requirements.txt
pip install -r modules/driver-wage/requirements.txt

# 3) 工资模块安装器（可选，复制到用户级 skill 目录）
cd modules/driver-wage && make install

# 4) 飞书 CLI 可用 + 机器人授权 + 入群
export PATH="$HOME/.workbuddy/binaries/node/cli-connector-packages/bin:$PATH"
lark-cli --help && lark-cli auth login && lark-cli auth status
# 群设置中添加机器人「廖浩成's Feishu CLI」(cli_aa7fca0a72ba9bb4)
```

### 7.3 配置说明

| 模块 | 配置文件 | 要点 |
|---|---|---|
| 月度报表 | `config/report.json`、`config/item_rules.json` | 前者：月份/源目录/输出与旧表清理/解析规则/净利润与环比公式/列与 KPI/文案配色；后者：项目 key、类别角色、别名优先级、明细表头、重叠关系。本地覆盖用 `config/report.local.json`（gitignore） |
| 报销 | `reimbursement/config.json`（真实）、`config.example.json`（脱敏模板） | 群白名单与 chat_id、`lookback_days`、`ocr` 凭证口径、`dedup` 三方池、`status_model` 四态、`reporting` 通报口径、`group_task` 任务与清单、`mention_policy` @ 权限、`adapters` 声明式 IO（当前仅声明、脚本不读取）、`runtime` 参数收敛 |
| 工资 | `driver-wage/config/local/`（真实，gitignore）优先于 `examples/config/`（脱敏） | `payroll.json`（路径/精度/核对阈值/名册主源/确认闸门/通知通道）、`rules.json`（规则+价目表+确认状态）、`field_map.json`（列映射/别名/筛选）、`roster.json`（名册/open_id/notify）、`style.json`（表格样式） |

可用 `--profile <目录绝对路径>` 或环境变量 `PAYROLL_PROFILE` 显式指定工资配置。

### 7.4 初始化方式

```bash
# 月度报表：无独立 init， Defaults 在 config/report.json；先 inspect 验证取数
cd modules/driver-month-report && python3 scripts/cli.py inspect

# 报销：首次启用或换机，回填并核对群历史池
cd modules/reimbursement
python3 scripts/ledger.py index-backfill
python3 scripts/ledger.py index-stats
# 可选：同步名册（仅提升归属精度，不是入账前置条件）
python3 scripts/driver_roster.py sync --chat-id <物流部群 chat_id>
# 机器人自检
python3 scripts/bot_check.py

# 工资：引导式接入
cd modules/driver-wage
python3 scripts/payroll.py init --trip-file <运输单> --wage-dir <历史表目录> \
  --template <模板.xlsx> [--roster-file <名册>] --out-dir config/local
python3 scripts/payroll.py validate
# 可选：反推规则 / 抽取样式 / 同步飞书名册
python3 scripts/payroll.py learn-rules --wage-dir <目录> --out config/local/rules.candidates.json
python3 scripts/payroll.py extract-style --template <模板.xlsx> --out config/local/style.json
python3 scripts/payroll.py sync-roster
```

规则反推结果需人工确认后写入 `rules.json`（`confirmed=true`），否则核对阻断。

### 7.5 启动命令

```bash
# 月度净利润报表
cd modules/driver-month-report
python3 scripts/cli.py generate                                  # 默认 2026-08，环比 2026-07
python3 scripts/cli.py generate --month 2026-09 --previous 2026-08
python3 scripts/cli.py inspect                                   # 只看取数
python3 scripts/cli.py generate --dry-run                        # 演算不落盘
python3 tests/test_engine.py                                     # 12 个用例

# 报销归集（脚本级，通常由 Agent 按 SKILL.md 编排）
cd modules/reimbursement
python3 scripts/ledger.py check-batch --json '[...]'             # 批量去重
python3 scripts/reimb_detail.py apply --data <data.json>         # 写报销明细
python3 scripts/group_task.py create ...                         # 建确认任务
python3 scripts/monthly_summary.py --send --run-id <运行ID>      # 月初月报
python3 scripts/notify.py --chat-id <群ID> --text-file <群版.txt> \
  --owner-text-file <专业版.txt> --run-id <运行ID>               # 通报（无新数据用 --no-new）
python3 scripts/mention.py gate --chat-id <群ID> --text "<正文>" # @ 触发门槛

# 工资计算与核对（固定顺序）
cd modules/driver-wage
python3 scripts/payroll.py compute --month YYYY-MM
python3 scripts/payroll.py verify --month YYYY-MM
python3 scripts/payroll.py preview --month YYYY-MM --notify-owner --no-dry-run
python3 scripts/payroll.py confirm --month YYYY-MM --token <口令> --owner <发起人>
python3 scripts/payroll.py dispatch --month YYYY-MM --no-dry-run --channel feishu
python3 scripts/payroll.py status --month YYYY-MM
python3 -m unittest discover -s tests -v                         # 40 项单元测试
```

### 7.6 常见问题

| 现象 | 原因 / 处理（依据现有文档） |
|---|---|
| 月度报表某数值为空 | 源表公式未被 WPS/Excel 打开保存过，没有缓存值；用 WPS/Excel 打开保存源表后重跑 |
| 月度报表同月数据重复 | 旧表清理失败；检查 `output.overwrite` 配置，`.recycle/<时间戳>/` 内有每次重生成前的备份 |
| 某司机标「不可比」 | 上月缺数据/为负/样本明显不全（运费低于配置的 `min_ratio`）；属预期标注，不硬算 |
| 报销提示「机器人未配置/不在群」 | 跑 `bot_check.py` 看 `issues`/`reminder`；按 `飞书机器人配置教程.md`：确认 lark-cli、`auth login`、机器人入群、核对 chat_id |
| 报销群里只有「暂无新报销」 | 本轮无新入账或全是重复提交；重复只在运行清单留痕，不进通报 |
| 手写票/纯文本报销没入账 | 规则如此：只认付款凭证截图；群内按司机分节提醒改发截图补录 |
| 同一张票重发又没加钱 | 三方池六层去重命中（含群历史/报表明细、当时未入账的票）；属预期，运行清单可查原出现时间 |
| 司机 @ 机器人没反应 | @ 仅物流部主群触发；飞书群 @ 非实时 webhook，需等下次拉取/运行 |
| 工资核对 `BLOCK` | 规则未确认或核对不通过；确认规则、修输入后重算，不得跳过 |
| 工资 `REVIEW` 怎么发 | 发起人显式加 `--accept-review` 确认后才可 `dispatch` |
| 工资条没发给某司机 | 仅发飞书在册、`notify=true`、有效 `open_id` 的司机；其余只作信息记录，不降级群发 |
| 发不出飞书消息 | 统一机器人 `cli_aa7fca0a72ba9bb4` 需授权且在目标群；按飞书教程排查，不用个人身份冒充 |

---

## 8. 仓库结构

```
driver-month-report/
├── README.md                 # 本文：统一系统说明
├── SKILL.md                  # 月度报表 skill 触发说明
├── docs/
│   ├── 司机报销系统.md        # 报销模块索引与流程要点
│   └── 司机工资计算与核对.md  # 工资模块索引与核对要点
├── config/
│   ├── report.json           # 月份/路径/解析/公式/版式/文案
│   └── item_rules.json       # 项目 key/别名/重叠关系
├── scripts/                  # cli / config / discovery / extractor / calculator / renderer / expr / common
├── tests/test_engine.py      # 12 个单元测试
└── requirements.txt          # openpyxl>=3.1
```

## 9. 边界

- 本仓库**不含**报销与工资模块的可执行代码；它们是独立 skill，按本文第 7 节命令在各自目录运行。
- 没有新增跨模块自动编排：报销完成不自动触发工资计算，工资不写回司机报表，月度报表不读确认任务状态。
- 本文只归纳已有实现与文档；未提及的功能一律视为不存在。
