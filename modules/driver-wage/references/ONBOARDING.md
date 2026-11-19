# 接入自己的数据

目标：把 `config/local/` 填好，让 `compute` + `verify` 能跑出 `PASS`。**这份目录含真实数据，已被 `.gitignore` 排除，不进开源仓库。**

## 七步接入

### 1. 起一份本地配置
```bash
cp examples/config/*.json config/local/
```
`payroll.json` 里先改 `paths` 与 `gate.owner`（唯一可确认的发起人）。
`set notify.channel = "feishu"` 是最终交付通道，但**调用仍默认 dry-run**，真实私发必须显式 `--no-dry-run`。
名册主源用 `roster.source = "feishu"` + `roster.feishu.department_query` / `chat_id` / `exclude_names`；验收阶段可先保持 `"local"`（脱敏样例默认）。

### 2. 定死统一表格格式
```bash
python3 scripts/payroll.py extract-style \
  --template <你指定的模板.xlsx> --out config/local/style.json
```
或直接用 `examples/config/style.json` 再微调。详见 `STYLE.md`。

### 3. 从历史工资表反推计薪规则
```bash
python3 scripts/payroll.py learn-rules \
  --wage-dir <历史工资表目录> --out config/local/rules.candidates.json
```
逐条与历史工资核对 → 写入 `config/local/rules.json` → 设 `confirmed=true` + `confirmed_by` / `confirmed_at`。
`needs_manual` 的司机必须手工补 `wage`。**未确认规则会被核对阻断，这是刻意设计。**

### 4. 同步飞书名册、填别名
```bash
python3 scripts/payroll.py sync-roster     # 只读飞书，写 state/feishu_roster.json
```
**名册以飞书司机数为主**：飞书通讯录部门/群成员是主源，`roster.json` 只作补充
（历史司机、别名、报表定位）。飞书返回的姓名经 `field_map.driver_aliases` 映射成本地 `id`，
`open_id` 由飞书覆盖，绝不手填猜测。

`roster.json` 的 `id` 必须与 `rules.json` 的 driver key 一致。把报表里出现过的
**所有姓名写法**都放进 `driver_aliases`：文件名、表内姓名、账单姓名可能是三种写法。

**有几个司机计算几个**：只算当月实际有运输单/报销的司机，不铺零值行。
飞书里没有的司机仍会参与计算，但**不会收到私聊**（`dispatch.json.skipped` 仅作信息记录，
不是异常、不是待办、不需要催 open_id）。飞书未同步时缓存为空，可私发对象为 0，绝不伪造名册。

### 5. 配运输单来源与筛选
- `columns`：表头名 / Excel 列字母 / 数字索引三选一，按该来源实际表头填；
- `header_row`：标题行占一行就填 2；
- `sheet`：**明确指定**，不要用 `*` 遍历全部 sheet；
- `max_row` / `max_col`：必须设，防止被格式撑到百万行拖死；
- `filters.exclude_unmapped_drivers: true`：只保留名册内司机，筛除行数会写进报告；
- **同一趟运输只能进入一个启用的来源**。

### 6. 接报销数据
- 结构化台账：`type: "sqlite"` + 只读 `query`，返回 `driver, 月份, amount` 三列；
- 或 `type: "csv"` / `"xlsx"` 按司机与月份汇总；
- 若历史工资表底部还有手填的「报销支出」，把 `wage_sheet_reimbursement.enabled` 打开做三方勾稽，并**人工裁决唯一口径**，不要两边都加。

### 7. 验收
```bash
make test
python3 scripts/payroll.py --profile example demo --month 2026-09      # 脱敏自检
python3 scripts/payroll.py --profile local preview --month <上月>       # 真实数据，应得 BLOCK/PASS 并说明原因
```
第一次真实跑通常会是 `BLOCK`：未确认规则、缺规则的司机、姓名别名、报销口径差异都会暴露出来。按 `verification.md` 逐项修，直到 `PASS`。

---

