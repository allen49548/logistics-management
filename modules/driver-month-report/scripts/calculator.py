"""取数结果 → 统一口径的每月净利润 → 环比。

所有"选哪个值""怎么算"的决策都读配置：
* 取数优先级 ``item.resolve_order``；
* 重叠科目（如奖金是否已含在工资里）由 ``overlap`` 段决定；
* 净利润公式 ``calculation.net_profit_expression``；
* 环比公式 ``mom.*_expression``；
* 可比性阈值 ``comparability``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import safe_ratio
from config import Config
from extractor import MonthExtraction, RawMatch
from expr import ExpressionError, evaluate, try_evaluate


@dataclass
class ResolvedItem:
    key: str
    name: str
    category: str
    role: str
    value: Optional[float] = None
    strategy: str = ""
    cell: str = ""
    label: str = ""
    header: str = ""
    note: str = ""
    alias_rank: int = 99
    excluded: bool = False
    suppressed_by: str = ""
    status: str = ""


@dataclass
class MonthResult:
    period: Dict[str, int]
    period_text: str
    sheet: Optional[str] = None
    items: Dict[str, ResolvedItem] = field(default_factory=dict)
    role_totals: Dict[str, Optional[float]] = field(default_factory=dict)
    net_profit: Optional[float] = None
    source_net: Optional[float] = None
    check_diff: Optional[float] = None
    notes: List[str] = field(default_factory=list)
    income_derived: bool = False

    def value(self, key: str) -> Optional[float]:
        item = self.items.get(key)
        return item.value if item else None


@dataclass
class DriverResult:
    name: str
    path: Path
    current: MonthResult
    previous: MonthResult
    delta: Optional[float] = None
    ratio: Optional[float] = None
    trend: str = ""
    comparable: bool = False
    incomparable_reason: str = ""
    status: str = ""
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------- 解析取值 ----
def _dedupe_pairs(pairs: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    seen, out = set(), []
    for label, value in pairs:
        key = (label, round(value, 6))
        if key not in seen:
            seen.add(key)
            out.append((label, value))
    return out


def _tolerance(cfg: Config) -> float:
    return float(cfg.get("resolution.agreement_tolerance_pct", 0.005))


def _close(a: float, b: float, tol_pct: float) -> bool:
    if a == b:
        return True
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base <= tol_pct


def resolve_items(extraction: MonthExtraction, cfg: Config) -> Tuple[Dict[str, ResolvedItem], List[str]]:
    notes: List[str] = []
    tol = _tolerance(cfg)
    resolved: Dict[str, ResolvedItem] = {}

    for item_cfg in cfg.items:
        key = item_cfg["key"]
        candidates = extraction.matches.get(key, [])
        entry = ResolvedItem(
            key=key,
            name=item_cfg.get("name", key),
            category=item_cfg["category"],
            role=cfg.category_role(item_cfg["category"]),
            suppressed_by=item_cfg.get("suppressed_by", ""),
        )
        if entry.role == "check":
            entry.status = "仅校验，不计入合计"
        resolved[key] = entry
        if not candidates:
            continue

        by_strategy: Dict[str, List[RawMatch]] = {}
        for match in candidates:
            by_strategy.setdefault(match.strategy, []).append(match)

        order = cfg.resolve_order(item_cfg)
        chosen: Optional[RawMatch] = None
        for strategy in order:
            if strategy == "derive":
                continue  # derive 在 compute_month 中按公式补充
            pool = by_strategy.get(strategy)
            if not pool:
                continue
            if strategy == "label":
                pool = sorted(pool, key=lambda m: (m.alias_rank, -abs(m.value)))
                top_rank = pool[0].alias_rank
                same_rank = [m for m in pool if m.alias_rank == top_rank]
                distinct = {round(m.value, 6) for m in same_rank}
                if len(distinct) > 1:
                    pairs = "、".join(f"{m.label}={m.value:g}" for m in _dedupe_pairs([(m.label, m.value) for m in same_rank]))
                    notes.append(cfg.msg("label_conflict", item=entry.name, pairs=pairs, chosen=f"{same_rank[0].label}={same_rank[0].value:g}"))
                chosen = same_rank[0]
            else:
                chosen = pool[0]
            break

        if chosen is None:
            continue

        entry.value = chosen.value
        entry.strategy = chosen.strategy
        entry.cell = chosen.cell
        entry.label = chosen.label
        entry.header = chosen.header
        entry.note = chosen.note
        entry.alias_rank = chosen.alias_rank

        # 同一项目的不同取数策略互相校验
        other_values = {
            strategy: matches[0].value
            for strategy, matches in by_strategy.items()
            if strategy != chosen.strategy
        }
        for strategy, value in other_values.items():
            if not _close(value, chosen.value, tol):
                notes.append(
                    cfg.msg(
                        "strategy_conflict",
                        item=entry.name,
                        preferred=cfg.strategy_title(chosen.strategy),
                        other=cfg.strategy_title(strategy),
                        a=f"{chosen.value:g}",
                        b=f"{value:g}",
                        diff=f"{chosen.value - value:g}",
                    )
                )

        if chosen.strategy == "detail":
            template = "detail_used" if chosen.from_total_row else "detail_sum_used"
            notes.append("[来源] " + cfg.msg(template, item=entry.name, cell=chosen.cell, header=chosen.header))

        if chosen.note:
            notes.append(f"源表批注：{chosen.note}")
        if chosen.duplicates:
            notes.append(f"同值重复标签已合并（只取一个值）：{'、'.join(chosen.duplicates)}")

    return resolved, notes


# --------------------------------------------------------------- 重叠科目 ----
def _totals_for(items: Dict[str, ResolvedItem], excluded: set) -> Dict[str, Optional[float]]:
    totals: Dict[str, Optional[float]] = {}
    for role in ("income", "wage", "expense"):
        values = [
            item.value
            for item in items.values()
            if item.role == role and item.value is not None and item.key not in excluded
        ]
        totals[role] = float(sum(values)) if values else None
    return totals


def _decide_overlap(
    cfg: Config,
    items: Dict[str, ResolvedItem],
    source_net: Optional[float],
    income_known: Optional[float],
    income_detail: Optional[float],
) -> Tuple[set, List[str]]:
    """决定「已含于合计项」的子科目是否重复计入（如奖金是否已含在工资里）。

    判定顺序：源表净利润 → 运费明细交叉印证 → 配置 fallback。
    """
    notes: List[str] = []
    if not cfg.get("overlap.enabled", True):
        return set(), notes

    suppressible = [item for item in items.values() if item.suppressed_by and item.value is not None]
    if not suppressible:
        return set(), notes

    fallback = str(cfg.get("overlap.fallback", "exclude"))
    default_excluded = {item.key for item in suppressible} if fallback == "exclude" else set()

    if len(suppressible) > 4:  # 组合爆炸保护
        return default_excluded, notes

    options: List[set] = [set()]
    for item in suppressible:
        options = [opt | {item.key} for opt in options] + [opt for opt in options]

    best: Optional[Tuple[float, set]] = None
    for option in options:
        totals = _totals_for(items, option)
        if income_known is not None and totals["wage"] is not None:
            income = income_known
        elif income_detail is not None:
            income = income_detail
        else:
            income = None

        if source_net is not None and income is not None:
            net = _net_from_totals(cfg, income, totals)
            if net is None:
                continue
            error = abs(net - source_net)
            if best is None or error < best[0]:
                best = (error, option)

    chosen = default_excluded
    if best is not None and best[0] <= max(abs(source_net or 0) * 1e-6, 0.01):
        chosen = best[1]

    for item in suppressible:
        owner = items.get(item.suppressed_by)
        owner_name = owner.name if owner else item.suppressed_by
        if item.key in chosen:
            item.excluded = True
            item.status = "已含于合计项，不重复计入"
            notes.append(cfg.msg("overlap_excluded", item=item.name, owner=owner_name))
        else:
            item.status = "单独计入支出"
            notes.append(cfg.msg("overlap_included", item=item.name, owner=owner_name))
    return chosen, notes


def _net_from_totals(cfg: Config, income: Optional[float], totals: Dict[str, Optional[float]]) -> Optional[float]:
    roles = {"income": income, "wage": totals.get("wage"), "expense": totals.get("expense")}
    variables: Dict[str, Optional[float]] = {}
    for var, role in cfg.get("calculation.variables", {}).items():
        value = roles.get(role)
        if value is None:
            return None
        variables[var] = value
    try:
        return round(evaluate(cfg.get("calculation.net_profit_expression", ""), variables), int(cfg.get("calculation.round_digits", 2)))
    except (ExpressionError, ZeroDivisionError):
        return None


# --------------------------------------------------------------- 月度计算 ----
def compute_month(extraction: MonthExtraction, cfg: Config) -> MonthResult:
    result = MonthResult(period=extraction.period, period_text=extraction.period_text, sheet=extraction.sheet)
    result.notes.extend(extraction.anomalies)

    if extraction.sheet is None:
        return result

    items, notes = resolve_items(extraction, cfg)
    result.items = items
    result.notes.extend(notes)

    src_item = items.get(cfg.get("calculation.source_profit_item", "source_net_profit"))
    source_net = src_item.value if src_item and src_item.value is not None else None
    result.source_net = source_net

    # 收入：标签 / 明细
    income_item = next((i for i in items.values() if i.role == "income"), None)
    income_known = income_item.value if income_item and income_item.value is not None else None
    income_detail = None
    detail_matches = [m for m in extraction.matches.get(income_item.key, []) if m.strategy == "detail"] if income_item else []
    if detail_matches:
        income_detail = detail_matches[0].value

    excluded, overlap_notes = _decide_overlap(cfg, items, source_net, income_known, income_detail)
    result.notes.extend(overlap_notes)
    totals = _totals_for(items, excluded)

    # 收入缺失时按源表净利润倒算（配置里 resolve_order 允许 derive 才执行）
    if totals["income"] is None and income_item is not None:
        allow_derive = "derive" in cfg.resolve_order(cfg.item(income_item.key))
        if allow_derive and source_net is not None and totals["wage"] is not None and totals["expense"] is not None:
            derived = source_net + totals["wage"] + totals["expense"]
            totals["income"] = round(derived, 6)
            income_item.value = totals["income"]
            income_item.strategy = "derive"
            income_item.cell = income_item.cell or "—"
            result.income_derived = True
            result.notes.append(
                cfg.msg(
                    "derived_income",
                    src=f"{source_net:g}",
                    wage=f"{totals['wage']:g}",
                    expense=f"{totals['expense']:g}",
                    value=f"{totals['income']:g}",
                )
            )

    result.role_totals = totals

    if income_item is not None and income_item.value is None:
        result.notes.append(cfg.msg("missing_item", item=income_item.name, aliases="、".join(cfg.item(income_item.key).get("aliases", []))))
    wage_item = next((i for i in items.values() if i.role == "wage"), None)
    if wage_item is not None and wage_item.value is None:
        result.notes.append(cfg.msg("missing_item", item=wage_item.name, aliases="、".join(cfg.item(wage_item.key).get("aliases", []))))

    has_expense = totals["expense"] is not None
    if not has_expense:
        result.notes.append("未识别到任何支出科目，按统一口径无法扣除支出")

    if totals["income"] is None:
        if source_net is not None:
            result.notes.append(f"{cfg.msg('cannot_compute')}；源表净利润为 {source_net:g}")
        else:
            result.notes.append(cfg.msg("cannot_compute"))
        result.net_profit = None
    elif not has_expense:
        if source_net is not None:
            result.notes.append(f"源表净利润为 {source_net:g}，但缺支出科目，无法按统一口径复核")
        result.net_profit = None
    else:
        result.net_profit = _net_from_totals(cfg, totals["income"], totals)

    if result.net_profit is not None and source_net is not None:
        result.check_diff = round(result.net_profit - source_net, 2)
        if abs(result.check_diff) <= float(cfg.get("calculation.check_tolerance", 0.01)):
            result.notes.append(cfg.msg("profit_match", calc=f"{result.net_profit:g}"))
        else:
            result.notes.append(
                cfg.msg("profit_diff", calc=f"{result.net_profit:g}", src=f"{source_net:g}", diff=f"{result.check_diff:g}")
            )
    elif source_net is None:
        result.notes.append(cfg.msg("no_source_profit"))

    return result


# ------------------------------------------------------------------ 环比 ----
def compute_driver(
    name: str,
    path: Path,
    current: MonthResult,
    previous: MonthResult,
    cfg: Config,
) -> DriverResult:
    dr = DriverResult(name=name, path=path, current=current, previous=previous)
    dr.notes.extend(current.notes)
    dr.notes.extend([f"{cfg.previous_period['month']}月：{n}" for n in previous.notes])

    cur_net = current.net_profit
    prev_net = previous.net_profit

    reason = ""
    comparable = True
    if cur_net is None:
        comparable = False
        reason = f"{cfg.current_period['month']}月净利润无法计算"
    elif prev_net is None:
        comparable = False
        reason = cfg.msg("prev_missing", prev_month=cfg.previous_period["month"])
    elif cfg.get("comparability.enabled", True):
        ref_key = cfg.get("comparability.reference_item", "income_freight")
        secondary = cfg.get("comparability.secondary_item", "")
        min_ratio = float(cfg.get("comparability.min_ratio", 0.6))
        ratio_val, metric = None, ""
        cur_ref = current.value(ref_key) or (current.value(secondary) if secondary else None)
        prev_ref = previous.value(ref_key) or (previous.value(secondary) if secondary else None)
        metric = "运费" if current.value(ref_key) else "工资"
        ratio_val = safe_ratio(prev_ref, cur_ref)
        if ratio_val is not None and ratio_val < min_ratio:
            comparable = False
            reason = cfg.msg(
                "incomparable_partial",
                prev_month=cfg.previous_period["month"],
                month=cfg.current_period["month"],
                metric=metric,
                pct=f"{ratio_val * 100:.1f}%",
                min_pct=f"{min_ratio * 100:.0f}%",
            )

    if cur_net is not None and prev_net is not None:
        dr.delta = round(
            evaluate(cfg.get("mom.delta_expression", "current - previous"), {"current": cur_net, "previous": prev_net}),
            2,
        )
        if prev_net <= 0:
            # 基期为负或为零时比例没有经济含义，仍给出增减额但不出比例
            dr.ratio = None
            dr.notes.append(cfg.msg("ratio_guard", prev_month=cfg.previous_period["month"], prev=f"{prev_net:g}"))
        else:
            dr.ratio = try_evaluate(
                cfg.get("mom.ratio_expression", "(current - previous) / previous"),
                {"current": cur_net, "previous": prev_net},
            )
        if dr.delta > 0:
            dr.trend = "up"
        elif dr.delta < 0:
            dr.trend = "down"
        else:
            dr.trend = "flat"

    dr.comparable = comparable
    dr.incomparable_reason = reason
    # 上月/当月净利润缺失时，具体原因已在月级说明里写过；只有"样本不全"这类判断需要额外记一笔
    if not comparable and reason and prev_net is not None and cur_net is not None:
        dr.notes.append(reason)

    dr.status = _status(dr, cfg)
    return dr


def _status(dr: DriverResult, cfg: Config) -> str:
    st = cfg.status
    month = cfg.current_period["month"]
    if dr.current.sheet is None:
        return str(st.get("no_data", "无数据")).format(month=month)
    if dr.current.net_profit is None:
        return str(st.get("cannot_compute", "无法计算"))
    if dr.current.check_diff not in (None, 0) and abs(dr.current.check_diff) > float(cfg.get("calculation.check_tolerance", 0.01)):
        return str(st.get("differs", "与源表有差异"))
    if not dr.comparable:
        return str(st.get("incomparable", "环比不可比"))
    if dr.current.notes:
        return str(st.get("noted", "有备注"))
    return str(st.get("ok", "完整"))


def summarize(results: List[DriverResult], cfg: Config) -> Dict[str, Any]:
    cur = [r.current.net_profit for r in results if r.current.net_profit is not None]
    prev = [r.previous.net_profit for r in results if r.previous.net_profit is not None]
    comparable = [r for r in results if r.comparable and r.delta is not None]
    deltas = [r.delta for r in comparable if r.delta is not None]
    total_cur = float(sum(cur)) if cur else None
    total_prev = float(sum(prev)) if prev else None
    total_delta = float(sum(deltas)) if deltas else None
    # 环比比例以「可比司机」的上月净利润为分母，避免把不可比司机的基数混进来
    comparable_prev = [r.previous.net_profit for r in comparable if r.previous.net_profit is not None]
    comparable_cur = [r.current.net_profit for r in comparable if r.current.net_profit is not None]
    comp_prev_total = float(sum(comparable_prev)) if comparable_prev else None
    comp_cur_total = float(sum(comparable_cur)) if comparable_cur else None
    issues = sum(1 for r in results for _ in r.notes)

    def _sum_role(role: str) -> Optional[float]:
        values = [r.current.role_totals.get(role) for r in results]
        values = [v for v in values if v is not None]
        return float(sum(values)) if values else None

    from common import now_text, period_text

    return {
        "net_current": total_cur,
        "net_previous": total_prev,
        "delta": total_delta,
        "ratio": safe_ratio(total_delta, comp_prev_total) if (total_delta is not None and comp_prev_total) else None,
        "net_comparable_current": comp_cur_total,
        "net_comparable_previous": comp_prev_total,
        "income_total": _sum_role("income"),
        "wage_total": _sum_role("wage"),
        "expense_total": _sum_role("expense"),
        "comparable": len(comparable),
        "up": sum(1 for d in deltas if d > 0),
        "down": sum(1 for d in deltas if d < 0),
        "flat": sum(1 for d in deltas if d == 0),
        "incomparable": len(results) - len(comparable),
        "issues": issues,
        "drivers": len(results),
        "has_current": bool(cur),
        "has_previous": bool(prev),
        "generated_at": now_text(),
        "current_period_text": period_text(cfg.current_period),
    }
