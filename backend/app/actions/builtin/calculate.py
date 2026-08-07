"""calculate — evaluate an arithmetic expression exactly.

Why a built-in rather than a connector: this is the one capability that
genuinely cannot be an HTTP call or an MCP server. Everything else new
should be config (see backend/app/tools/http_tool_store.py).

Why at all: asked what $10,000 at 8% compounded monthly is worth after 7
years, a run answered $17,470.60. The formula, the substitutions and the
working were all correct — only the final arithmetic was wrong. The real
value is $17,474.22. A $3.62 error is harmless; the same confident
mental arithmetic inside a financial model, a pricing sheet or a
valuation is not, and nothing in the output tells the reader which they
got. Models are good at choosing the formula and bad at evaluating it,
so give them somewhere to do the evaluation.

Deliberately NOT a general Python eval. It parses the expression to an
AST and walks it, permitting only arithmetic and a small set of maths
functions. Names, attributes, calls to anything unlisted, comprehensions
and imports are rejected — so a prompt-injected `__import__('os')` is a
parse-level refusal, not a sandbox that has to hold.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec

# Guard against a model asking for 9**9**9 and hanging the process.
MAX_EXPONENT = 1000
MAX_EXPR_CHARS = 500

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# Maths only. No builtins, no attribute access, nothing that touches the
# process.
_FUNCTIONS: Dict[str, Any] = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "pow": pow, "sqrt": math.sqrt, "exp": math.exp, "log": math.log,
    "log2": math.log2, "log10": math.log10, "floor": math.floor,
    "ceil": math.ceil, "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
_CONSTANTS: Dict[str, float] = {"pi": math.pi, "e": math.e}


class CalculationError(Exception):
    pass


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise CalculationError(f"only numbers are allowed, got {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalculationError(f"unsupported operator {type(node.op).__name__}")
        left, right = _eval(node.left), _eval(node.right)
        if op is operator.pow and isinstance(right, (int, float)) and right > MAX_EXPONENT:
            raise CalculationError(f"exponent {right} too large (max {MAX_EXPONENT})")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise CalculationError(f"unsupported unary operator {type(node.op).__name__}")
        return op(_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            name = getattr(node.func, "id", type(node.func).__name__)
            raise CalculationError(f"function {name!r} is not allowed")
        if node.keywords:
            raise CalculationError("keyword arguments are not supported")
        return _FUNCTIONS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalculationError(f"unknown name {node.id!r} — use a number")
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]
    raise CalculationError(f"unsupported expression element {type(node).__name__}")


def evaluate(expression: str) -> float:
    """Evaluate `expression` or raise CalculationError. Public so tests
    and other callers can use it without going through the action."""
    expr = (expression or "").strip()
    if not expr:
        raise CalculationError("empty expression")
    if len(expr) > MAX_EXPR_CHARS:
        raise CalculationError(f"expression too long (max {MAX_EXPR_CHARS} chars)")
    # '^' means XOR in Python but power to everyone else; a model writing
    # 1.0067^84 means exponentiation.
    expr = expr.replace("^", "**")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"could not parse {expr!r}: {exc.msg}") from exc
    try:
        return _eval(tree)
    except CalculationError:
        raise
    except ZeroDivisionError as exc:
        raise CalculationError("division by zero") from exc
    except (OverflowError, ValueError) as exc:
        raise CalculationError(str(exc)) from exc


def _handler(args: Dict[str, Any]) -> str:
    expr = str(args.get("expression") or "")
    try:
        result = evaluate(expr)
    except CalculationError as exc:
        return f"(calculate failed: {exc})"
    # Show full precision alongside a rounded figure: the caller usually
    # wants 2dp for money, but silently rounding hides compounding error
    # when a result feeds into the next step.
    if isinstance(result, float):
        return f"{expr} = {result!r}  (rounded: {round(result, 4)})"
    return f"{expr} = {result}"


SPEC = ActionSpec(
    name="calculate",
    description=(
        "Evaluate an arithmetic expression EXACTLY. Use this for EVERY "
        "number you report — compound interest, growth rates, totals, "
        "percentages, unit conversions — instead of working it out in "
        "your head, which produces confidently wrong figures. Supports "
        "+ - * / // % ** and sqrt, exp, log, log10, floor, ceil, abs, "
        "round, min, max, sum, plus pi and e. Example expression: "
        "10000*(1+0.08/12)**(12*7)"
    ),
    parameters=[
        {
            "name": "expression",
            "type": "string",
            "description": (
                "The arithmetic to evaluate, e.g. '10000*(1+0.08/12)**(12*7)'. "
                "Numbers only — no variable names."
            ),
            "required": True,
        },
    ],
    handler=_handler,
    preview=lambda args: f"Calculate {str(args.get('expression') or '')[:80]}",
    mutating=False,
    planner_excluded=False,
    capability="math.evaluate",
)
