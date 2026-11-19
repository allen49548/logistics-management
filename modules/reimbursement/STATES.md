# STATES.md — 运行时状态说明

本技能运行会产生本地状态，全部位于 `state/`，**除 `schema.sql` 外均不入库**（见 `.gitignore`）。

| 文件 / 目录 | 作用 | 是否入库 |
| --- | --- | --- |
| `state/schema.sql` | 台账建表语句，首次运行前执行 | ✅ 入库（无数据） |
| `state/ledger.sqlite` | 幂等台账：司机 / 金额 / 日期 / 订单号 / 报表定位 | ❌ 忽略（真实财务数据） |
| `state/backups/` | 每次写报表前的原样备份副本 | ❌ 忽略（真实 Excel） |
| `state/runs/` | 每轮运行清单（chat_id / 任务 GUID / 金额） | ❌ 忽略（隐私） |
| `state/monthly_sent.json` | 月报发送记录 | ❌ 忽略 |

## 首次启动

1. 执行 `state/schema.sql` 建表。
2. `cp config.example.json config.json` 并填好占位符。
3. 安装依赖与 `lark-cli`，按 SKILL.md 跑一轮。

## 数据归属

- `ledger.sqlite` 是唯一账本，写入 / 去重以它为准。
- 报表（`report_root` 下的 Excel）是外部财务文件，本系统只做**加法累加**，且每次写前备份、写后回读校验，从不重建或覆盖。
