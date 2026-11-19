# AGENTS.md — 维护者指南

本文件是公开维护规则（与 khazix-skills 一致：`.claude/settings.local.json` 等本地配置不入库，规则写在这里）。

## 仓库性质

这是**单个** Agent Skill（遵循 Agent Skills 开放标准）。`SKILL.md` 在仓库根。若要扩成多技能合集，把本技能移入子目录并在 `.claude-plugin/marketplace.json` 登记。

## 贡献前必读

- **绝不提交真实数据**：`config.json`、`lark-im-resources/`、`state/ledger.sqlite`、`state/backups/`、`state/runs/`、`state/monthly_sent.json` 已被 `.gitignore` 排除。改动后务必 `git status` 复核。
- **配置模板同步**：改了运行配置结构，必须同步更新 `config.example.json` 的占位符与说明，保证他人 `cp` 后可用。
- **脚本可编译**：提交前 `python -m py_compile scripts/*.py`，CI 会再校验一次。
- **SKILL.md frontmatter**：`name` / `description` 是 Agent Skills 的触发契约；`description` 要讲清触发场景与不触发场景，改动需谨慎。

## 代码结构

- `ledger.py`：幂等台账 + 四层去重（`message_id → SHA-256 → dHash → 业务键`）+ 四态 + 余额。改动去重逻辑务必保留「相同金额不是重复，以订单号 + 支付时间为准」。
- `notify.py`：两层通报发送；`per_driver_dm.enabled=false` 时必须硬拦截司机私聊（即使旧自动化误传 `--driver-dm-manifest`）。
- `references/`：去重、通报、报表布局三份规则，与 SKILL.md 的铁律保持一致。

## 分支与发布

- 默认分支 `main`。改动走 Pull Request。
- 改了技能内容再更新版本号；只改文档 / README 不必升版本。
