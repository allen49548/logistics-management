# driver-monthly-sheet · 司机月度表

以 WPS 根目录的**单 sheet 模板**为格式样张，在每个司机的真实报表里精确插入一个格式/字段一模一样的**月份 sheet**，把表格文档里按司机姓名分组的数据和当月报销填进去。全配置驱动、不硬编码、幂等、保护 WPS 文件特有部件。

## 一句话原理

模板负责"长什么样"，数据源负责"填什么"，配置负责"谁对应谁、填到哪个文件"。机器人只做搬运，所有映射都能改配置文件。

---

## 一、安装

### 方式 A：当作 WorkBuddy Skill 使用（推荐）

仓库就放在技能目录（本目录即 `modules/driver-monthly-sheet`），无需额外安装。新机器部署：

```bash
git clone <你的仓库地址> modules/driver-monthly-sheet
cd modules/driver-monthly-sheet
python3 scripts/driver_sheet.py selftest      # 先自测，应看到 "ok": true, 15/15
```

依赖：Python 3.9+ 与 `openpyxl`（仅读模板/数据源/报销 sheet 时用）：

```bash
pip3 install -r requirements.txt
```

### 方式 B：任意目录直接跑

```bash
cd /path/to/driver-monthly-sheet
python3 scripts/driver_sheet.py selftest
```

---

## 二、三分钟上手

```bash
# 1. 生成配置骨架
python3 scripts/driver_sheet.py init
#    → 生成 config/local/config.json（真实数据，已 gitignore，不会提交）

# 2. 用编辑器打开 config/local/config.json，改这几处：
#    paths.workdir      WPS/冷链物流 根目录
#    paths.template     你的单 sheet 模板路径
#    paths.source       月度数据源表格文档路径（没有可先留空/指向不存在文件）
#    paths.target_dir   司机表目录（含 {year} 占位）
#    reimbursement.ledger_db  reimbursement 的 ledger.sqlite 路径
#    template.column_map      模板表头 → 语义键
#    source.column_aliases    语义键 → 数据源列名候选

# 3. 检查配置是否正确识别
python3 scripts/driver_sheet.py inspect --year 2026 --month 11

# 4. 预演（不写任何文件，先看映射对不对）
python3 scripts/driver_sheet.py plan --year 2026 --month 11

# 5. 确认无误后真正写入
python3 scripts/driver_sheet.py run --year 2026 --month 11 --no-dry-run
```

每次真正写入前可随时用 `plan` 预演；`run` 不带 `--no-dry-run` 时只预演不落盘。

---

## 三、你需要准备的三样东西

### 1. 模板（格式样张）

一个只含 **1 个 sheet** 的 xlsx，第一行是表头（字段名）。技能会完整复制它的：表头文字、单元格样式（字体/颜色/对齐/边框/数字格式）、列宽、合并单元格、冻结窗格。

### 2. 数据源表格文档（可暂缺）

一张表，含：
- 一列**司机姓名**（列名在 `source.driver_column` 里配候选，命中即识别）；
- 其余列是运输数据。列名不必和模板一致，靠 `source.column_aliases` 做映射。

没有数据源时先跑 → 生成只有表头的空 sheet；有了之后按幂等策略填入（见第六节）。

### 3. 报销明细（可选）

默认读 `reimbursement` 的 `state/ledger.sqlite`，按司机 + 月份过滤聚合出「笔数 / 金额」，通过 `template.column_map` 里的 `reimb.count` / `reimb.amount` 填进月份 sheet。也可改成读各司机表的报销明细 sheet，或关闭。

---

## 四、配置字段速查（config/local/config.json）

完整注释见 `config/config.example.json`。核心：

