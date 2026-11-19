#!/usr/bin/env python3
"""配置加载、profile 解析与输入指纹。

开源约定：仓库内只放脱敏样例（examples/config/）；真实数据放 config/local/，
已被 .gitignore 排除。默认优先使用 local，其次 example，绝不把真实姓名、路径、
金额、open_id 写进可开源代码或配置样例。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
LOCAL_DIR = CONFIG_DIR / "local"
EXAMPLE_DIR = ROOT / "examples" / "config"

REQUIRED_FILES = ("payroll.json", "rules.json", "field_map.json",
                  "style.json", "roster.json")


class ConfigError(ValueError):
    pass


def profile_dir(profile: str | None = None) -> Path:
    p = profile or os.environ.get("PAYROLL_PROFILE") or "auto"
    if p == "local":
        return LOCAL_DIR
    if p == "example":
        return EXAMPLE_DIR
    if p == "auto":
        return LOCAL_DIR if (LOCAL_DIR / "payroll.json").exists() else EXAMPLE_DIR
    path = Path(p)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_json(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置 JSON 非法 {path}: {exc}") from exc


def load_all(profile: str | None = None) -> dict:
    base = profile_dir(profile)
    missing = [n for n in REQUIRED_FILES if not (base / n).exists()]
    if missing:
        raise ConfigError(f"profile {base} 缺少配置: {', '.join(missing)}")
    cfg = {n[:-5]: load_json(base / n) for n in REQUIRED_FILES}
    cfg["_meta"] = {"profile": str(base), "root": str(ROOT)}

    main = cfg["payroll"]
    for key, default in (("data_dir", str(ROOT / "examples" / "data")),
                         ("out_dir", str(ROOT / "out")),
                         ("state_dir", str(ROOT / "state"))):
        raw = main.get("paths", {}).get(key, default)
        cfg.setdefault("paths", {})[key] = expand(raw, main, cfg["_meta"])
    _apply_roster_source(cfg)
    return cfg


def _apply_roster_source(cfg: dict) -> None:
    """名册主源由 adapters.ROSTER_SOURCES 按 payroll.roster.source 选择。

    manual（默认，只读 roster.json）/ csv / excel / feishu 走同一接口；
    飞书不再被硬编码，脱敏样例与离线环境用 manual 即可跑通。
    """
    import adapters
    adapters.apply_roster_source(cfg)


def expand(value: str, main: dict, meta: dict) -> str:
    """展开 {root} / {data_dir} / $ENV_VAR，返回绝对路径。"""
    if not isinstance(value, str):
        return value
    ctx = {"root": meta["root"], "profile": meta["profile"]}
    ctx.update({k: v for k, v in main.get("paths", {}).items() if isinstance(v, str)})
    for _ in range(3):
        try:
            value = value.format(**ctx)
        except (KeyError, IndexError):
            break
    for name, val in os.environ.items():
        value = value.replace("${" + name + "}", val)
    return os.path.abspath(os.path.expanduser(value))


def canonical_hash(obj) -> str:
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def input_fingerprint(cfg: dict, paths: list[str]) -> str:
    """输入指纹：任一输入或配置变化都会让已确认的结果失效，必须重新确认。"""
    payload = {
        "rules": cfg["rules"], "field_map": cfg["field_map"],
        "roster": cfg["roster"], "checks": cfg["payroll"].get("checks", {}),
        "inputs": {os.path.basename(p): file_hash(p) for p in sorted(paths)
                   if os.path.isfile(p)},
    }
    return canonical_hash(payload)
