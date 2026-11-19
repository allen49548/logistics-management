#!/usr/bin/env python3
"""配置读取与运行时默认值（单一真源）。

目的：把散落在各脚本里的硬编码常量集中到 config.json 的 `runtime` 节，
由本模块统一提供「配置优先、原值兜底」的读取函数。默认值与改造前完全一致，
因此未配置 runtime 时输出逐字节不变；填写 runtime 后即可免改代码调整口径。

设计原则：
  · 不改动 config.json 原有任何键，只在末尾新增可选 `runtime` 节；
  · 所有 helper 都有与原硬编码相同的默认值，缺配置/缺键/类型错误时静默回退；
  · 本模块只依赖标准库，可被所有脚本安全 import（含 subprocess 调用的独立进程）。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.abspath(os.path.join(HERE, "..", "config.json"))

# 以下 DEFAULT 即改造前各脚本里的硬编码值，保证输出不变。
DEFAULT = {
    "default_year": 0,                  # 0 = 取当前系统年份（替代写死的 2026）
    "unknown_driver": "未识别司机",
    "default_group_name": "物流部",
    "sheet_name": "11月报销明细",       # 2026-11-16 统一报表名（原「报销明细」），旧名自动迁移
    "run_log_window_days": 14,          # owner_copy 文案里的「窗口 N 天」
    "phash_lookback_days": 90,          # ledger L3 感知哈希回溯天数
    "no_change_line_override": None,    # 群内无新增文案；None=用 driver_message.no_change_wording
    "no_change_wording": "本次没有新的报销，之前记过的都算数。",
    "monthly_no_change_wording": "上月没有报销，之前记过的都算数。",
    "undated_group": "未标月份",        # 报表按月分节的「无日期」分组名
    "idempotency_prefix": "reimb",      # notify / group_task 幂等键前缀
    "idempotency_max_len": 50,          # 幂等键最大长度
    "chat_id_key_len": 8,               # 幂等键里 chat_id 截断长度
    "open_id_prefix": "ou_",            # open_id 格式校验前缀
    "lark_identity_fallback": ["bot", "user"],  # 群消息发送身份降级顺序
    "task_url_template": "https://applink.feishu.cn/client/todo?guid=%s",
    "description_max_chars": 1000,
    "joke_fallback": "慢点开，平安到，比什么报销都划算。",
    "date_max_show": 4,
    "issue_prefix": "小提示：",
    "header_template": "【报销通知｜%s】%s",
    "name_len_min": 2,                  # plausible_person_name 长度下限
    "name_len_max": 12,                 # 上限
    "name_forbidden_tokens": ["@", "/", "\\", ":", " ", "群", "机器人", "CLI", "Bot", "bot"],
    "name_require_cjk": True,
    "tmp_dir": "/tmp",                  # monthly_summary 中间文件目录
    "locator_max_rows": 2000,           # locate_report 扫描上限
    "locator_max_cols": 40,
    "locator_label_max_chars": 20,
    "locator_label_contains": ["报销"],
    "locator_label_excludes": ["备注", "说明", "口径"],
    "locator_right_offset": 3,
}


def load_config(path=None):
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def runtime(cfg=None):
    """取 config.runtime 节（缺省为空 dict）。"""
    if cfg is None:
        cfg = load_config()
    r = cfg.get("runtime")
    return r if isinstance(r, dict) else {}


def get(cfg, key, default=None):
    """配置优先：runtime.<key> → 本模块 DEFAULT → 传入 default。"""
    if default is None and key in DEFAULT:
        default = DEFAULT[key]
    v = runtime(cfg).get(key, default)
    return default if v is None and default is not None else v


def state_dir(cfg=None):
    if cfg is None:
        cfg = load_config()
    return cfg.get("state_dir") or os.path.join(HERE, "..", "state")


def current_year(cfg=None):
    """业务年份：runtime.default_year>0 时用配置值，否则取当前系统年份。"""
    from datetime import date
    y = int(get(cfg, "default_year") or 0)
    return y if y > 0 else date.today().year
