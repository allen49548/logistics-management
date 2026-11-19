"""配置化公式求值：只允许四则运算、括号、少量白名单函数。

净利润公式、环比增减额/比例公式都写在 config/report.json 里，由本模块解释执行，
因此调整口径只改配置，不改代码。
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Dict

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": lambda value, digits=0: round(value, int(digits)),
}


class ExpressionError(ValueError):
    """公式非法或无法求值。"""


def evaluate(expression: str, variables: Dict[str, Any]) -> float:
    """按表达式与变量求值；变量值为 None 视为 0。"""
    if not expression or not str(expression).strip():
        raise ExpressionError("公式为空")
    try:
        tree = ast.parse(str(expression), mode="eval")
    except SyntaxError as exc:  # pragma: no cover - 配置写错时给清晰报错
        raise ExpressionError(f"公式语法错误：{expression!r}") from exc
    return float(_eval(tree.body, variables))


def try_evaluate(expression: str, variables: Dict[str, Any]) -> float | None:
    """求值失败（如除零）返回 None，用于环比比例等可空指标。"""
    try:
        return evaluate(expression, variables)
    except (ExpressionError, ZeroDivisionError):
        return None


def _eval(node: ast.AST, variables: Dict[str, Any]) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExpressionError(f"公式只允许数字常量，发现 {node.value!r}")
        return float(node.value)

    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ExpressionError(f"公式引用了未定义变量：{node.id}")
        value = variables[node.id]
        return 0.0 if value is None else float(value)

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"不支持的运算符：{type(node.op).__name__}")
        left = _eval(node.left, variables)
        right = _eval(node.right, variables)
        if isinstance(node.op, (ast.Div, ast.Mod, ast.FloorDiv)) and right == 0:
            raise ZeroDivisionError("公式除数为 0")
        return float(op(left, right))

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"不支持的一元运算符：{type(node.op).__name__}")
        return float(op(_eval(node.operand, variables)))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ExpressionError("公式只允许 min/max/abs/round 四个函数")
        if node.keywords:
            raise ExpressionError("函数不支持关键字参数")
        return float(_FUNCS[node.func.id](*[_eval(arg, variables) for arg in node.args]))

    raise ExpressionError(f"公式中出现不允许的语法：{type(node).__name__}")