## 真实报表常见坑（务必逐条检查）

| 坑 | 表现 | 处理 |
|---|---|---|
| **每位司机表头不同** | 有人 9 列、有人 6 列、有人双区块；公里数在 G/H/E 列各不相同 | 每个来源单独配 `columns`；`learn-rules` 会产出 `field_map.candidates.json` 列布局 |
| **姓名写法不一致** | 文件名与表内姓名、账单姓名不同（形近字、异体字） | 全部写进 `driver_aliases`，绝不模糊匹配 |
| **同一文件多个 sheet 结构不同** | 一个是全量登记表（表头在第 2 行），一个是某司机明细（表头在第 1 行、带运费列） | 明确 `sheet` + `header_row`；不要两个都读，否则重复计薪 |
| **格式撑爆行/列** | `max_row` 显示 1048576、`max_column` 16384 | 设 `max_row` / `max_col` 上限 |
| **数组公式读不出值** | 公里数单元格是数组公式对象 | 标 `needs_manual`，人工补录或从路线文本解析，绝不猜 |
| **工资列歧义** | 同一 sheet 有多处「工资 / 报销」标签 | `learn-rules` 会跳过并警告；人工指定，不自动选第一个 |
| **报销口径两套** | 台账逐笔金额与工资表手填汇总数差异巨大 | 三方勾稽 + 人工裁决；别默认任一为准 |
| **账单自带核对差异** | 「核对费用 / 核对差异」列不为 0 | 打开 `external_bill` 交叉验证运输单可信度 |
| **汇总块不固定** | 报销/车贷/管理费标签散落不同列与行 | 只按标签+向右找第一个数值读取，找不到就报缺，不猜 |
| **外部司机混在全量表** | 一份登记表里有几十位司机，只有部分属于本公司 | `exclude_unmapped_drivers` + 透明记录筛除行数 |

---

## 图片类运输单的转录契约

图片不直接进入计算。流程：

1. 用视觉能力逐张转录，输出 CSV 或 JSON 行数组，**保留行列位置**（来源文件名、行号）；
2. 转录要求：逐字转录可见数字，不推测、不补全模糊数字；看不清写「无法辨认」；
3. 把转录产物配成 `field_map.trips_sources` 的一个来源（`type: csv|json`）；
4. 低置信度 / 无日期 / 涂改遮挡的行不自动计入，进待人工确认清单；
5. 明细里保留 `source` 与 `source_row`，核对与争议时可回溯到图片与行号。

这样核心代码不依赖任何私有或付费 OCR，替换 OCR 供应商不影响计薪逻辑。

---

## 上线前检查清单

- [ ] `make test` 全绿；`--profile example demo` 得到 `PASS`
- [ ] 所有司机规则 `confirmed=true`，`needs_manual` 已清零
- [ ] `driver_aliases` 覆盖所有姓名写法
- [ ] 每个启用的来源都指定了 `sheet`、`max_row`、`max_col`
- [ ] 同一趟运输只进一个来源（核对 `completeness` 无重复单）
- [ ] 报销口径已人工裁决为唯一来源
- [ ] `gate.owner` 是真实发起人；`confirm_ttl_hours` 合适
- [ ] `notify.channel = "feishu"`；`roster.source = "feishu"` 且已执行过 `sync-roster`
- [ ] 已核对飞书在册人数、当月计算人数、可私发人数三者差异（`preview.roster`）
- [ ] 发起人已看过 `verification.md` 与统一工资表
- [ ] 定时任务只到 `preview`，确认不会自动 `dispatch`
- [ ] `make check-privacy` 通过，仓库内无真实姓名/金额/路径/open_id

## 开源 / 脱敏

仓库内只允许出现虚构司机、虚构路线、示例金额与占位路径。真实信息只放 `config/local/`（gitignore）。
提交前跑 `make check-privacy`；打包用 `make package`（自动排除 local / out / state）。
`make install` 会把含真实配置的包复制到本机技能目录，该副本**不在 git 仓库里**。
