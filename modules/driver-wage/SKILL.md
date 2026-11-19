---
name: driver-wage
description: 司机工资「计算 + 核对」两阶段技能。按每位司机各自的历史计薪规则，从月度运输单明细、报销数据、司机名册算出工资，再用独立路径复算并做数据完整性、勾稽、规则合规、报销口径、环比异常、外部账单六类核对，产出统一格式的工资表与逐司机工资条；经发起人确认后才允许一人一条私发。适用于司机工资计算、工资核对、月度工资表、按司机计薪规则不同、运输单筛选、报销并入工资、每月 10 号定时发起工资核算等请求。定义配置与代码分离、规则需人工确认、核对阻断、确认硬闸门、逐人私发白名单与幂等、以及"只做计算与核对两件事"的边界。2026-11-14 定案：名册以飞书司机数为主（飞书优先＋本地补充），有几个司机计算几个、不铺零值行，通讯通道为飞书。
agent_created: true
---

# 司机工资：只做两件事

## 一、边界（最高优先级，先读）

**本技能只有两个业务能力：计算工资、核对工资。**

- `compute` 计算工资；`verify` 核对工资。**不得增加第三个业务能力**。
- 不得做：报销归集（属另一个技能）、运费结算、车贷/油耗/管理费/净利润核算、考勤、社保、个税、报表写入、绩效分析、司机管理。
- 下列内容**不是**第三个能力，而是这两件事的输入准备与交付编排，允许执行但不得扩张业务范围：
  - 输入准备：飞书名册同步（sync-roster）、运输单筛选与字段映射、图片转录、规则反推与确认、样式抽取；
  - 交付编排：统一表格渲染、待确认预览、发起人确认闸门、逐司机私发、每月 10 号定时发起。
- **绝不修改**用户已有的历史工资表、报销台账或任何原始输入文件。所有产物只写到 `out_dir`。
- 历史工资表中的报销/车贷/油耗/管理费/净利润等汇总块：只把「报销」作为工资加项读取，其余一律不读、不算、不写。

## 二、触发时机

- 每月 10 号 09:00 定时自动化：**只发起计算与核对，止于待确认预览**。
- 用户要求"算某月司机工资""核对工资""生成工资表""按历史规则算工资""发工资条给司机"。
- 用户要求查看核对报告、阻断项、确认状态、投递回执。

## 三、固定动作顺序（不可跳步、不可换序）

```
输入准备 → compute → verify → preview → 发起人确认 → dispatch
                         ↑                    ↑
                    BLOCK 即停          硬闸门，定时不得代劳
```

1. **读配置**：`config/local/` 优先，其次 `examples/config/`。绝不凭记忆猜路径、列名、司机归属或计薪规则。读 `config.json` 类配置前先看 `references/ONBOARDING.md`。启用飞书主源时先执行 `payroll.py sync-roster`（只读飞书、只写本地缓存、不发消息）。
2. **图片类运输单**：先用视觉能力逐张转录成 CSV/JSON（保留行列位置），再由 `ingest` 消费。不猜看不清的数字；低置信度行进待人工确认。
3. **compute**：`python3 scripts/payroll.py compute --month YYYY-MM`。只读输入，按已确认规则逐行求值，输出逐行明细 + 每人汇总 + 输入指纹。
4. **verify**：`python3 scripts/payroll.py verify --month YYYY-MM`。独立路径重算并执行 8 类检查，输出 `verification.json` 与人类可读 `verification.md`。
5. **preview**（定时任务的终点）：`python3 scripts/payroll.py preview --month YYYY-MM`。一次完成计算、核对、统一表格渲染、逐司机工资条生成、待确认预览与发起人通知。**此命令绝不向司机发送任何消息**。
6. **发起人确认**：由发起人本人核对 `verification.md` 与工资表后执行
   `python3 scripts/payroll.py confirm --month YYYY-MM --token <口令> --owner <发起人>`。
   核对结论为 `REVIEW` 时还须加 `--accept-review`。**Agent 不得代替发起人确认，不得自行编造 token**。
7. **dispatch**：`python3 scripts/payroll.py dispatch --month YYYY-MM --no-dry-run --channel feishu`。
   默认 dry-run；只有显式关闭才真发。按司机一人一条，只含本人数据。

任何一步失败都要停在该步并报告原因，不得跳过核对直接发、不得因发送失败而回滚已写入的产物。

## 四、铁律

- **飞书机器人配置自检（2026-11-16 用户定案）**：`payroll.py` 每次运行前跑 `scripts/bot_check.py` 检测机器人（lark-cli 可用 + bot 已授权），输出 JSON 带 `bot_setup` 字段。若 `bot_setup.bot_setup_required=true`，**不要静默、不要用个人身份冒充发送**，把 `bot_setup.reminder`（问题清单 + 教程）原样转述给用户，提示先按本目录 `飞书机器人配置教程.md` 配置机器人（统一机器人 = 廖浩成's Feishu CLI `cli_aa7fca0a72ba9bb4`）。

