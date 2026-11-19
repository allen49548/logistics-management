# 输入输出方式配置化（adapters）

> 2026-11-15 新增。与开源包 `document-intake-ledger` 的 `adapters.input` / `adapters.output` 结构一致。

## 一、要解决的问题

改造前是「**参数可配、方式不可配**」：`config.json` 里能改群 ID、报表路径、天数、人名映射，但下面这些「方式」写死在脚本里：

- 输入固定 `lark-cli im +chat-messages-list --as user ... --download-resources`；
- 文档固定只认机打付款凭证截图、由视觉识别抽取；
- 输出固定写 `报销明细` sheet（`reimb_detail.py` 里的 `SHEET_NAME` 常量）；
- 通报固定「群内极简总览 + 负责人私聊精简版」两层；
- 确认任务固定走飞书任务。

`adapters` 把这些「方式」提升为配置声明，使不同项目可以换输入源、换写入目标、换通报通道，而不必改流程文档。

## 二、三条设计原则

1. **单一真源**：`adapters` 只声明用哪种方式，具体参数仍放在原有顶层键里。所有 `*_ref` 是指针，不是副本；禁止在 `adapters` 里重复写一份值。
2. **声明式优先**：当前 `resolution = "declarative"`。这一节是契约，不是分发器——现有脚本不读它，仍按内建默认实现运行。两条定时自动化（每日归集、月初月报）的行为与改造前完全一致。
3. **方式可换、铁律不换**：切换任何适配器都不得削弱去重、状态口径、写入安全与通报透明性（见第五节）。

## 三、输入（`adapters.input`）

| 维度 | 配置路径 | 当前默认值 | 对应现状 |
| --- | --- | --- | --- |
| 消息来源通道 | `input.source` | `type: im_group`，`mode: shared`，白名单/停用群/发现规则分别 `_ref` 到 `chat_whitelist` / `retired_chats` / `chat_discovery` | 单一混合群，停用群不读不发 |
| 读取身份与客户端 | `input.client` | `adapter: lark-cli im +chat-messages-list`、`identity: user`、`page_all/order/no_reactions/download_attachments` | `--as bot` 会报 230002，故固定 user；图片落到 `lark-im-resources/` |
| 文档形态 | `input.documents` | `extractor: ViewImage`、`supported_shapes: ["image"]`，类目与阈值 `_ref` 到 `ocr.*` | 只认机打付款凭证截图；手写票、纯文本跳过 |
| 拉取窗口与增量策略 | `input.window` | `strategy: lookback_days`、`clock: system_date`、`incremental: false`、`full_rescan_each_run: true` | 每轮按系统 `date` 取最近 14 天全量重扫；跳过 system / 已撤回消息 |

`alternatives` 列出**已规划但尚未实现**的可选方式：输入侧含 `local_directory` / `http_api` / `message_queue` / `pdf` / `structured_json` / `incremental_cursor` / `webhook_events`。写入 `alternatives` 不等于生效。

`identity_fallback: none` 表示读取身份失败就是错误，绝不静默降级到另一种身份。

## 四、输出（`adapters.output`）

| 维度 | 配置路径 | 当前默认值 | 对应现状 |
| --- | --- | --- | --- |
| 写入目标 | `output.write` | `type: xlsx_detail_sheet`、`adapter: scripts/reimb_detail.py`，目标/ sheet / 策略 `_ref` 到 `report_root` / `report_detail_sheet` / `write_policy` | 只在已存在的年份报表内追加「报销明细」sheet 的明细行；`idempotency_key: message_id` |
| 通报通道 | `output.notifications.channels[]` | 三条：`group_overview`（群，极简）、`owner_detail`（负责人私聊，精简专业版）、`per_driver_dm`（`enabled=false` + `hard_blocked=true`） | 两层通报，绝不发司机私聊 |
| 确认任务系统 | `output.tasks` | `backend_ref: group_task`、`adapter: scripts/group_task.py`，分批与权限 `_ref` 到 `group_task.batching` / `task_permissions` | 按司机分组建任务，负责人可编辑、报销人仅查看，轮询完成状态 |
| 渲染格式与内容分层 | `output.rendering` | `renderer_registry` 指向 `driver_copy.py` / `owner_copy.py`；字段可见性 `_ref` 到 `output.notifications.channels`；`policy_hooks_ref: driver_message` | 群内版只写金额/日期/@人/任务链接；专业版保留四态、去重明细、路径等全部审计字段 |

