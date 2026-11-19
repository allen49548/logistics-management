# 维护规则（driver-wage）

- **一个仓库只放这一个技能**。新能力请另开仓库，不要往这里塞第二个 skill。
- 改动走分支 + PR；CI 会校验 `scripts/*.py` 可编译、`SKILL.md` frontmatter 完整。
- 提交前必查：`git status` 里不得出现 `config.json`、`.env`、`state/` 下真实文件。
- 版本号在 `.claude-plugin/marketplace.json`：新增能力升 minor，修 bug 升 patch，
  目录结构或调用方式破坏性变更升 major。
