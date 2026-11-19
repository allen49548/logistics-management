# 配置说明

所有配置都是 JSON。**`config/local/` 优先于 `examples/config/`**；也可用
`--profile <目录绝对路径>` 或环境变量 `PAYROLL_PROFILE` 显式指定。

| 文件 | 作用 | 是否需脱敏 |
|---|---|---|
| `payroll.json` | 路径、精度、核对阈值、名册主源、确认闸门、通知通道、调度说明 | 路径与发起人姓名需脱敏 |
| `rules.json` | 每位司机的计薪规则 + 价目表 + 确认状态 | 是 |
| `field_map.json` | 来源报表 → 标准字段的列映射、司机别名、筛选条件 | 是 |
| `roster.json` | 司机名册、`open_id`、`notify` 开关 | 是 |
| `style.json` | 统一表格样式（字体/字号/颜色/背景/边框/列宽/数字格式） | 否 |

`config/local/` 整个目录已被 `.gitignore` 排除，可放心写真实姓名、路径与 open_id。

---

## payroll.json

| 字段 | 说明 |
|---|---|
| `precision` | 金额小数位，默认 2（Decimal + ROUND_HALF_UP） |
| `paths.data_dir` / `out_dir` / `state_dir` | 支持 `{root}` 展开；产物只写 `out_dir` |
| `checks.amount_tolerance` | 金额比对容差，默认 `0.01` |
| `checks.variance_warn_pct` / `variance_block_pct` | 环比波动复核线 / 阻断线（百分比） |
| `checks.max_km` | 单趟公里数上限，越界即阻断 |
| `checks.duplicate_key` | 判重字段组合，默认 `driver+date+route+km+trip_no` |
| `gate.owner` | **唯一可确认的发起人**；不匹配一律拒绝 |
| `gate.confirm_ttl_hours` | 确认有效期，默认 72 小时 |
| `roster.source` | `local`（样例）/ `feishu`（生产，名册主源） |
| `roster.feishu.department_query` / `chat_id` | 飞书通讯录部门关键词 / 群 id，两者取并集 |
| `roster.feishu.exclude_names` / `exclude_open_ids` | 排除群主、负责人等非司机成员 |
| `roster.feishu.cache_path` | 名册缓存，默认 `{root}/state/feishu_roster.json`（gitignore） |
| `notify.channel` | `dry-run` / `stdout` / `feishu`（生产）；**调用仍默认 dry-run** |
| `notify.feishu_roster_only` | 默认 `true`：只私发飞书在册司机 |
| `notify.owner_user_id` / `owner_chat_id` | 待确认通知发给谁（不是司机） |
| `notify.message_template` | 司机工资条文案，占位符见下 |
| `schedule` | 仅记录口径；实际调度由 WorkBuddy 自动化创建 |

`message_template` 占位符：`{month} {name} {gross} {trips} {km}`
`{reimbursement} {deductions} {net}`。**司机版不得出现其他司机姓名或金额**。

---

## rules.json

```json
{
  "version": 1,
  "tables": {"route_price_x": {"路线A": 150}},
  "drivers": {
    "DRIVER_ID": {
      "wage": {"op": "tier", "field": "km",
               "brackets": [{"lte": 60, "value": 130}], "default": 200},
      "adjustments": [{"op": "subtract", "label": "扣款", "amount": "50.00"}],
      "confirmed": true,
      "confirmed_by": "发起人",
      "confirmed_at": "ISO8601"
    }
  }
}
```

`wage` 支持 `tier` / `lookup` / `const` / `formula` / `sum`，详见
[`references/RULE_DSL.md`](../references/RULE_DSL.md)。
**`confirmed=false` 的规则会被核对阻断**，这是刻意设计。

`adjustments` 中 `source: "reimbursement"` 的项不参与计算（报销由报销数据源统一提供，避免双记）。

---

## field_map.json

```json
{
  "driver_aliases": {"旧姓名": "DRIVER_ID"},
  "trips_sources": [{
    "id": "main", "type": "xlsx", "path": "…/{M}月运输单.xlsx",
    "sheet": "Sheet1", "header_row": 2,
    "max_row": 20000, "max_col": 30,
    "columns": {"date": "A", "driver": "D", "km": "H", "route": "G"},
    "filters": {"require": ["driver","date","km"],
                "km_min": 1, "km_max": 3000,
                "exclude_unmapped_drivers": true}
  }],
  "reimbursement": {"type": "sqlite", "path": "…", "query": "SELECT …"},
  "history": {"enabled": false, ...},
  "external_bill": {"enabled": false, ...},
  "wage_sheet_reimbursement": {"enabled": false, ...}
}
```

要点：

- `columns` 值可为**表头名**（优先）、**Excel 列字母**、或 **0-based 数字**。
- 路径占位符：`{month}` `{M}` `{MM}` `{year}` `{YYYY}`。
- `max_row` / `max_col` 必须设置：真实报表常被格式撑到 1048576 行 / 16384 列。
- `exclude_unmapped_drivers: true` 是"筛选数据"主手段，只保留名册内司机，并透明记录筛除行数。
- **`sheet` 要明确指定**：同一文件多个 sheet 结构可能不同，全部读取会造成重复计薪。
- **同一趟运输只能进入一个启用的来源**，否则核对会判重复并阻断。
- `reimbursement.type=sqlite` 用只读 URI 打开，绝不改动报销台账。
- 图片来源：先转录成 CSV/JSON 再配一个 source；不在配置里塞图片路径。

---

## roster.json（本地补充名册）

```json
{"drivers": [{"id": "D01", "name": "示例司机A",
              "open_id": "ou_…", "notify": true}]}
```

启用 `roster.source = "feishu"` 后，`sync-roster` 会用飞书真实成员**覆盖** `open_id`，
并给每位司机打 `feishu: true/false` 标记（飞书优先＋本地补充的并集）。
飞书返回的姓名先经 `field_map.driver_aliases` 映射到本地 `id`，避免形近字/异体字分叉。

| 司机类型 | 参与计算 | 收到私聊 |
|---|---|---|
| 飞书在册 + 当月有单 | 是 | 是（还需 `notify=true` + 有效 `open_id`） |
| 仅本地名册 + 当月有单 | 是 | 否，`dispatch.json.skipped` 仅作信息记录 |
| 名册里当月 0 单 | **否**（不铺零值行） | 否 |

**「缺 open_id / 司机没到齐」不再是异常、待办或催办理由。** 只有在
「飞书在册但 open_id 无效」时才进 `missing_open_id`；绝不猜 open_id、绝不降级群发。

---

## style.json

由 `extract-style` 从发起人指定的模板 xlsx 抽取，或手工编写。字段见
[`references/STYLE.md`](../references/STYLE.md)。所有月份、所有司机、所有产出共用一份。
