---
name: driver-monthly-sheet
description: 司机月度表生成技能。以「WPS 根目录的单 sheet 模板」为格式样张，在各司机真实报表（{司机}{年}.xlsx）里精确插入一个格式/字段完全一致的月份 sheet（如 2026年11月），把「表格文档」里按司机姓名分组的运输数据填入对应司机，并把该月报销明细一并并入。以图表/报表中的司机名称作为目标标识定位对应司机的 sheet；只新增不覆盖——sheet 不存在则新建、已存在则按去重指纹只追加新行，绝不清空/替换已有内容；目标文件不存在则基于模板新建。全配置驱动不硬编码；zip 层精确追加/插入保护 WPS 文件特有部件（customXml/metadata/vmlDrawing 等）。适用于根据图片或报表更新司机工资报表、按司机名分发表格数据、增量追加运输明细、报销并入月度表等请求。
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

- **只新增、绝不覆盖（2026-11-19 定案）**：① 目标标识 = 图表/报表里的司机名称，只处理数据源中出现的司机；② sheet 不存在则新建写入，目标文件不存在则基于模板新建；③ sheet 已存在则按去重指纹（`sheet.dedup_columns`）只追加本轮真正新增的行，保留已有 sheet 与原有内容，不重建、不清空、不替换；④ 本技能**不提供覆盖能力**：`exist_policy=overwrite` 在写入层强制降级为 append。
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
- `sheet.name_pattern`：`{year}年{month}月`；`exist_policy`：仅作兼容字段，写入层一律按 `append`（只增不覆盖）处理；`sheet.dedup_columns`：去重指纹列（模板列名列表，如 `["日期","车牌","司机","路线"]`），留空则按整行去重。
- `drivers.from`：`target_dir`（扫描文件名得到司机清单）/ `list`（显式名单）。

## 六、目标识别与幂等增量写入（2026-11-19 定案）

- **目标识别**：以数据源（图表/报表）里的司机名称为标识，定位 `{target_dir}/{driver}{year}.xlsx`；只处理数据源里出现的司机，不扫描目录全量。
- **sheet 不存在**：新建该 sheet 并按模板写入；目标司机文件整个不存在时，基于模板新建文件（保留 WPS 特有部件）后写入。
- **sheet 已存在**：读取已有行指纹，只追加本轮新增且不重复的行（追加在 `</sheetData>` 前，已有行字节不变）；重复运行零改动，天然幂等。
- **无数据源（兼容）**：`paths.source` 不存在时回退到目标司机清单，生成只有表头+样式骨架的空 sheet。

## 七、铁律

1. 写入前必须 `plan` 核对；默认 dry-run，绝不未经确认写真实表。
2. 每次真实写入都备份到 `backup_dir`，回读校验原部件零差异，失败自动回滚。
3. 已存在 sheet 只追加新行，绝不改动、覆盖、清空已有单元格；覆盖能力已移除。
4. 去重指纹列从 `sheet.dedup_columns` 读取（模板列名），同一指纹的行不会重复追加。
4. 司机清单以"有目标文件 ∩ 数据源出现"为准；数据源里出现但无目标文件的司机只报告、不新建文件。
5. 仓库内不出现真实司机姓名/路径/金额；真实配置只在 `config/local/`（已 gitignore）。
