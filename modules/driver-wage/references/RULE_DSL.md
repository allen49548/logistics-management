# 计薪规则 DSL

每位司机算法不同，因此**算法是数据，不是代码**。规则放在 `rules.json`，版本可 diff、可评审、可逐条确认。

## 一、五种规则

| op | 用途 | 示例 |
|---|---|---|
| `const` | 固定单价 | `{"op":"const","value":150}` |
| `tier` | 公里数阶梯（历史表的嵌套 IF） | `{"op":"tier","field":"km","brackets":[{"lte":60,"value":130},{"lte":150,"value":150}],"default":200}` |
| `lookup` | 路线价目表（历史表的 VLOOKUP） | `{"op":"lookup","key":"route","table":"route_price_x","default":0}` |
| `formula` | 原样 Excel 逐行公式 | `{"op":"formula","expr":"=IF(G2<=60,130,150)","cell_map":{"G2":"km"}}` |
| `sum` | 多段相加 | `{"op":"sum","items":[…]}` |

`brackets` 按顺序取第一个 `value <= lte` 命中项；全不命中用 `default`（缺失即报错，绝不猜）。
`tables` 是顶层字典，供 `lookup` / `formula` 里的 `LOOKUP()` 使用。

## 二、调整项 `adjustments`

```json
"adjustments": [
  {"op": "subtract", "label": "扣款", "amount": "50.00"},
  {"op": "add", "label": "补贴", "amount": "30.00"}
]
```

- 报销**不写在这里**：由 `field_map.reimbursement` 统一提供，避免两个来源双记。
- 若某项标了 `"source":"reimbursement"`，计算阶段会跳过它。
- 实发 = 逐行工资合计 + 固定加项 − 固定扣项 + 报销 − 扣款合计。

## 三、安全求值器（`formula`）

不用 `eval`。流程：

1. `translate_excel` 去掉开头 `=`，把 `cell_map` 里的单元格引用（如 `G2`、`$J2`）替换为标准字段名，把 `<>` 换 `!=`、单 `=` 比较换 `==`。
2. `ast.parse(..., mode="eval")` 后用**白名单节点**递归求值。
3. 白名单函数：`IF` `ROUNDDOWN` `ROUNDUP` `ROUND` `INT` `ABS` `MIN` `MAX` `SUM` `LOOKUP`。
4. 拒绝：属性访问、下标、lambda、推导式、幂运算、关键字参数、跨表引用（`!`）、范围引用（`G2:G12`）、未知变量、未知函数、除零、布尔值当金额。

`IF` **惰性求值**：只计算命中的分支，未命中分支里的除零不会误报。
数值全程 `Decimal`；`ROUNDDOWN` 向零取整、`ROUND` 为 `ROUND_HALF_UP`，与 Excel 口径一致。
合计由 `compute` 独立累加，公式只负责逐行，因此不需要支持范围引用。

**表达式共用说明（诚实边界）**：`verify` 的独立重算对 `tier` / `lookup` / `const` / 聚合全部另写一套实现；只有 `formula` 复用同一个安全求值器，该求值器由 `tests/test_rules.py` 单独覆盖（含注入攻击、惰性、取整、未映射引用）。这样既能迁移历史 Excel 公式，又不会让"同一段代码自证自己"。

## 四、把历史工资表平移过来

真实历史表里常见四种形态，各自的处理：

| 历史形态 | 处理方式 |
|---|---|
| 嵌套 `IF(km<=…)` 阶梯 | `learn-rules` 保留原公式 + `cell_map`；可选手工整理成更可读的 `tier` |
| `VLOOKUP` 路线价目表 | 转成 `lookup`，同时把参数 sheet 抽成 `tables.<name>` |
| 固定数值 | `const` |
| `ROUNDDOWN(IF(...)*系数,0)` | `formula`，`cell_map` 映射公里数列 |
| 数组公式 / 多义表头 / 空值 | **标 `needs_manual`**，绝不猜；由发起人手工补录 |

操作：

```bash
python3 scripts/payroll.py learn-rules \
  --wage-dir <历史工资表目录> --out config/local/rules.candidates.json
```

产物同时包含 `field_map.candidates.json`（每位司机自己的列布局），以及这些警告：表内姓名与文件名不一致、工资列命中多个（歧义）、数组公式读不出、公式形态未支持。

**候选规则一律 `confirmed=false`**。发起人须逐条核对：

1. 打开 `rules.candidates.json`，对照历史工资表确认 `wage` 与原公式一致；
2. 对 `needs_manual` 的司机手工补 `wage`；
3. 把确认无误的规则写入 `config/local/rules.json`，设 `confirmed=true` 并填 `confirmed_by` / `confirmed_at`；
4. 跑 `compute` + `verify`，确认 `rule_compliance` 通过。

未确认的规则会被核对判 BLOCK，**不可能被拿去发薪**。

## 五、规则变更管理

- 改规则 = 改 `input_hash` → 已有确认立即失效，必须重新计算、核对、确认。
- 建议每条规则保留 `source`（来自哪份历史表、哪个单元格）与 `confirmed_at`，便于追溯。
- 价目表（`tables`）同样是指纹的一部分，改价目也要重新确认。
- 不要用"临时改一下再改回来"绕过确认：结果指纹会变，且审计日志会留痕。

## 六、常见错误

| 现象 | 原因 / 处理 |
|---|---|
| `未映射的单元格引用 G2` | `cell_map` 缺该引用；按该司机表头补列字母映射 |
| `tier 未命中且无 default` | 公里数超出所有档位；补 `default` 或检查数据 |
| `价目表缺 '某路线'` | 路线名与价目表不一致；补表或设 `default`（设了 default 要人工确认是否合理） |
| `不支持跨表引用` | 原公式含 `'表名'!范围`；转成 `lookup` + `tables` |
| `布尔值不能当金额` | 明细里的控制字段被当公式变量；`formula_variables` 已排除 bool，勿手动塞整条 trip |
| 金额差 0.01 | 取整口径不同；确认是逐行取整后累加，还是合计后取整，写进规则说明 |
