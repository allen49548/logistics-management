# 通用化设计：让任何人、任何组织都能无痛接入

本技能的业务内核（**计算工资 + 核对工资**）与具体组织无关，真正会卡住「换一家公司」的，
是内核之外的输入与交付环节。本文说明如何把这两层做「可插拔」，从而适配所有人。

## 一、为什么不能直接照搬

| 换公司时的摩擦 | 旧实现 | 通用化后 |
|---|---|---|
| 用什么 IM 发工资条 | 硬编码飞书 | 通道可插拔：csv-export / stdout / feishu / 可扩展 |
| 司机名册从哪来 | 硬编码飞书通讯录 | 来源可插拔：manual / csv / excel / feishu |
| 怎么生成第一份配置 | 手改 5 个 JSON | `init` 指向样本文件，自动出草稿 + 人话复核清单 |
| 规则 DSL 怎么写 | 人手写表达式 | `learn-rules` 反推候选；规则必须逐条人工确认 |
| 配置错了报什么 | 技术 JSON 报错 | `validate` 人话告诉你缺什么、下一步做什么 |

## 二、可插拔通道（notify.channel）

实现见 `scripts/adapters.py` 的 `Channel` 与 `CHANNELS` 注册表。新增一家 IM 只需
继承 `Channel` 实现 `send(open_id, text, key, cfg)`，再到 `CHANNELS` 注册。

| 通道 | 用途 | 是否真发 |
|---|---|---|
| `dry-run`（默认） | 只生成预览，绝不外发 | 否 |
| `stdout` | 把每条工资条打印到终端，便于本地验证 | 否 |
| `csv-export` | 把每条工资条导出成一个 .txt 文件 | 否（产出可投递文件） |
| `feishu` | 飞书逐人私聊（含 bot→user 身份降级、幂等键） | 是 |
| `email`（占位） | 演示通道可插拔；未接真实 SMTP | 否 |

**关键**：没有对接任何 IM 工具的用户，用 `csv-export` 就能拿到「每条一个文件」的可投递
产物，手动邮件/微信发送即可，不必等飞书集成。

## 三、可插拔名册来源（roster.source）

实现见 `scripts/adapters.py` 的 `RosterSource` 与 `ROSTER_SOURCES`。

| 来源 | 含义 | 是否联网 |
|---|---|---|
| `manual`（默认） | 名册即 `roster.json` 本地名册 | 否 |
| `csv` / `excel` | 从一份名册文件（id,name,open_id,notify）读取 | 否 |
| `feishu` | 从飞书通讯录/群同步（只读、可离线降级） | 是（仅 sync-roster 时） |

`apply_roster_source` 按 `source` 选适配器并做「飞书优先 + 本地补充」并集，所有来源走统一
接口；飞书不再是特例，脱敏样例用 `manual` 即可跑通。

## 四、引导式接入 init

```
python3 scripts/payroll.py init \
  --trip-file 运输单.xlsx --wage-dir 历史工资表/ \
  --template 工资表模板.xlsx [--roster-file 名册.csv] \
  --out-dir config/local
```

自动完成：抽取统一样式 `style.json`、反推测薪规则候选 `rules.candidates.json`、
建立名册 `roster.json`、生成草稿 `payroll.json` / `field_map.json`，并输出一份人话
`REVIEW.md`。所有生成的规则 `confirmed=false` —— 设计上绝不自动放行，必须人核对。

## 五、人话校验 validate

`python3 scripts/payroll.py --profile <p> validate` 不再抛技术错误，而是输出：
发起人是否填、通道是否合法、来源参数是否齐全、有几条规则未确认、名册是否为空，
并给一句「下一步做什么」。新手照着修即可。

## 六、边界不变

适配器、init、validate 全部属于「输入准备 / 交付编排」，不是第三个业务功能。
技能仍然只做两件事：计算工资、核对工资。init 不改任何历史工资表；真实数据只在
`config/local/`（gitignore）与 `out/`，仓库内全脱敏，可直接开源。
