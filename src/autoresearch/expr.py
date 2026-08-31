"""A small, total expression language for goal objectives and derived constants.

Why this exists rather than `eval`: a domain's `goal.yaml` is configuration, and
configuration that can execute arbitrary Python is not configuration. But the
thing being replaced is worse than either -- in the harness this core is derived
from, `T`, `Q` and `break-even` were *prose in a markdown table*, parsed by
regex, and the parser understood one of the two table forms actually in use. The
result was a board policed against a superseded `T` and a retired break-even
rate for as long as nobody looked (H102), and a tool that printed "4 constants
rows are unpoliced" on every push while letting a retired number from exactly
those rows through, because the warning read as noise (H138).

So: derived values are **expressions over named metrics**, evaluated on demand
from the one place the metric is measured. There is no stored copy to go stale,
which is the whole point.

Supported: arithmetic, comparison, boolean, ternary, and a fixed function set.
Anything else -- calls to unknown names, attribute access, subscripting,
comprehensions, lambdas -- is refused at parse time with the offending
construct named.
"""
import ast
import math
import operator

from .errors import GoalError

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_COMPARE = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}
FUNCTIONS = {
    "round": round, "abs": abs, "min": min, "max": max,
    "int": int, "float": float,
    "ln": math.log, "log": math.log10, "log2": math.log2,
    "exp": math.exp, "sqrt": math.sqrt,
    "floor": math.floor, "ceil": math.ceil,
}


def names(source: str) -> set[str]:
    """Every free name an expression reads. Used to check a goal is well-formed
    *before* anything is measured -- an objective naming a metric that no domain
    command produces should fail at config load, not at the first iteration."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise GoalError(f"cannot parse expression {source!r}: {exc}") from exc
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id not in FUNCTIONS}


def evaluate(source: str, namespace: dict):
    """Evaluate `source` against `namespace`. Raises GoalError, never NameError."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise GoalError(f"cannot parse expression {source!r}: {exc}") from exc
    return _eval(tree.body, namespace, source)


def _eval(node, ns, source):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool, str)) or node.value is None:
            return node.value
        raise GoalError(f"unsupported constant in {source!r}")

    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        raise GoalError(
            f"expression {source!r} reads undefined name {node.id!r}; "
            f"defined: {sorted(ns)}")

    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise GoalError(f"unsupported operator {type(node.op).__name__} in {source!r}")
        try:
            return op(_eval(node.left, ns, source), _eval(node.right, ns, source))
        except ZeroDivisionError as exc:
            raise GoalError(f"division by zero evaluating {source!r}") from exc

    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise GoalError(f"unsupported unary op in {source!r}")
        return op(_eval(node.operand, ns, source))

    if isinstance(node, ast.BoolOp):
        values = [_eval(v, ns, source) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    if isinstance(node, ast.Compare):
        left = _eval(node.left, ns, source)
        for op_node, right_node in zip(node.ops, node.comparators):
            op = _COMPARE.get(type(op_node))
            if op is None:
                raise GoalError(f"unsupported comparison in {source!r}")
            right = _eval(right_node, ns, source)
            if not op(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        return (_eval(node.body, ns, source) if _eval(node.test, ns, source)
                else _eval(node.orelse, ns, source))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            shown = getattr(node.func, "id", type(node.func).__name__)
            raise GoalError(
                f"expression {source!r} calls {shown!r}, which is not one of "
                f"{sorted(FUNCTIONS)}")
        if node.keywords:
            raise GoalError(f"keyword arguments are not supported in {source!r}")
        return FUNCTIONS[node.func.id](*[_eval(a, ns, source) for a in node.args])

    raise GoalError(
        f"unsupported construct {type(node).__name__} in expression {source!r}")
