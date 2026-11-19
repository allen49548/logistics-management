"""通用工具：期间解析、标签归一化、金额解析、目录处理。"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------- 期间 ------
class PeriodError(ValueError):
    """期间格式错误。"""


def parse_period(text: str) -> dict:
    """把 ``2026-08`` / ``2026.8`` / ``2026/8`` 解析成 {year, month}。"""
    if text is None:
        raise PeriodError("期间不能为空")
    m = re.fullmatch(r"\s*(\d{4})\s*[-./年]\s*(\d{1,2})\s*月?\s*", str(text))
    if not m:
        raise PeriodError(f"无法解析期间：{text!r}（应形如 2026-08）")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise PeriodError(f"月份越界：{text!r}")
    return {"year": year, "month": month}


def prev_period(period: dict) -> dict:
    year, month = period["year"], period["month"] - 1
    if month == 0:
        year, month = year - 1, 12
    return {"year": year, "month": month}


def is_adjacent(current: dict, previous: dict) -> bool:
    """previous 是否为 current 的上一个月。"""
    p = prev_period(current)
    return p["year"] == previous["year"] and p["month"] == previous["month"]


def period_text(period: dict) -> str:
    return f"{period['year']}-{period['month']:02d}"


def period_ctx(period: dict, template: str) -> str:
    """按模板渲染月份相关文本，如 ``{year}.{month}`` -> ``2026.8``。"""
    return template.format(year=period["year"], month=period["month"])


# ------------------------------------------------------------ 标签归一化 ----
def normalize_label(text: Any, strip_parenthetical: bool = True) -> str:
    """标签归一化：全角转半角、去空白、去尾部冒号、可选去括号注释。"""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = s.replace("\u3000", "").replace(" ", "").replace("\t", "")
    s = s.replace("：", "").replace(":", "")
    s = s.strip().rstrip("*").strip()
    if strip_parenthetical:
        s = re.sub(r"\([^()]*\)$", "", s).strip()
    return s


def is_numeric_like(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_number(value: Any, strip_chars: Iterable[str] = ()) -> Optional[float]:
    """把单元格值解析成金额；不可解析返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time, _dt.timedelta)):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    for ch in strip_chars:
        text = text.replace(ch, "")
    text = text.replace("%", "") if text.endswith("%") else text
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?", text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def statement_like(value: Any) -> bool:
    """判断是否为公式（用于识别未缓存值的公式单元格）。"""
    return isinstance(value, str) and value.strip().startswith("=")


# ---------------------------------------------------------------- 杂项 ------
def deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator
