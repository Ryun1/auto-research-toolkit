"""The expression language must be total and must refuse everything else.

It replaces constants that were prose in a markdown table, parsed by a regex
that understood one of the two table forms in use (H102). The whole benefit is
that a derived value cannot go stale, so the evaluator has to be trustworthy.
"""
import pytest

from autoresearch import expr
from autoresearch.errors import GoalError


def test_arithmetic_and_functions():
    ns = {"T": 907000.0, "Q": 1267}
    assert expr.evaluate("round(T) * Q", ns) == 907000 * 1267
    assert expr.evaluate("T / Q", ns) == pytest.approx(907000 / 1267)
    assert expr.evaluate("ln(exp(2))", {}) == pytest.approx(2.0)
    assert expr.evaluate("max(1, 2, 3)", {}) == 3
    assert expr.evaluate("2 if T > 0 else 3", ns) == 2
    assert expr.evaluate("1 < 2 < 3", {}) is True


def test_names_ignores_builtin_functions():
    assert expr.names("round(T) * Q") == {"T", "Q"}
    assert expr.names("ln(x) + exp(y)") == {"x", "y"}


@pytest.mark.parametrize("source", [
    "__import__('os').system('echo pwned')",
    "open('/etc/passwd')",
    "(lambda: 1)()",
    "[x for x in range(3)]",
    "T.__class__",
    "T[0]",
])
def test_refuses_everything_that_is_not_arithmetic(source):
    """The negative test invariant 3 requires: this guard must refuse something."""
    with pytest.raises(GoalError):
        expr.evaluate(source, {"T": 1})


def test_undefined_name_names_what_was_available():
    with pytest.raises(GoalError) as exc:
        expr.evaluate("T * Z", {"T": 1})
    assert "Z" in str(exc.value) and "['T']" in str(exc.value)


def test_division_by_zero_is_a_goal_error_not_a_crash():
    with pytest.raises(GoalError, match="division by zero"):
        expr.evaluate("T / Q", {"T": 1, "Q": 0})
