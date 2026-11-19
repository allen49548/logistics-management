---
name: driver-monthly-sheet
description: 司机月度表生成技能。以「WPS 根目录的单 sheet 模板」为格式样张，在各司机真实报表（{司机}{年}.xlsx）里精确插入一个格式/字段完全一致的月份 sheet（如 2026年11月），把「表格文档」里按司机姓名分组的运输数据填入对应司机，并把该月报销明细一并并入。支持无数据源先建空 sheet、后续填入不重建的幂等两阶段；全配置驱动不硬编码；zip 层精确插入保护 WPS 文件特有部件（customXml/metadata/vmlDrawing 等）。适用于生成司机月度表、按模板建月份 sheet、把汇总表数据拆分到各司机表、报销并入月度表、按司机分发表格数据等请求。
agent_created: true
---

# 司机月度表（driver-monthly-sheet）

## 一、这个技能做什么

一句话：**拿一个模板样张，在每个司机的真实表里生成一个新月份 sheet，把汇总表格里属于他的数据和当月报销填进去。**

输入三样：
1. **模板**：只含 1 个 sheet 的格式样张（用户放在 WPS 根目录，表头字段即目标字段）。
2. **数据源表格文档**：单张表、含司机姓名列、每行一条记录（可暂缺 → 先建空 sheet）。
3. **报销明细**：默认从 `reimbursement` 的台账按司机+月份聚合（可切 `detail_sheet` / `none`）。

产物：每个司机 `{司机}{年}.xlsx` 里新增一个 `{year}年{month}月` sheet，格式与模板一致。

## 二、边界（先读）

- **只增/填 sheet，不改原数据**：默认 `exist_policy=fill`，sheet 已存在就跳过、绝不重建、绝不覆盖用户手填内容。要强制重来才用 `overwrite`。
- **绝不整体重写工作簿**：真实表是 WPS 文件，含 `customXml`/`metadata.xml`/`vmlDrawing` 等部件。本技能用 zip 层精确插入，只改 4 个索引部件 + 新增 1 个 worksheet 部件，其余字节原样保留。已用真实文件验证（32 部件零丢失）。
- **配置与代码分离**：路径、字段映射、司机列别名、sheet 命名、报销来源全部在 `config/local/config.json`。绝不猜列名、不猜路径。
- **不碰报销归集本身**：报销数据只读，不写台账、不发飞书通知（那是 `reimbursement` 的职责）。
- **默认 dry-run**：`run` 默认不写文件，加 `--no-dry-run` 才真正写入。

## 三、固定流程

```
init（首次）→ 编辑 config/local/config.json → inspect 核对结构 → plan 预演 → run --no-dry-run 写入
```

1. `init`：生成配置骨架到 `config/local/config.json`。
2. **填配置**：把 `paths` 里的模板/数据源/目录/台账路径、`template.column_map`、`source.column_aliases` 按真实表头改好。
3. `inspect --year 2026 --month 11`：核对模板表头、数据源识别到的司机、目标表清单。
4. `plan --year 2026 --month 11`：只解析不写入，确认将生成哪些 sheet、映射是否正确。
5. `run --year 2026 --month 11 --no-dry-run`：真正写入（自动备份 + 回读校验 + 失败回滚）。
6. `selftest`：随时跑内置脱敏样例自测（15 项，全在临时目录）。

## 四、命令速查

| 目的 | 命令 |
|---|---|
| 生成配置骨架 | `python3 scripts/driver_sheet.py init` |
| 检查结构 | `python3 scripts/driver_sheet.py inspect --year 2026 --month 11` |
| 预演（不写） | `python3 scripts/driver_sheet.py plan --year 2026 --month 11` |
| 真正写入 | `python3 scripts/driver_sheet.py run --year 2026 --month 11 --no-dry-run` |
| 临时指定数据源 | `... --source /path/to/月度数据.xlsx` |
| 脱敏自测 | `python3 scripts/driver_sheet.py selftest` |
| 单元测试 | `python3 -m unittest discover -s tests -v` |

所有命令输出 JSON 到 stdout。退出码：0 成功 / 2 业务错误 / 3 配置错误。

## 五、配置要点（config/local/config.json）

- `paths.template`：模板 xlsx 路径（`{year}/{month}` 占位可用）。
- `paths.source`：数据源表格文档路径；没有就先建空 sheet。
- `paths.target_file`：`{target_dir}/{driver}{year}.xlsx` —— 已存在的司机表文件名规则。
- `template.column_map`：**模板表头列名 → 语义键**。语义键四类：
  - `source.<列名>`：从数据源取（`source.column_aliases` 做列名归一）；
  - `reimb.count` / `reimb.amount`：从报销聚合取；
  - `literal:文本`：固定值；`blank`：留空。
- `source.driver_column`：司机姓名列的候选表头（命中任一即可）。
- `source.column_aliases`：语义键 → 数据源可能的列名列表（不硬编码）。
- `reimbursement.mode`：`ledger`（默认，读 reimbursement 台账）/ `detail_sheet`（读各表报销明细 sheet）/ `none`。
- `sheet.name_pattern`：`{year}年{month}月`；`exist_policy`：`fill`（默认，幂等）/ `skip` / `overwrite`。
- `drivers.from`：`target_dir`（扫描文件名得到司机清单）/ `list`（显式名单）。

## 六、幂等两阶段

- **阶段一（无数据源）**：`paths.source` 指向不存在的文件 → 每个目标司机表生成一个只有表头+样式骨架的空 sheet。
- **阶段二（有数据源）**：数据源文件出现后，指定 `--source` 或配好 `paths.source` 再跑 → 因 `exist_policy=fill`，已存在的 sheet 不重建。要填入数据请在阶段一之后、首次带数据源运行时就带数据；若 sheet 已空建好需要回填，用 `overwrite` 策略显式覆盖。

## 七、铁律

1. 写入前必须 `plan` 核对；默认 dry-run，绝不未经确认写真实表。
2. 每次真实写入都备份到 `backup_dir`，回读校验原部件零差异，失败自动回滚。
3. `exist_policy=fill|skip` 时绝不改已存在 sheet 的任何单元格。
4. 司机清单以"有目标文件 ∩ 数据源出现"为准；数据源里出现但无目标文件的司机只报告、不新建文件。
5. 仓库内不出现真实司机姓名/路径/金额；真实配置只在 `config/local/`（已 gitignore）。
