"""公共工具：配置读取、路径展开、内置模块调用、结果输出。

本模块只提供通用能力；任何司机姓名、金额、业务口径都不在此硬编码。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
LOCAL_DIR = CONFIG_DIR / "local"

EXIT_OK = 0
EXIT_BUSINESS = 2
EXIT_CONFIG = 3


def emit(payload: Dict[str, Any]) -> None:
    """统一 JSON 输出（stdout），保证可被上层脚本/自动化解析。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def expand(path: Optional[str], base: Optional[Path] = None) -> Optional[str]:
    """展开 ~ 与环境变量；相对路径按 base（默认工具包根）解析。"""
    if not path:
        return path
    p = os.path.expandvars(os.path.expanduser(str(path)))
    if not os.path.isabs(p):
        p = str((base or ROOT) / p)
    return os.path.normpath(p)


def read_json(path: str | Path, base: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(expand(str(path), base))  # type: ignore[arg-type]
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data: Any, base: Optional[Path] = None) -> Path:
    p = Path(expand(str(path), base))  # type: ignore[arg-type]
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return p


def load_config() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """加载 modules.json（必）与业务配置（local 优先，example 兜底）。"""
    modules = read_json(CONFIG_DIR / "modules.json")
    local = LOCAL_DIR / "config.json"
    example = CONFIG_DIR / "config.example.json"
    cfg_path = local if local.exists() else example
    cfg = read_json(cfg_path)
    cfg["_config_source"] = str(cfg_path)
    return modules, cfg


def resolve_skill_root(cfg_module: Dict[str, Any]) -> Path:
    root = expand(cfg_module.get("skill_root", ""), ROOT)
    if not root:
        raise ValueError("模块缺少 skill_root")
    return Path(root)


def run_delegate(
    module_cfg: Dict[str, Any],
    action_cfg: Dict[str, Any],
    extra_args: Iterable[str] = (),
    timeout: Optional[int] = None,
    capture: bool = False,
) -> Dict[str, Any]:
    """调用本工具包内置模块的脚本，原样透传参数与输出。

    子进程的工作目录设为内置模块根目录，保证其相对路径配置可正确解析；
    不访问任何外部 skill。MCP 调用使用 capture=True 避免污染 stdio 协议。
    """
    skill_root = resolve_skill_root(module_cfg)
    script = skill_root / action_cfg["script"]
    cmd: List[str] = [sys.executable, str(script)]
    sub = action_cfg.get("subcommand")
    if sub:
        cmd.append(sub)
    cmd.extend(str(a) for a in extra_args)
    if not script.exists():
        return {
            "ok": False,
            "error": f"委托脚本不存在：{script}",
            "exit_code": EXIT_CONFIG,
            "command": cmd,
        }
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(skill_root),
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "委托执行超时", "exit_code": EXIT_BUSINESS, "command": cmd}
    result: Dict[str, Any] = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "command": cmd,
        "cwd": str(skill_root),
    }
    if capture:
        result["stdout"] = proc.stdout
        result["stderr"] = proc.stderr
    return result


def normalize_driver(name: Optional[str], aliases: Dict[str, str]) -> str:
    if not name:
        return ""
    cleaned = str(name).strip()
    return aliases.get(cleaned, cleaned)


def match_alias(token: str, alias_map: Dict[str, List[str]]) -> Optional[str]:
    """按配置的别名表把表头/字段识别为语义键（最长别名优先）。"""
    t = str(token).strip().lower()
    best_key, best_len = None, 0
    for key, aliases in alias_map.items():
        for alias in aliases:
            a = str(alias).strip().lower()
            if not a:
                continue
            if (t == a or a in t) and len(a) > best_len:
                best_key, best_len = key, len(a)
    return best_key


def make_key(fields: Dict[str, Any], key_fields: Iterable[str]) -> str:
    import hashlib

    parts = [str(fields.get(k, "")) for k in key_fields]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