| 配置块 | 作用 |
|---|---|
| `paths` | 模板/数据源/目标目录/文件命名/备份目录；支持 `{year}` `{month}` `{month2}` `{driver}` 占位 |
| `template.sheet_index` / `header_row` / `style_from_row` | 模板取哪个 sheet、哪行表头、照抄哪行的数据样式 |
| `template.column_map` | **模板表头列名 → 语义键**（`source.xxx` / `reimb.count` / `reimb.amount` / `literal:xxx` / `blank`） |
| `source.driver_column` | 司机姓名列的候选表头 |
| `source.column_aliases` | 语义键 → 数据源可能的列名（别名归一，不硬编码） |
| `reimbursement.mode` | `ledger` / `detail_sheet` / `none` |
| `sheet.name_pattern` | 新 sheet 命名，默认 `{year}年{month}月` |
| `sheet.exist_policy` | `fill`（默认，已存在不动）/ `skip` / `overwrite` |
| `drivers.from` | `target_dir`（扫描 `{driver}{年}.xlsx`）/ `list` |
| `run.dry_run` | CLI 的 `--no-dry-run` 可覆盖；默认 true 保护真实表 |

---

## 五、安全性（为什么敢写真实 WPS 表）

真实司机表是 WPS 保存的，含 `customXml/`、`xl/metadata.xml` 等 openpyxl 会丢弃的部件。本技能**不用 openpyxl 整体重写**，而是 zip 层精确插入：

- 只改 4 个索引文件：`xl/workbook.xml`、`xl/_rels/workbook.xml.rels`、`[Content_Types].xml`、`docProps/app.xml`；
- 新增 1 个 worksheet 部件；
- 其余部件**字节原样复制**；
- 写前备份到 `backup_dir`，写后回读校验"原有部件零差异 + openpyxl 能打开 + 新 sheet 在册"，失败自动回滚。

已用真实文件（32 个部件）验证：零丢失，customXml/metadata 完整保留。

---

## 六、幂等两阶段

| 场景 | 行为 |
|---|---|
| 阶段一：无数据源 | 每个目标司机表生成一个只有表头+样式的空 sheet |
| 阶段二：带数据源首次写入 | 数据填入新 sheet |
| sheet 已存在 + `fill` | 跳过、不重建、不动原文件（默认，最安全） |
| sheet 已存在 + `overwrite` | 显式覆盖该 sheet（用于空建后回填 / 重做） |
| sheet 已存在 + `skip` | 跳过 |

> 若你已先建了空 sheet、现在想回填数据：把 `sheet.exist_policy` 临时改成 `overwrite` 跑一次，再改回 `fill`。

---

## 七、自测与测试

```bash
python3 scripts/driver_sheet.py selftest          # 15 项端到端，全在临时目录
python3 -m unittest discover -s tests -v          # 单元测试
make test                                          # 同上（若装了 make）
```

自测覆盖：模板解析、司机分组、报销按月过滤聚合、字段映射、表头一致、zip 插入保部件、幂等不重建、无数据源建空 sheet、dry-run 不写盘。

---

## 八、文件结构

```
driver-monthly-sheet/
├── SKILL.md                       # 技能说明（WorkBuddy 触发用）
├── README.md                      # 本文件
├── requirements.txt               # openpyxl
├── install.sh / Makefile          # 安装与常用命令
├── config/
│   ├── config.example.json        # 全脱敏配置样例（可公开）
│   └── local/                     # 真实配置（gitignore）
├── scripts/
│   ├── driver_sheet.py            # CLI 入口
│   ├── pipeline.py                # 编排
│   ├── xlsxzip.py                 # zip 层精确插入（核心安全件）
│   ├── styles_bridge.py           # 模板样式 → 目标工作簿映射
│   ├── sheet_writer.py            # worksheet XML 生成
│   ├── source_reader.py           # 数据源分组
│   ├── reimb_reader.py            # 报销聚合
│   └── selftest.py                # 脱敏自测
├── examples/                      # 脱敏样例配置
├── tests/                         # 单元测试
└── references/                    # 设计说明
```

## 九、触发词（在 WorkBuddy 里说这些就会用本技能）

“生成司机月度表”“按模板建 11 月 sheet”“把这张汇总表的数据分发到各司机表”“报销并入月度表”“按司机分发表格数据”。
