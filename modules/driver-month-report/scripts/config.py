"""配置装载与校验：report.json（运行参数）+ item_rules.json（项目识别规则）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from common import deep_merge, is_adjacent, parse_period, period_ctx, period_text


class ConfigError(ValueError):
    """配置缺失或自相矛盾。"""


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = REPO_ROOT / "config" / "report.json"
DEFAULT_RULES = REPO_ROOT / "config" / "item_rules.json"


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 非法：{path}（{exc}）") from exc


class Config:
    """把两份配置合并成一个只读视图，并向调用方暴露语义化访问器。"""

    def __init__(self, report: Dict[str, Any], rules: Dict[str, Any], source: Path):
        self._report = report
        self._rules = rules
        self.source_path = source
        self.messages: Dict[str, str] = report.get("messages", {})
        self.status: Dict[str, str] = report.get("status", {})
        self.highlight = report.get("highlight", {})
        self.number_formats = report.get("number_formats", {"money": "#,##0.00", "percent": "0.00%", "int": "0"})

    # ------------------------------------------------------------ 构造 ----
    @classmethod
    def load(
        cls,
        report_path: Optional[Path] = None,
        rules_path: Optional[Path] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> "Config":
        report = _load_json(Path(report_path or DEFAULT_REPORT))
        rules = _load_json(Path(rules_path or DEFAULT_RULES))
        if overrides:
            report = deep_merge(report, overrides)
        cfg = cls(report, rules, Path(report_path or DEFAULT_REPORT))
        cfg.validate()
        return cfg

    # ------------------------------------------------------------ 校验 ----
    def validate(self) -> None:
        cur = self.current_period
        prev = self.previous_period
        if self.get("report.comparison_only_previous", True) and not is_adjacent(cur, prev):
            raise ConfigError(
                "环比配置冲突：report.previous_period 必须是 current_period 的上一个月"
                f"（当前 {period_text(cur)} vs {period_text(prev)}）。"
                "如确需跨月比较，请把 report.comparison_only_previous 设为 false。"
            )

        keys = [item["key"] for item in self.items]
        if len(keys) != len(set(keys)):
            raise ConfigError(f"item_rules.items 存在重复 key：{keys}")

        roles = {cat.get("role") for cat in self.categories.values()}
        for var, role in self.get("calculation.variables", {}).items():
            if role not in roles:
                raise ConfigError(f"calculation.variables.{var} 指向未知角色：{role}")

        for item in self.items:
            cat = item.get("category")
            if cat not in self.categories:
                raise ConfigError(f"项目 {item['key']} 的类别 {cat!r} 未在 categories 中定义")
            if not item.get("aliases"):
                raise ConfigError(f"项目 {item['key']} 缺少 aliases")
            owner = item.get("suppressed_by")
            if owner and owner not in keys:
                raise ConfigError(f"项目 {item['key']} 的 suppressed_by 指向未知项目：{owner}")

        for item in self.items:
            for covered in item.get("covers", []):
                if covered not in keys:
                    raise ConfigError(f"项目 {item['key']} 的 covers 指向未知项目：{covered}")

        for col in self.get("columns", []):
            if "key" not in col or "title" not in col:
                raise ConfigError("columns 每项都必须包含 key 与 title")

    # ---------------------------------------------------------- 访问器 ----
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._report
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def current_period(self) -> Dict[str, int]:
        return parse_period(self.get("report.current_period"))

    @property
    def previous_period(self) -> Dict[str, int]:
        return parse_period(self.get("report.previous_period"))

    @property
    def title(self) -> str:
        return str(self.get("report.title_template", "{year}年{month}月汇总")).format(**self.current_period)

    @property
    def unit(self) -> str:
        return self.get("report.unit", "元")

    @property
    def driver_dir(self) -> Path:
        return Path(self.get("source.driver_dir", "")).expanduser()

    @property
    def output_dir(self) -> Path:
        raw = self.get("output.dir") or ""
        return Path(raw).expanduser() if raw else self.driver_dir

    @property
    def output_filename(self) -> str:
        return str(self.get("output.filename_template", "{title}.xlsx")).format(title=self.title)

    @property
    def items(self) -> List[Dict[str, Any]]:
        return [item for item in self._rules.get("items", []) if not item.get("disabled")]

    @property
    def all_items(self) -> List[Dict[str, Any]]:
        return list(self._rules.get("items", []))

    @property
    def categories(self) -> Dict[str, Dict[str, Any]]:
        return self._rules.get("categories", {})

    def item(self, key: str) -> Dict[str, Any]:
        for item in self.all_items:
            if item["key"] == key:
                return item
        raise ConfigError(f"未知项目 key：{key}")

    def category_role(self, category: str) -> str:
        return (self.categories.get(category) or {}).get("role", "other")

    def resolve_order(self, item: Dict[str, Any]) -> List[str]:
        return list(item.get("resolve_order") or self.get("resolution.default_order", ["label"]))

    def sheet_name(self, period: Dict[str, int]) -> str:
        return period_ctx(period, str(self.get("source.sheet_name_template", "{year}.{month}")))

    def sheet_title(self, key: str, period: Dict[str, int]) -> str:
        template = self.get(f"output.sheets.{key}", key)
        return str(template).format(**period)

    def strategy_title(self, strategy: str) -> str:
        return str(self.get(f"resolution.strategy_titles.{strategy}", strategy))

    def msg(self, key: str, **kwargs: Any) -> str:
        template = self.messages.get(key)
        if template is None:
            return key
        try:
            return str(template).format(**kwargs)
        except (KeyError, IndexError):
            return str(template)

    def match_globs(self) -> List[str]:
        ctx = dict(self.current_period)
        ctx["title"] = self.title
        ctx["prev_year"] = self.previous_period["year"]
        ctx["prev_month"] = self.previous_period["month"]
        return [str(g).format(**ctx) for g in self.get("output.overwrite.match_globs", [])]

    def money(self, value: Optional[float]) -> str:
        if value is None:
            return "—"
        return f"¥{value:,.{int(self.get('calculation.round_digits', 2))}f}"
