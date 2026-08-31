"""One error type per refusal reason, so callers never string-match a message.

The harness this replaces had eight tools that silently ignored an unrecognised
flag and ran their default mode instead (H135), and guards that passed vacuously
when their input stopped matching (H81, H87, H89). Both are the same shape: a
failure that reads as a success. Every refusal here is an exception; nothing in
core reports a problem by printing and continuing.
"""


class AutoresearchError(Exception):
    """Base for every refusal core makes."""


class ConfigError(AutoresearchError):
    """A domain's configuration is missing, malformed, or self-contradictory."""


class SchemaError(AutoresearchError):
    """A record does not validate against its schema."""


class TransitionError(AutoresearchError):
    """An entry state transition is not permitted by the declared machine."""


class PolicyError(AutoresearchError):
    """A declared never-rule refuses the action."""


class BudgetExceeded(AutoresearchError):
    """A meter's ceiling was reached. Carries the meter that stopped the work."""

    def __init__(self, meter: str, spent, ceiling, message: str = ""):
        self.meter, self.spent, self.ceiling = meter, spent, ceiling
        super().__init__(
            message or f"budget {meter!r} exhausted: {spent} of {ceiling}")


class ClaimError(AutoresearchError):
    """A claim could not be taken, released, or reaped."""


class GoalError(AutoresearchError):
    """A goal is malformed, or an objective cannot be evaluated."""
