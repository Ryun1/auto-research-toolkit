"""The typed goal: metrics, an objective over them, a possibly-moving target,
derived constants, and the conditions under which the loop stops.

This is the module that makes autonomy possible. Ranking needs to know what an
experiment is *worth*, and the stopping rule needs to know whether the loop is
still making progress; both are functions of distance to a goal. In the harness
this core is derived from, the goal was prose in an agent brief and its distance
was recomputed by hand in roughly thirty curation essays -- so no tool could
rank, and nothing could say when to stop (H125).

Three properties are deliberate:

* **Derived constants are expressions, never stored numbers.** See `expr.py` for
  the failure this avoids.
* **The target may be an external moving value.** The domain that motivated this
  chases a frontier that moved 22.5% in 18.8 days, which means "are we winning"
  is not answerable from our own measurements alone. `Target.moving` makes that
  first-class rather than a footnote.
* **A goal declares its own stopping conditions.** Met, unreachable, or yielding
  below a floor -- all three end a loop, and a loop that cannot end is not
  autonomous, it is unattended.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

from . import expr
from .errors import GoalError

MINIMISE, MAXIMISE = "minimise", "maximise"
DIRECTIONS = (MINIMISE, MAXIMISE)


@dataclass(frozen=True)
class Metric:
    """One number a domain can measure, and how to read it out of a run record."""
    name: str
    field: str                       # key in the run record's `metrics` mapping
    direction: str = MINIMISE
    unit: str = ""
    description: str = ""

    def __post_init__(self):
        if self.direction not in DIRECTIONS:
            raise GoalError(
                f"metric {self.name!r} has direction {self.direction!r}; "
                f"expected one of {DIRECTIONS}")


@dataclass
class Target:
    """What the objective is measured against.

    Either a fixed `value`, or a `source` command that prints one number -- a
    moving target probed at most every `refresh_seconds`.
    """
    value: float | None = None
    source: str | None = None
    moving: bool = False
    refresh_seconds: float = 3600.0
    _cached: float | None = dataclasses.field(default=None, repr=False)
    _cached_at: float = dataclasses.field(default=0.0, repr=False)

    def __post_init__(self):
        if (self.value is None) == (self.source is None):
            raise GoalError(
                "a target needs exactly one of `value` or `source`; "
                f"got value={self.value!r} source={self.source!r}")

    def resolve(self, probe=None, now=None) -> float:
        """Current target value. `probe` is a callable taking the source command
        and returning a float -- injected so this stays testable and so the core
        never decides how a domain runs a command."""
        if self.value is not None:
            return float(self.value)
        now = time.time() if now is None else now
        fresh = (self._cached is not None
                 and now - self._cached_at < self.refresh_seconds)
        if fresh:
            return self._cached
        if probe is None:
            if self._cached is not None:
                return self._cached
            raise GoalError(
                f"target source {self.source!r} needs a probe and none was given")
        self._cached = float(probe(self.source))
        self._cached_at = now
        return self._cached


@dataclass
class YieldFloor:
    """The loop stops when it stops learning.

    `confirmed_per_iteration` below this rate, sustained over `over_iterations`,
    means the current queue is not repaying the machine time. This is the third
    exit -- beside "goal met" and "budget spent" -- and its absence is why the
    original loop could only be stopped by a human noticing.
    """
    confirmed_per_iteration: float = 0.0
    over_iterations: int = 0

    @property
    def active(self) -> bool:
        return self.over_iterations > 0

    def breached(self, verdicts_per_iteration: list[int]) -> bool:
        if not self.active or len(verdicts_per_iteration) < self.over_iterations:
            return False
        window = verdicts_per_iteration[-self.over_iterations:]
        return (sum(window) / len(window)) < self.confirmed_per_iteration


@dataclass
class Goal:
    id: str
    objective: str                              # expression over metric names
    metrics: dict[str, Metric]
    target: Target
    direction: str = MINIMISE                   # of the objective itself
    description: str = ""
    derived: dict[str, str] = field(default_factory=dict)
    stop_when: str | None = None
    yield_floor: YieldFloor = field(default_factory=YieldFloor)
    #: Gate names that must read "passed" on the entry owning the best run
    #: before the loop may record goal-met. Field evidence (eip8200-research):
    #: its goal text required a kernel-checked proof, yet the loop recorded
    #: `goal-met` from the objective expression alone -- the record diagnosed
    #: the mismatch in stop_detail and the next iterations stopped anyway. A
    #: stop that records a win the goal text does not allow is the defect, so
    #: the linkage is declared here and checked in the one place the loop can
    #: decide to end; an unearned win is recorded as running-with-reason.
    required_gates: tuple[str, ...] = ()
    #: Escape hatch for a goal whose target is legitimately exactly zero. At
    #: zero-on-zero any comparison expression is satisfied vacuously, so a loop
    #: with `stop_when: "objective < target"` stops on iteration one having
    #: learned nothing (eip8200-research displayed those near-zero gaps as
    #: "-0"). The default rule refuses a zero target outright via
    #: `distance_to`; this flag covers custom `stop_when` expressions. Set it
    #: true only when zero really is the target.
    allow_degenerate_target: bool = False

    def __post_init__(self):
        if self.direction not in DIRECTIONS:
            raise GoalError(f"goal direction {self.direction!r} not in {DIRECTIONS}")
        self._check_names()

    def _check_names(self):
        """Every free name in every expression must resolve. Checked at load, so
        a typo in `goal.yaml` fails before an iteration is spent, not during one."""
        known = set(self.metrics)
        for name, source in self.derived.items():
            missing = expr.names(source) - known - set(self.derived)
            if missing:
                raise GoalError(
                    f"derived constant {name!r} = {source!r} reads unknown "
                    f"name(s) {sorted(missing)}; metrics are {sorted(known)}")
        available = known | set(self.derived)
        missing = expr.names(self.objective) - available
        if missing:
            raise GoalError(
                f"objective {self.objective!r} reads unknown name(s) "
                f"{sorted(missing)}; available are {sorted(available)}")
        if self.stop_when:
            allowed = available | {"objective", "target", "distance"}
            missing = expr.names(self.stop_when) - allowed
            if missing:
                raise GoalError(
                    f"stop_when {self.stop_when!r} reads unknown name(s) "
                    f"{sorted(missing)}; available are {sorted(allowed)}")

    # -- evaluation ------------------------------------------------------

    def namespace(self, measurements: dict[str, float]) -> dict[str, Any]:
        """Metrics plus every derived constant, computed. Refuses a measurement
        set missing a declared metric rather than defaulting it -- a missing
        number that reads as zero is how four rows claiming a perfect score got
        into the corpus this core is derived from (H134)."""
        missing = set(self.metrics) - set(measurements)
        if missing:
            raise GoalError(
                f"measurements are missing declared metric(s) {sorted(missing)}")
        ns: dict[str, Any] = {k: measurements[k] for k in self.metrics}
        for name, source in self.derived.items():
            ns[name] = expr.evaluate(source, ns)
        return ns

    def objective_value(self, measurements: dict[str, float]) -> float:
        return expr.evaluate(self.objective, self.namespace(measurements))

    def better(self, value: float, than: float) -> bool:
        """Is `value` the better objective, in whichever direction this goal
        wants? Three callers each kept their own `<` and all three were wrong
        for `direction: maximise` -- see `runs.best_run`, which is now the only
        place that picks a winner."""
        return value < than if self.direction == MINIMISE else value > than

    def distance_to(self, value: float, target: float) -> float:
        """Signed, normalised distance from an objective value to the target.
        Positive means not there yet; negative means past it, in whichever
        direction the goal wants.

        Normalising by the target is what lets ranking compare a proposed
        improvement against the objective without the domain's units leaking
        into core."""
        if target == 0:
            raise GoalError("cannot normalise distance against a zero target")
        gap = (value - target) if self.direction == MINIMISE else (target - value)
        return gap / abs(target)

    def distance(self, measurements: dict[str, float], target: float) -> float:
        """`distance_to` for a set of measurements."""
        return self.distance_to(self.objective_value(measurements), target)

    def is_met(self, measurements: dict[str, float], target: float) -> bool:
        ns = self.namespace(measurements)
        ns["objective"] = expr.evaluate(self.objective, ns)
        ns["target"] = target
        if self.stop_when:
            # A custom expression decides; the normalised distance is only
            # injected when the expression reads it. Computing it anyway made
            # a zero target refuse even a `stop_when` that never uses
            # `distance` -- which is exactly the case allow_degenerate_target
            # exists to arbitrate.
            if "distance" in expr.names(self.stop_when):
                ns["distance"] = self.distance(measurements, target)
            return bool(expr.evaluate(self.stop_when, ns))
        ns["distance"] = self.distance(measurements, target)
        return ns["distance"] <= 0

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> Goal:
        if "goal" in data:
            data = data["goal"]
        try:
            gid = data["id"]
            objective = data["objective"]
        except KeyError as exc:
            raise GoalError(f"goal is missing required key {exc}") from exc

        raw_metrics = data.get("metrics") or {}
        if not raw_metrics:
            raise GoalError(f"goal {gid!r} declares no metrics")
        metrics = {}
        for name, spec in raw_metrics.items():
            if not isinstance(spec, dict):
                raise GoalError(f"metric {name!r} must be a mapping, got {type(spec).__name__}")
            metrics[name] = Metric(
                name=name,
                field=spec.get("field", name),
                direction=spec.get("direction", MINIMISE),
                unit=spec.get("unit", ""),
                description=spec.get("description", ""))

        raw_target = data.get("target")
        if not isinstance(raw_target, dict):
            raise GoalError(f"goal {gid!r} needs a `target` mapping")
        target = Target(
            value=raw_target.get("value"),
            source=raw_target.get("source"),
            moving=bool(raw_target.get("moving", False)),
            refresh_seconds=float(raw_target.get("refresh_seconds", 3600)))

        raw_floor = data.get("yield_floor") or {}
        floor = YieldFloor(
            confirmed_per_iteration=float(raw_floor.get("confirmed_per_iteration", 0.0)),
            over_iterations=int(raw_floor.get("over_iterations", 0)))

        raw_gates = data.get("required_gates") or []
        if not isinstance(raw_gates, list):
            raise GoalError(
                f"goal {gid!r}: required_gates must be a list of gate names, "
                f"got {type(raw_gates).__name__}")
        gates: list[str] = []
        for name in raw_gates:
            if not isinstance(name, str) or not name.strip():
                raise GoalError(
                    f"goal {gid!r}: required_gates entries must be nonempty "
                    f"strings, got {name!r}")
            if name in gates:
                raise GoalError(f"goal {gid!r}: required_gates names {name!r} twice")
            gates.append(name)

        degenerate = data.get("allow_degenerate_target", False)
        if not isinstance(degenerate, bool):
            raise GoalError(
                f"goal {gid!r}: allow_degenerate_target must be a bool, got "
                f"{type(degenerate).__name__}")

        return cls(
            id=gid,
            objective=objective,
            metrics=metrics,
            target=target,
            direction=data.get("direction", MINIMISE),
            description=data.get("description", ""),
            derived=dict(data.get("derived") or {}),
            stop_when=data.get("stop_when"),
            yield_floor=floor,
            required_gates=tuple(gates),
            allow_degenerate_target=degenerate)
