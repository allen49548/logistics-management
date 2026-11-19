"""司机源文件发现、月份工作表定位、旧报表的清理（重生成前先删除）。"""
from __future__ import annotations

import fnmatch
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl

from common import now_text, period_text
from config import Config


class DiscoveryError(RuntimeError):
    """找不到可用的源目录 / 文件。"""


@dataclass
class DriverSource:
    name: str
    path: Path
    raw_name: str = ""


@dataclass
class SheetHit:
    sheet: Optional[str]
    reason: str = ""


def _excluded(name: str, globs: List[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in globs)


def _canonical_name(raw: str, aliases: Dict[str, List[str]]) -> str:
    """按配置的别名表把「幸荣超」这类异体文件名归并到同一个司机。"""
    for canonical, variants in aliases.items():
        if raw == canonical or raw in variants:
            return canonical
        for variant in variants:
            if variant and variant in raw:
                return canonical
    return raw


def discover_drivers(cfg: Config) -> Tuple[List[DriverSource], List[str]]:
    """扫描司机目录，返回（司机源列表, 提示信息）。"""
    directory = cfg.driver_dir
    if not directory.exists():
        raise DiscoveryError(f"司机目录不存在：{directory}")

    patterns = cfg.get("source.file_patterns", ["*.xlsx"])
    excludes = cfg.get("source.exclude_globs", [])
    name_regex = cfg.get("source.driver_name_regex")
    aliases = cfg.get("source.driver_aliases", {}) or {}

    seen_path = set()
    found: Dict[str, DriverSource] = {}
    notes: List[str] = []

    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            if not path.is_file() or _excluded(path.name, excludes):
                continue
            if path.resolve() in seen_path:
                continue
            seen_path.add(path.resolve())

            raw = None
            if name_regex:
                match = re.match(name_regex, path.stem)
                if match:
                    raw = (match.groupdict().get("name") or "").strip()
            raw = raw or path.stem
            canonical = _canonical_name(raw, aliases)

            if canonical in found:
                notes.append(
                    f"司机「{canonical}」存在多个源文件，已采用 {found[canonical].path.name}，"
                    f"忽略 {path.name}"
                )
                continue
            found[canonical] = DriverSource(name=canonical, path=path, raw_name=raw)

    if not found:
        raise DiscoveryError(
            f"未在 {directory} 找到匹配 {patterns} 的司机报表（已排除 {excludes}）"
        )

    order = list(cfg.get("source.driver_order") or [])
    drivers = sorted(
        found.values(),
        key=lambda d: (order.index(d.name) if d.name in order else len(order), d.name),
    )
    return drivers, notes


def detect_sheet(path: Path, cfg: Config, period: Dict[str, int]) -> SheetHit:
    """定位某个月份对应的工作表名；不存在时给出原因。"""
    wanted = cfg.sheet_name(period)
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 源文件可能被占用/损坏
        return SheetHit(None, f"源文件无法打开（{type(exc).__name__}: {exc}）")
    try:
        names = workbook.sheetnames
    finally:
        workbook.close()

    if wanted in names:
        return SheetHit(wanted)

    # 宽松回退：允许「2026.8」「2026.08」「2026-8」等写法
    year, month = period["year"], period["month"]
    candidates = {
        f"{year}.{month}", f"{year}.{month:02d}", f"{year}-{month}", f"{year}-{month:02d}",
        f"{year}/{month}", f"{year}年{month}月", f"{month}月",
    }
    for name in names:
        if name.strip() in candidates:
            return SheetHit(name, f"按回退规则匹配到工作表「{name}」")
    return SheetHit(None, f"无「{wanted}」工作表（现有：{'、'.join(names) or '空'}）")


# ------------------------------------------------------------ 旧报表清理 ----
@dataclass
class CleanupResult:
    removed: List[Tuple[str, str]]  # (原路径, 去向)
    mode: str


def find_stale_outputs(cfg: Config) -> List[Path]:
    """按配置的 glob 找出本次将被覆盖的旧报表。"""
    if not cfg.get("output.overwrite.enabled", True):
        return []
    directory = cfg.output_dir
    if not directory.exists():
        return []
    stale: List[Path] = []
    for pattern in cfg.match_globs():
        for path in directory.glob(pattern):
            if path.is_file() and path not in stale:
                stale.append(path)
    return stale


def cleanup_stale_outputs(cfg: Config, dry_run: bool = False) -> CleanupResult:
    """重生成前删除旧报表：recycle 模式移入回收目录，delete 模式直接删除。"""
    stale = find_stale_outputs(cfg)
    mode = cfg.get("output.overwrite.mode", "recycle")
    if dry_run or not stale:
        return CleanupResult([(str(p), "dry-run" if dry_run else "无需清理") for p in stale], mode)

    archive_root = cfg.output_dir / cfg.get("output.overwrite.recycle_dir", ".recycle")
    stamp = now_text().replace(":", "").replace(" ", "_")
    removed: List[Tuple[str, str]] = []

    if mode == "delete":
        for path in stale:
            path.unlink()
            removed.append((str(path), "已删除"))
        return CleanupResult(removed, mode)

    archive_dir = archive_root / stamp
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in stale:
        target = archive_dir / path.name
        if target.exists():
            target = archive_dir / f"{path.stem}_{stamp}{path.suffix}"
        shutil.move(str(path), str(target))
        removed.append((str(path), str(target)))
    return CleanupResult(removed, mode)


def cleanup_summary_text(result: CleanupResult) -> str:
    if not result.removed:
        return "重生成前检查：未发现同名旧报表，无需清理"
    lines = [f"重生成前检查：发现 {len(result.removed)} 个旧报表，已按 {result.mode} 模式处理（避免重复数据）"]
    for src, dst in result.removed:
        lines.append(f"  - {Path(src).name} → {dst}")
    return "\n".join(lines)


def period_label(cfg: Config, period: Dict[str, int]) -> str:
    return period_text(period)
