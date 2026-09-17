"""The entry state machine, declared up front and proven total.

The harness this core is derived from grew its state machine by discovering,
one defect at a time, that it had no legal move:

* `blocked -> queued` had no supported writer, so a stale block was permanent by
  construction (H111, and H35 which named it and fixed only the downstream half).
* There was no supported way to release a claim, so an agent that claimed
  out-of-lane work had to "close it dishonestly or squat" (H78).
* A closure label could not be corrected without reopening the entry, and the
  two labels did not cover a direct point measurement (H133).
* A `slope` closure with no re-open condition is a permanent closure wearing a
  temporary label, and 7 of 18 were (H136).

Every one of those is a state or an edge that was *terminal by omission*: nobody
decided it should be a dead end, it simply had no writer. So this module makes
the machine declarative and asserts three properties at load time:

1. every status is reachable from the initial status;
2. every status has at least one outbound transition -- including terminal ones,
   whose outbound edge is the reopen path;
3. every transition names the writer responsible for it, so "who can move this"
   is never an open question.

`ar validate` runs `StateMachine.check_total()`, which means a domain cannot ship
a state machine with a dead end in it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConfigError, TransitionError


@dataclass(frozen=True)
class Status:
    name: str
    terminal: bool = False
    #: a terminal status must point at a memo that exists before it can be set
    requires_evidence: bool = False
    #: a refutation must say whether it holds outside the range measured
    requires_closure_kind: bool = False
    description: str = ""


@dataclass(frozen=True)
class Transition:
    source: str
    dest: str
    writer: str          # the command permitted to make this move
    reason_required: bool = False


#: Why a direction is dead, and therefore what would re-open it. Ported verbatim
#: in meaning from the harness that invented the taxonomy, because it is the
#: single sharpest idea in it: a refutation asserts a direction is dead, and that
#: assertion has a scope.
CLOSURE_KINDS = {
    "mechanism": "holds outside the range measured; re-opens only on a new mechanism",
    "slope":     "holds only inside the band measured; leaving that band re-opens it",
    "cell":      "a direct point measurement; re-opens if the cell moves",
}


@dataclass
class StateMachine:
    statuses: dict[str, Status]
    initial: str
    transitions: list[Transition] = field(default_factory=list)
    required_gates: tuple[str, ...] = ()

    def __post_init__(self):
        if self.initial not in self.statuses:
            raise ConfigError(
                f"initial status {self.initial!r} is not declared; "
                f"declared: {sorted(self.statuses)}")
        for t in self.transitions:
            for end in (t.source, t.dest):
                if end not in self.statuses:
                    raise ConfigError(
                        f"transition {t.source}->{t.dest} names undeclared "
                        f"status {end!r}")

    # -- the totality properties ----------------------------------------

    def reachable(self) -> set[str]:
        seen, frontier = {self.initial}, [self.initial]
        while frontier:
            here = frontier.pop()
            for t in self.transitions:
                if t.source == here and t.dest not in seen:
                    seen.add(t.dest)
                    frontier.append(t.dest)
        return seen

    def dead_ends(self) -> list[str]:
        """Statuses with no way out. A terminal status still needs one: that is
        the reopen edge, and its absence is H111 exactly."""
        with_exit = {t.source for t in self.transitions}
        return sorted(set(self.statuses) - with_exit)

    def check_total(self) -> None:
        problems = []
        unreachable = sorted(set(self.statuses) - self.reachable())
        if unreachable:
            problems.append(
                f"unreachable from {self.initial!r}: {unreachable} -- a status "
                f"nothing can enter is a status nothing can be")
        dead = self.dead_ends()
        if dead:
            problems.append(
                f"no outbound transition: {dead} -- terminal by omission (H111). "
                f"A terminal status needs a reopen edge; a live one needs an exit")
        unwritten = sorted({f"{t.source}->{t.dest}" for t in self.transitions
                            if not t.writer})
        if unwritten:
            problems.append(f"transitions with no named writer: {unwritten}")
        if problems:
            raise ConfigError(
                "state machine is not total:\n  - " + "\n  - ".join(problems))

    # -- use -------------------------------------------------------------

    def transition(self, source: str, dest: str) -> Transition:
        for t in self.transitions:
            if t.source == source and t.dest == dest:
                return t
        legal = sorted(t.dest for t in self.transitions if t.source == source)
        raise TransitionError(
            f"{source!r} -> {dest!r} is not a declared transition; "
            f"from {source!r} the legal moves are {legal or '(none)'}")

    def status(self, name: str) -> Status:
        try:
            return self.statuses[name]
        except KeyError:
            raise TransitionError(
                f"unknown status {name!r}; declared: {sorted(self.statuses)}") from None

    @property
    def terminal_names(self) -> set[str]:
        return {n for n, s in self.statuses.items() if s.terminal}

    @property
    def open_names(self) -> set[str]:
        return {n for n, s in self.statuses.items() if not s.terminal}

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> StateMachine:
        raw = data.get("statuses")
        if not raw:
            raise ConfigError("a track's state machine declares no statuses")
        statuses = {}
        for name, spec in raw.items():
            spec = spec or {}
            statuses[name] = Status(
                name=name,
                terminal=bool(spec.get("terminal", False)),
                requires_evidence=bool(spec.get("requires_evidence", False)),
                requires_closure_kind=bool(spec.get("requires_closure_kind", False)),
                description=spec.get("description", ""))
        transitions = []
        for spec in data.get("transitions") or []:
            try:
                transitions.append(Transition(
                    source=spec["from"], dest=spec["to"], writer=spec.get("writer", ""),
                    reason_required=bool(spec.get("reason_required", False))))
            except KeyError as exc:
                raise ConfigError(f"transition is missing key {exc}: {spec!r}") from exc
        return cls(statuses=statuses,
                   initial=data.get("initial", "queued"),
                   transitions=transitions)


def default_machine(terminal: dict[str, dict] | None = None) -> StateMachine:
    """The shape both of the original harness's queues share, as a starting
    point a domain can override. Research used confirmed/refuted; harness debt
    used fixed/wontfix/refuted. The differences are entirely in the terminal
    set, which is why it is the only parameter."""
    terminal = terminal or {
        "confirmed": {"requires_evidence": True},
        "refuted": {"requires_evidence": True, "requires_closure_kind": True},
    }
    statuses = {
        "queued": Status("queued", description="filed, unclaimed"),
        "in-progress": Status("in-progress", description="claimed; work started"),
        "blocked": Status("blocked", description="waiting on a named condition"),
    }
    for name, spec in terminal.items():
        statuses[name] = Status(name, terminal=True, **spec)

    transitions = [
        Transition("queued", "in-progress", "ar claim"),
        # H78: releasing a claim must be a supported move, or an agent that
        # claimed the wrong thing has to lie about it.
        Transition("in-progress", "queued", "ar claim --release", reason_required=True),
        Transition("in-progress", "blocked", "ar close", reason_required=True),
        # H111: this edge is the one that was missing, and its absence made
        # `blocked` permanent by construction.
        Transition("blocked", "queued", "ar close --reopen", reason_required=True),
    ]
    for name in terminal:
        transitions.append(Transition("in-progress", name, "ar close"))
        # Every terminal status keeps a reopen edge: a discharged premise must
        # have a supported route back, or `wontfix` becomes a hand edit.
        transitions.append(
            Transition(name, "queued", "ar close --reopen", reason_required=True))
    return StateMachine(statuses=statuses, initial="queued", transitions=transitions)
