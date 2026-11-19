#!/usr/bin/env python3
"""计薪规则 DSL 与受限表达式求值器。

设计目标（对应"通用 / 可开源 / 每位司机算法不同"）：
  1. 规则是数据（JSON），不是代码；仓库内不含任何真实司机姓名与金额。
  2. 历史工资表里的 Excel 公式可平移为本 DSL，单元格引用由 field_map 映射到标准字段。
  3. 绝不 eval。使用 Python `ast` + 白名单节点求值，禁止属性访问、下标、lambda、
     推导式、幂运算等一切可执行构造；函数名同样白名单。
  4. 金额一律 Decimal，ROUND_HALF_UP 到配置精度，避免浮点误差导致核对假阳性。

支持的规则 op：
  const    {"op":"const","value":150}
  tier     {"op":"tier","field":"km","brackets":[{"lte":60,"value":130},...],"default":200}
  lookup   {"op":"lookup","key":"route","table":"route_price","column":1,"default":0}
  formula  {"op":"formula","expr":"IF(km<=60,130,150)","cell_map":{"G2":"km"}}
  sum      {"op":"sum","items":[...]}

formula 支持的函数：IF / ROUNDDOWN / ROUND / ROUNDUP / INT / ABS / MIN / MAX /
SUM / LOOKUP。比较符兼容 Excel 写法（=、<>）。范围引用（G2:G12）不支持，
合计由 compute 独立累加，不进入逐行公式。
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP

D = Decimal


class RuleError(ValueError):
    """规则缺失、语法非法或引用了不允许的构造。一律中断，绝不猜值。"""


def dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise RuleError("布尔值不能当金额")
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").replace("¥", "").strip())
    except Exception as exc:
        raise RuleError(f"无法转为数值: {value!r}") from exc


def quantize(value: Decimal, precision: int = 2) -> Decimal:
    q = Decimal(1).scaleb(-int(precision))
    return value.quantize(q, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Excel 公式 -> 安全 Python 表达式
# --------------------------------------------------------------------------
_CELL_TOKEN = re.compile(r"\$?([A-Z]{1,3})\$?([0-9]{1,7})\b")


def translate_excel(expr: str, cell_map: dict | None = None) -> str:
    """把 Excel 逐行公式转为可被 ast 解析的表达式。

    cell_map: {"G2": "km", "$J2": "route"} —— 只映射逐行单元格，范围引用会被拒绝。
    """
    s = (expr or "").strip()
    if not s:
        raise RuleError("空表达式")
    if s.startswith("="):
        s = s[1:]
    if re.search(r"\$?[A-Z]{1,3}\$?\d+\s*:\s*\$?[A-Z]{1,3}\$?\d+", s, re.I):
        raise RuleError("不支持单元格范围引用；合计由 compute 独立累加")
    if "'!" in s or "!" in s:
        raise RuleError("不支持跨表引用；请把价目表放进 rules.tables 并用 lookup")

    cell_map = cell_map or {}
    # 长引用优先，避免 G2 误伤 G20
    for ref in sorted(cell_map, key=len, reverse=True):
        pattern = r"\$?" + re.escape(ref.lstrip("$")) + r"\b"
        s = re.sub(pattern, cell_map[ref], s, flags=re.I)
    # 转换后仍残留的单元格引用 -> 明确报错，不静默当变量
    leftover = _CELL_TOKEN.search(s)
    if leftover:
        raise RuleError(f"未映射的单元格引用 {leftover.group(0)}；请补 cell_map")

    s = s.replace("<>", "!=")
    s = re.sub(r"(?<![<>!=])=(?!=)", "==", s)
    return s


_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.IfExp, ast.BoolOp, ast.keyword,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.UAdd, ast.USub,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.And, ast.Or, ast.Not,
)


def _fn_rounddown(x, n=0):
    q = Decimal(1).scaleb(-int(n))
    return (dec(x) / q).to_integral_value(rounding=ROUND_DOWN) * q


def _fn_roundup(x, n=0):
    q = Decimal(1).scaleb(-int(n))
    return (dec(x) / q).to_integral_value(rounding=ROUND_UP) * q


def _fn_round(x, n=0):
    q = Decimal(1).scaleb(-int(n))
    return dec(x).quantize(q, rounding=ROUND_HALF_UP)


def _fn_int(x):
    return dec(x).to_integral_value(rounding=ROUND_DOWN)


def _fn_sum(*args):
    return sum((dec(a) for a in args), Decimal(0))


def _fn_min(*args):
    return min(dec(a) for a in args)


def _fn_max(*args):
    return max(dec(a) for a in args)


def _fn_abs(x):
    return abs(dec(x))


_FUNCS = {
    "ROUNDDOWN": _fn_rounddown, "ROUNDUP": _fn_roundup, "ROUND": _fn_round,
    "INT": _fn_int, "ABS": _fn_abs, "SUM": _fn_sum, "MIN": _fn_min, "MAX": _fn_max,
}


def _ev(node, variables: dict, tables: dict):
    if not isinstance(node, _ALLOWED_NODES):
        raise RuleError(f"表达式含禁用语法: {type(node).__name__}")
    if isinstance(node, ast.Expression):
        return _ev(node.body, variables, tables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float, str)):
            raise RuleError("只允许数字或字符串常量")
        return dec(node.value) if isinstance(node.value, (int, float)) else node.value
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise RuleError(f"未知变量 {node.id}")
        v = variables[node.id]
        return dec(v) if not isinstance(v, str) else v
    if isinstance(node, ast.UnaryOp):
        v = _ev(node.operand, variables, tables)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _ev(node.left, variables, tables), _ev(node.right, variables, tables)
        a, b = dec(a), dec(b)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            if b == 0:
                raise RuleError("除数为 0")
            return a / b
        if isinstance(node.op, ast.Mod):
            return a % b
        raise RuleError("不支持的运算符")
    if isinstance(node, ast.Compare):
        left = _ev(node.left, variables, tables)
        left = dec(left) if not isinstance(left, str) else left
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, variables, tables)
            right = dec(right) if not isinstance(right, str) else right
            ok = {
                ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
            }.get(type(op), lambda: False)()
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _ev(node.body if _ev(node.test, variables, tables) else node.orelse,
                   variables, tables)
    if isinstance(node, ast.BoolOp):
        vals = [_ev(v, variables, tables) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise RuleError("只允许调用白名单函数")
        name = node.func.id.upper()
        if node.keywords:
            raise RuleError("不支持关键字参数")
        if name == "IF":
            if len(node.args) != 3:
                raise RuleError("IF 需要 3 个参数")
            cond = _ev(node.args[0], variables, tables)
            # 惰性求值：未命中的分支不计算，避免无害分支里的除零误报
            return _ev(node.args[1] if cond else node.args[2], variables, tables)
        if name == "LOOKUP":
            if len(node.args) != 2:
                raise RuleError("LOOKUP(key, table_name)")
            key = str(_ev(node.args[0], variables, tables))
            tname_node = node.args[1]
            if not isinstance(tname_node, ast.Constant):
                raise RuleError("LOOKUP 表名必须是字符串常量")
            table = tables.get(str(tname_node.value), {})
            if key not in table:
                raise RuleError(f"价目表 {tname_node.value!r} 中找不到 {key!r}")
            return dec(table[key])
        if name not in _FUNCS:
            raise RuleError(f"禁用函数 {name}")
        return _FUNCS[name](*[_ev(a, variables, tables) for a in node.args])
    raise RuleError(f"无法求值节点 {type(node).__name__}")


def formula_variables(trip: dict) -> dict:
    """构造公式变量：只收数值与字符串，排除 bool 等控制字段。"""
    out = {}
    for k, v in trip.items():
        if k.startswith("_") or isinstance(v, bool) or v is None:
            continue
        if isinstance(v, str):
            # 明细里数值字段常以字符串保存（km="40"），数字串要转成 Decimal，
            # 否则 "40" <= 60 会抛类型错误；路线/日期等仍保留为字符串。
            s = v.replace(",", "").replace("¥", "").strip()
            out[k] = dec(s) if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s) else v
        elif isinstance(v, (int, float, Decimal)):
            out[k] = dec(v)
    return out


def safe_eval(expr: str, variables: dict, tables: dict | None = None,
              cell_map: dict | None = None) -> Decimal:
    """求值一条逐行公式，返回 Decimal。任何异常都是 RuleError，绝不返回猜测值。"""
    try:
        tree = ast.parse(translate_excel(expr, cell_map), mode="eval")
        value = _ev(tree, variables, tables or {})
        return dec(value)
    except RuleError:
        raise
    except Exception as exc:
        raise RuleError(f"公式求值失败 {expr!r}: {exc}") from exc


# --------------------------------------------------------------------------
# 结构化规则
# --------------------------------------------------------------------------
def evaluate(rule: dict, trip: dict, tables: dict | None = None,
             precision: int = 2) -> Decimal:
    """对一条运输明细求值，返回该行工资（未取整，取整在合计层统一做）。"""
    if not isinstance(rule, dict):
        raise RuleError("规则必须是对象")
    op = rule.get("op")
    tables = tables or {}
    if op == "const":
        return dec(rule["value"])
    if op == "tier":
        field = rule.get("field", "km")
        value = dec(trip.get(field, 0))
        for b in rule.get("brackets", []):
            if value <= dec(b["lte"]):
                return dec(b["value"])
        if "default" not in rule:
            raise RuleError(f"tier 未命中且无 default (field={field}, value={value})")
        return dec(rule["default"])
    if op == "lookup":
        key = str(trip.get(rule.get("key", "route"), ""))
        table = tables.get(rule["table"])
        if table is None:
            raise RuleError(f"缺少价目表 {rule['table']}")
        if key not in table:
            if "default" in rule:
                return dec(rule["default"])
            raise RuleError(f"价目表 {rule['table']} 中找不到 {key!r}")
        return dec(table[key])
    if op == "formula":
        return safe_eval(rule["expr"], formula_variables(trip), tables,
                         rule.get("cell_map"))
    if op == "sum":
        return sum((evaluate(i, trip, tables, precision) for i in rule["items"]),
                   Decimal(0))
    raise RuleError(f"未知规则 op: {op!r}")


def validate_rules(doc: dict) -> list[str]:
    """返回问题列表（空 = 通过）。规则未确认一律视为阻断项，由 verify 判定。"""
    problems = []
    if not isinstance(doc, dict) or "drivers" not in doc:
        return ["rules 缺少 drivers"]
    for driver_id, spec in doc.get("drivers", {}).items():
        if not isinstance(spec, dict):
            problems.append(f"{driver_id}: 规则不是对象")
            continue
        if "wage" not in spec:
            problems.append(f"{driver_id}: 缺少 wage 规则")
            continue
        if not spec.get("confirmed"):
            problems.append(f"{driver_id}: 规则未经发起人确认（confirmed=false）")
        try:
            evaluate(spec["wage"], {"km": 100, "route": "示例", "trip_no": 1},
                     doc.get("tables", {}))
        except RuleError as exc:
            problems.append(f"{driver_id}: 规则自测失败 - {exc}")
        except Exception as exc:
            problems.append(f"{driver_id}: 规则自测异常 - {exc}")
    return problems