1. **规则必须人工确认**：从历史工资表反推的规则一律 `confirmed=false`，未确认即被 `verify` 判 BLOCK。绝不凭历史数值猜算法，绝不 `eval` 任意公式。
2. **核对不通过不发**：`BLOCK` 禁止确认与私发；`REVIEW` 需发起人显式接受复核项；`PASS` 才进入确认闸门。
3. **确认与指纹绑定**：确认后任何输入、规则或结果变化都会使确认失效，必须重新核算与确认。确认有有效期。
4. **名册以飞书为主、人数弹性**：`sync-roster` 从飞书通讯录/群拉取真实司机作为主源，本地名册只作补充；只计算当月实际有运输单（或报销）的司机，有几个算几个，绝不铺全员零值行、不凑人数、不因"谁没到齐 / 缺 open_id"记异常或催办。姓名不在名册/别名中时标记 `unresolved` 并由核对阻断；名册外司机按配置筛选并透明记录筛除行数。
5. **一人一条、绝不群发**：只发给**飞书在册**、`notify=true`、`open_id` 以 `ou_` 开头的司机；仅在本地名册的司机只作信息记录，绝不降级发到群里、绝不把个人工资条发到群。
6. **幂等**：同月同司机成功发送后不重发；幂等键含月份、司机与结果指纹。
7. **金额用 Decimal**：逐行按精度取整后累加，避免浮点误差造成核对假阳性。
8. **异常不静默**：规则缺失、求值失败、列歧义、日期无法解析、姓名冲突、写入失败、投递失败，全部进清单与核对报告。
9. **dry-run 默认开启**：所有外发默认不落地；真实私发必须同时满足"已核对 + 已确认 + 显式关闭 dry-run"。

## 四之二、名册口径（2026-11-14 定案）

| 问题 | 口径 |
|---|---|
| 名册主源 | 飞书通讯录部门 + 群成员（`payroll.roster.source = "feishu"`），排除群主与机器人 |
| 本地名册 | 只作补充（别名、历史司机、报表定位），不决定计算人数 |
| 计算范围 | 飞书在册 ∪ 本地当月有运输单/报销的司机；**有几个算几个**，不铺零值行 |
| 私发范围 | 只发飞书在册且 `open_id` 有效者；其余只作信息记录，不当异常、不催办 |
| 通讯通道 | `notify.channel = "feishu"`；仍默认 dry-run，仍须发起人确认 |
| 飞书不可用 | 保留旧缓存并降级，绝不伪造名册；缓存为空时没有可私发对象 |

`sync-roster` 只读飞书、只写 `state/feishu_roster.json`（gitignore）；compute / verify 只读缓存，不联网。

## 五、命令速查

| 目的 | 命令 |
|---|---|
| **引导式接入（新手首选）** | `payroll.py init --trip-file 运输单 --wage-dir 历史表 --template 模板 [--roster-file 名册] --out-dir config/local` |
| **人话配置校验** | `payroll.py validate` |
| 同步名册（飞书/源） | `payroll.py sync-roster` |
| 计算 | `payroll.py compute --month YYYY-MM` |
| 核对 | `payroll.py verify --month YYYY-MM` |
| 定时终点（待确认） | `payroll.py preview --month YYYY-MM --notify-owner --no-dry-run` |
| 发起人确认 | `payroll.py confirm --month YYYY-MM --token <口令> --owner <发起人>` |
| 逐司机私发 | `payroll.py dispatch --month YYYY-MM --no-dry-run --channel feishu\|csv-export\|stdout` |
| 查状态 | `payroll.py status --month YYYY-MM` |
| 反推历史规则 | `payroll.py learn-rules --wage-dir <目录> --out config/local/rules.candidates.json` |
| 抽取统一模板 | `payroll.py extract-style --template <模板.xlsx> --out config/local/style.json` |
| 脱敏自检 | `payroll.py --profile example demo --month 2026-09` |

所有命令输出 JSON 摘要到 stdout；`--profile local|example|<目录>` 可显式指定配置。
通用化设计（可插拔名册来源 / 通知通道 / 引导接入 / 友好校验）见 `references/UNIVERSAL.md`。

## 六、资源索引

| 文件 | 用途 |
|---|---|
| `references/WORKFLOW.md` | 两阶段详细流程、状态机、每月 10 号自动化边界 |
| `references/RULE_DSL.md` | 计薪规则 DSL、Excel 公式平移、从历史工资表迁移 |
| `references/VERIFICATION.md` | 8 类核对项、判定口径、阻断与复核区别 |
| `references/STYLE.md` | 统一表格格式：文字/字体/背景/颜色/模板 |
| `references/NOTIFICATION.md` | 通讯适配器、确认闸门、防发错人、幂等与回执 |
| `references/ONBOARDING.md` | 接入自己数据的步骤与常见坑（含真实报表常见异常） |
| `config/local/` | 真实配置（已 gitignore，含真实姓名/路径/open_id） |
| `examples/config/` + `examples/data/` | 全脱敏、可直接运行的样例 |
| `tests/` | 40 项单元测试：规则安全、核对拦截、闸门、幂等、飞书名册与弹性人数、发送降级 |

## 七、开源约定

仓库内不得出现真实司机姓名、真实金额、真实路径、真实 open_id/chat_id、密钥。这些只存于 `config/local/`（已 gitignore）。提交前执行 `make check-privacy`。