`write.alternatives`：`csv` / `database` / `none`（只出清单不写）。`notifications.alternatives`：`webhook` / `none`。`tasks.alternatives`：`none` / `custom_http`。

`hard_blocked: true` 的通道即使被旧自动化 prompt 误传参数，也必须在发送端硬拦截，不得降级成群消息。

## 五、切换适配器的步骤

1. 实现对应能力：输入侧至少提供「按窗口取消息 + 保留发送者身份与时间戳」，输出侧至少提供「写入 / 发送 + 幂等回执」。
2. 在对应 `alternatives` 中登记该方式，再改 `type` 或 `adapter` 字段。
3. **保持所有 `*_ref` 不变**，除非新方式确实需要不同参数；新参数加到被引用的顶层键里，不要写进 `adapters`。
4. 把 `resolution` 从 `declarative` 改为可分发模式，并实现分发逻辑（当前脚本未实现，直接改会启动报错）。
5. 先 dry-run：关闭写入与发送，核对识别结果、归属解析、去重判定与渲染内容。
6. 打开单个通道验证回执，再逐步放开。

## 六、切换方式时不可削弱的铁律

- 去重以「订单号 + 支付时间」为准，相同金额不是重复；四层命中必须透明注明原登记时间与是否已报销。
- 只有「已登记·待打款」计入未结；其余四态金额不进任何合计。
- 无报表文件一律 `record_without_write`，绝不新建报表文件。
- 每笔写入必须先备份、写后回读校验（未改动部件零差异）、失败自动回滚；台账登记必须在校验成功之后。
- `--driver-dm-manifest` 硬拦截，绝不向司机发私聊。
- 每轮无论有无新增/异常，两层通报都必须发。
- 司机归属绝不猜；弹性名册不催到齐、不铺零值行。

## 七、与开源包的一致性边界

两侧共享同一套契约骨架：`adapters.input.{source, client, documents, window}` 与 `adapters.output.{write, notifications, tasks, rendering}`，八个维度一一对应，字段命名规则相同（`type` 选方式、`*_ref` 指参数、`alternatives` 列候选）。

允许的差异只有两类：

- **值**：契约侧用 `<PLACEHOLDER>` 与抽象通道名，实现侧用真实默认值与具体脚本/客户端标识。
- **扩展字段**：实现侧可带实现细节字段（如 `no_reactions` / `mode` / `period_field_priority`），契约侧可带额外的 `*_ref` 指针（如 `safety_ref` / `poll_completion_ref`）。扩展字段不得改变骨架语义，也不得复制被引用参数的值。

`schema_version` / `resolution` / `hard_blocked` / `identity_fallback` 等关键字段两侧必须同名同义。注意：`_ref` 只指向配置路径，脚本路径用普通 `adapter` 字段表示，不要写成 `_ref`。

## 八、校验

新增 `adapters` 后应检查：

- 两个配置文件都能被 JSON 解析，且 `adapters` 骨架与开源包一致；
- 每个 `*_ref` 都能在 `config.json` 里解析到实际键；
- `adapters` 内没有与被引用键重复的值；
- `hard_blocked: true` 的通道同时 `enabled: false`；
- `config.json` 除新增 `adapters` 外与改造前逐字段相同；
- 没有改动 `scripts/` 下任何文件；两条定时自动化的 prompt 与绝对路径未变。

改造前的 `config.json` 已备份到 `state/backups/config.json.bak-20261115_015416`（该目录已 gitignore）。

## 九、已知遗留（本次未改，需另行确认）

- `config.example.json` 的 `driver_open_ids` 键仍是真实人名，`cloud_sync` 里仍有真实云端资源 ID。该文件会入库，建议后续统一占位化。
- 公开仓库的历史提交里曾带入真实 `oc_` / `ou_` ID，仅脱敏当前版本不清除历史。
