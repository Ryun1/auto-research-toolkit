"""Entries: one hypothesis or defect, one file, one truth.

This is the module that deletes the largest failure class in the harness this
core is derived from. There, an entry was a `##` section in an append-only
markdown document, its status was a `- **Status:**` prose line, and *the same
status was also written into a claim record*. Two encodings of one fact (H01),
and the log records what that cost:

* a fix landed on `main` while its entry still read `queued`, and nothing
  noticed (H72);
* an entry could carry two `Result` fields -- the writer wrote the first and the
  reader read the first -- leaving six live entries showing a closed status
  against a Result of "--" (H93);
* `--reopen` left the old Result in place, so the linter refused the entry it
  had just created (H137);
* a Result written by hand could land in the *next* entry's section (H116);
* a closed entry could have no section at all (H117);
* two sections could share an id and both writers silently took the first
  (H11, H13);
* and the heading regex that addressed all of it was derived in six independent
  places, where an en dash for an em dash took every copy to zero matches while
  reporting a clean pass (H81, H106, H132).

Here an entry is a record. Status is a field. The queue document is rendered
from the records and never parsed back. Two entries cannot share an id because
an id is a filename. A Result cannot land in a neighbour because there are no
neighbours.

**History is append-only.** Every transition appends who, when, and why. That is
the property the original guarded with "never delete a section" -- a refutation
is a result, and it is the thing agents most reliably lose (152 of 258 verdicts
in the source corpus were refutations).
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import json
import math
import pathlib
import re
from dataclasses import dataclass, field

import yaml

from .errors import AutoresearchError, SchemaError, TransitionError
from .gates import Gate
from .gates import validate as validate_gates
from .states import CLOSURE_KINDS, REOPEN_REASONS

DISPOSITIONS = ("experiment", "superseded", "already-shipped")

#: Branch intents. Scoped memory keys on exactly this: `debug` branches get
#: their ancestral chain (prior fix attempts), `improve`/`probe` branches get
#: their siblings' verdicts. A root carries no kind -- it is a probe by
#: construction.
KINDS = ("improve", "debug", "probe")


@dataclass
class Applicability:
    """Exact-match scope. Omitted dimensions are unrestricted, never guessed."""
    baseline: str = ""
    source_revision: str = ""
    workload: str = ""
    hardware: str = ""
    parameters: dict[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("baseline", "source_revision", "workload", "hardware"):
            value = getattr(self, name)
            if not isinstance(value, str) or (value and not value.strip()):
                raise SchemaError(f"context.{name} must be a string, not blank whitespace")
        if not isinstance(self.parameters, dict):
            raise SchemaError("context.parameters must be a mapping of named scalars")
        for name, value in self.parameters.items():
            if not isinstance(name, str) or not name.strip():
                raise SchemaError("context parameter names must be non-empty strings")
            if type(value) not in (str, int, float, bool) or (
                    isinstance(value, float) and not math.isfinite(value)):
                raise SchemaError(f"context parameter {name!r} must be a finite scalar")

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SchemaError("context must be an object")
        try:
            return cls(**data)
        except TypeError as exc:
            raise SchemaError(f"invalid context: {exc}") from exc

    def matches(self, candidate: Applicability) -> bool:
        for name in ("baseline", "source_revision", "workload", "hardware"):
            value = getattr(self, name)
            if value and value != getattr(candidate, name):
                return False
        return all(name in candidate.parameters
                   and type(value) is type(candidate.parameters[name])
                   and value == candidate.parameters[name]
                   for name, value in self.parameters.items())

    def describe(self) -> str:
        parts = [f"{name}={getattr(self, name)}"
                 for name in ("baseline", "source_revision", "workload", "hardware")
                 if getattr(self, name)]
        parts += [f"{name}={value!r}" for name, value in sorted(self.parameters.items())]
        return ", ".join(parts) or "unscoped"


def parse_context(text: str) -> Applicability:
    try:
        return Applicability.from_dict(json.loads(text))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid context JSON: {exc}") from exc

ID_RE = re.compile(r"^([A-Z]+)(\d+)$")

#: The evidence class a terminal result carries. The default is the full
#: fabrication net: a confirmed experiment's typed summary must trace to the
#: run ledger (`runs.faithfulness_problems`). The other two classes exist
#: because two field corpora (pvfast-stwo-simd H10/H11) legitimately hold
#: evidence no local ledger row can or should back:
#:   official -- evaluator/host-owned numbers measured on an external pinned
#:               machine (a hosted submission's official ratios);
#:   census   -- deliberately measurement-free work (differential tests, asm
#:               censuses, policy readings; evidence retained as memos).
#: A result marked official/census is not un-evidenced: its memo still must
#: exist (the close gate enforces that regardless of class). What changes is
#: only which mechanical checks apply -- the class is declared, typed, and
#: visible in the record, so a second-class verdict is a stated verdict.
EVIDENCE_CLASSES = ("ledger", "official", "census")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class Claim:
    session: str
    at: str
    why: str = ""
    budget: str = ""
    #: hard ceilings the claim is metered against; free text was H125's defect
    max_runs: int | None = None
    max_hours: float | None = None
    #: sha256 of the token `ar claim` handed out (#33). Ownership is proved
    #: by presenting the token, not by typing a session name; only the hash
    #: is stored, so reading the record does not reveal it. Empty on claims
    #: from before tokens, which keep name ownership.
    token_sha: str = ""


@dataclass
class Result:
    verdict: str
    memo: str
    at: str
    session: str
    summary: str = ""
    #: refutations only: how far the refutation reaches
    closure_kind: str | None = None
    #: what would re-open it. H136: 7 of 18 `slope` closures had none, which
    #: makes a permanent closure wearing a temporary label.
    reopen_condition: str = ""
    #: the worker's own re-review, recorded at the reply boundary. A verdict
    #: reply without one is refused by the coordinator: a claim nobody
    #: re-checked is a claim the record takes on faith. Empty on closes made
    #: outside the loop (`ar close`, migration), which the gate never saw.
    verification: dict = field(default_factory=dict)
    #: None on historical results: their original unscoped semantics survive.
    applicability: Applicability | None = None
    disposition: str = "experiment"
    #: Snapshot the tested premises so later entry amendments cannot extend them.
    mechanisms: list[str] | None = None
    #: Which evidence class backs this result; see EVIDENCE_CLASSES. `None`
    #: means nobody said -- the closure was written without the flag, and the
    #: record says so rather than claiming the run-ledger net that a static
    #: proxy or an empty memo never got (QSB H87). An unlabelled result still
    #: faces the full net: only an explicit `official`/`census` opts out.
    evidence_class: str | None = None
    #: what the closure was measured under (#40): evaluator_sha and
    #: benchmark_revision (its epoch), source_revision and core. Empty on
    #: closures from before the stamp -- `ar epochs` counts those, it does
    #: not guess them.
    pin: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.pin, dict):
            raise SchemaError("result.pin must be a mapping")
        if isinstance(self.applicability, dict):
            self.applicability = Applicability.from_dict(self.applicability)
        if self.applicability is not None and not isinstance(self.applicability, Applicability):
            raise SchemaError("result.applicability must be a context object")
        if self.applicability is not None:
            self.applicability.__post_init__()
        if self.disposition not in DISPOSITIONS:
            raise SchemaError(f"result.disposition must be one of {DISPOSITIONS}")
        if self.mechanisms is not None and (not isinstance(self.mechanisms, list)
                or any(not isinstance(m, str) or not m.strip() for m in self.mechanisms)):
            raise SchemaError("result.mechanisms must be a list of non-empty strings")
        # Absence reads as unlabelled (QSB H87); a blank string is absence too.
        # A value that is present but wrong still raises.
        if isinstance(self.evidence_class, str) and not self.evidence_class.strip():
            self.evidence_class = None
        if (self.evidence_class is not None
                and self.evidence_class not in EVIDENCE_CLASSES):
            raise SchemaError(
                f"result.evidence_class must be one of {EVIDENCE_CLASSES}, "
                f"got {self.evidence_class!r}")


@dataclass
class Event:
    at: str
    kind: str
    who: str
    detail: str = ""
    snapshot: dict = field(default_factory=dict)


@dataclass
class Entry:
    id: str
    track: str
    title: str
    status: str = "queued"
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)

    # the experiment, stated before it is run
    hypothesis: str = ""
    prediction: str = ""
    bar: str = ""                       # pre-registered confirm/refute criteria
    null_checks: list[str] = field(default_factory=list)
    why_filed: str = ""

    # what ranking reads
    confidence: float = 0.5             # P(confirm), 0..1
    impact: float = 0.0                 # expected fractional move on the objective
    cost: float = 1.0                   # in domain run-units
    #: The EXCLUSION vocabulary: what would have to be true for this to work.
    #: A `mechanism`-kind refutation on a shared tag hard-excludes an entry, so
    #: these must name a mechanism, never a category. Migration deliberately
    #: leaves this empty rather than guessing -- mapping a category (a "Lane")
    #: in here made one refutation close an entire lane of the corpus.
    mechanisms: list[str] = field(default_factory=list)
    #: Free classification. Read by humans and by ranking's explanations; never
    #: used to exclude anything.
    tags: list[str] = field(default_factory=list)
    #: a precondition on SHIPPABILITY, not on investigating. Typed, so ranking
    #: can filter on it rather than an agent rediscovering it (H09).
    gate: str = ""
    #: This entry measures the harness, not a hypothesis: a wiring check, a
    #: placeholder, a smoke test. Its numbers are real and its row stays on the
    #: board, but the frontier -- the board's best-objective line, the budget's
    #: stop decision, the loop's -- is computed over everything else, and the
    #: board names the row it left out. Default-deny in the other direction:
    #: nothing is scaffolding unless the domain says so (sig.golf H4, where a
    #: wiring-check fixture scored 4,768 against a real scheme's 3,899,520 and
    #: held the headline for the rest of the campaign).
    scaffold: bool = False
    #: names a [hardware.<class>] requirement. The host is checked against it
    #: BEFORE the entry is claimed, so an experiment this machine cannot run is
    #: refused with a reason instead of dispatched and discovered.
    hardware: str = ""
    gates: list[Gate] = field(default_factory=list)
    context: Applicability = field(default_factory=Applicability)

    # -- defect evidence ---------------------------------------------------
    # Required (non-empty) on any track declaring `requires_defect_evidence`;
    # see config.Track. A defect report missing these is an opinion, and the
    # validation, export and ingest paths all refuse it by name.
    #: which core was running when the defect was observed. Run provenance
    #: deliberately omits the core version -- runs are reproducible from the
    #: record alone. A defect is the opposite case: without the core that
    #: produced it, nobody upstream can reproduce what the reporter saw.
    core: str = ""
    #: a command, or a path relative to the domain root, that demonstrates the
    #: defect. The upstream half of the loop runs it before believing the report.
    repro: str = ""
    #: what actually happened. Paired with `expected`; a report carrying only
    #: one of the two has not said what broke.
    observed: str = ""
    #: what should have happened instead.
    expected: str = ""

    sources: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    #: The tree. A child entry is a branch off work already in the record --
    #: the search step that exchanges a part rather than turning a dial. ""
    #: (the default) marks a novel root branch. Ranking partitions the
    #: shortlist on exactly this field; the coordinator enforces the tree
    #: budgets (`tree_max_depth`, `tree_max_children`) at filing time.
    parent: str = ""
    #: The branch's intent, so memory can be scoped the way AIRA measures it
    #: (arXiv 2507.02554 §4.1): a `debug` branch is handed its ancestral
    #: chain -- the prior fix attempts, so it does not undo its parent's
    #: repair -- while `improve` and `probe` branches are scoped to *sibling*
    #: verdicts, which pushes diversity instead of mode collapse. Empty on
    #: roots (a root is a probe by construction).
    kind: str = ""
    #: Lineage the tree cap does not count (#23): the entry this follows up,
    #: recorded when a `--parent` branch would pass `tree_max_depth`. The
    #: graph draws it and ranking reads the entry as not novel; depth and
    #: sibling budgets ignore it. Empty on roots and on ordinary branches.
    derived_from: str = ""

    claim: Claim | None = None
    result: Result | None = None
    history: list[Event] = field(default_factory=list)
    body: str = ""                      # free prose, carried verbatim

    def __post_init__(self):
        if isinstance(self.context, dict):
            self.context = Applicability.from_dict(self.context)
        if not isinstance(self.context, Applicability):
            raise SchemaError("entry.context must be a context object")
        self.context.__post_init__()
        # list[str] prose fields are joined verbatim into views and briefs; a
        # non-string item (a YAML `key: value` line collapses to a mapping)
        # must be refused here, at the record that caused it, not crash a
        # renderer three layers away.
        for name in ("mechanisms", "tags", "null_checks",
                     "sources", "supersedes", "related"):
            value = getattr(self, name)
            if (not isinstance(value, list)
                    or not all(isinstance(item, str) and item.strip()
                               for item in value)):
                raise SchemaError(
                    f"entry.{name} must be a list of non-empty strings "
                    f"(got {type(value).__name__} item in {name})")
        if type(self.scaffold) is not bool:
            # Typed, like `gate.required`: this field decides whether a run row
            # is a finding, and a string that reads as false is a finding the
            # record silently reclassified as scaffolding.
            raise SchemaError(
                f"entry.scaffold must be a boolean, got "
                f"{type(self.scaffold).__name__}")
        if self.result is not None:
            self.result.__post_init__()
        if not isinstance(self.derived_from, str) or (
                self.derived_from and not ID_RE.match(self.derived_from)):
            # An id, exactly: a hand-edited `x/../Q1` passed the filing check
            # and then silently drew no edge (#48).
            raise SchemaError(
                f"entry.derived_from must be an entry id like Q12, got "
                f"{self.derived_from!r}")
        if self.kind:
            if self.kind not in KINDS:
                raise SchemaError(
                    f"entry.kind must be one of {KINDS}, got {self.kind!r}")
            if not self.parent:
                raise SchemaError(
                    f"entry.kind {self.kind!r} is branch intent; an entry "
                    "with no parent is a novel root and carries no kind")
        if not isinstance(self.gates, list):
            raise SchemaError("gates must be a list")
        try:
            self.gates = [Gate(**g) if isinstance(g, dict) else g for g in self.gates]
        except TypeError as exc:
            raise SchemaError(f"invalid gate record: {exc}") from exc
        validate_gates(self.gates)

    def gate_readiness(self, required_names=()) -> str:
        validate_gates(self.gates)
        named = {g.name: g for g in self.gates}
        required = set(required_names) | {g.name for g in self.gates if g.required}
        if not self.gates and not required:
            return "unconfigured"
        outcomes = [named[n].state if n in named else "pending" for n in required]
        for outcome in ("failed", "blocked", "pending"):
            if outcome in outcomes:
                return outcome
        return "ready" if required else "unconfigured"

    @property
    def readiness(self) -> str:
        return self.gate_readiness()
    @property
    def context_changed(self) -> bool:
        """A scoped result is outside the current context; review/reprice, not reopen."""
        return bool(self.result and self.result.applicability is not None
                    and not self.result.applicability.matches(self.context))


    def closure_mechanisms(self) -> list[str]:
        if self.result is not None and self.result.mechanisms is not None:
            return self.result.mechanisms
        return self.mechanisms

    def result_applies_to(self, candidate: Entry) -> bool:
        return bool(self.result and (self.result.applicability is None
                    or self.result.applicability.matches(candidate.context)))

    # -- transitions ------------------------------------------------------

    def apply(self, machine, dest: str, who: str, why: str = "",
              result: Result | None = None, memo_exists=None) -> None:
        """Move to `dest`, enforcing the declared machine and the evidence rules.

        `memo_exists` is injected rather than read here so this stays pure and so
        core never assumes a filesystem layout the domain owns.
        """
        transition = machine.transition(self.status, dest)
        self.__post_init__()
        if transition.reason_required and not why.strip():
            raise TransitionError(
                f"{self.id}: {self.status} -> {dest} requires a reason "
                f"(writer: {transition.writer}). The reason is what makes the "
                f"move accountable rather than a way to tidy the queue.")

        status = machine.status(dest)
        if status.terminal:
            if result is None:
                raise TransitionError(
                    f"{self.id}: closing as {dest!r} needs a result with an "
                    "evidence memo")
            result.__post_init__()
            validate_gates(self.gates)
            if result.verdict != dest:
                raise TransitionError("result verdict must match destination status")
            if dest in ("confirmed", "fixed"):
                required = set(machine.required_gates) | {
                    gate.name for gate in self.gates if gate.required}
                if required and self.gate_readiness(required) != "ready":
                    raise TransitionError(
                        f"{self.id}: incomplete required gates prevent {dest}")
            if status.requires_evidence:
                if not result.memo:
                    raise TransitionError(
                        f"{self.id}: {dest!r} requires an evidence memo and none "
                        "was given")
                if memo_exists is not None and not memo_exists(result.memo):
                    raise TransitionError(
                        f"{self.id}: evidence memo {result.memo!r} does not "
                        "exist. An entry closed against a memo nobody wrote is "
                        "a Closed row pointing at nothing (H74, H117).")
            if status.requires_closure_kind and result.disposition == "experiment":
                if result.closure_kind not in CLOSURE_KINDS:
                    raise TransitionError(
                        f"{self.id}: closing as {dest!r} requires closure_kind "
                        f"in {sorted(CLOSURE_KINDS)}; got "
                        f"{result.closure_kind!r}.\n"
                        + "\n".join(f"  {k}: {v}" for k, v in CLOSURE_KINDS.items()))
                # H136: a `slope` closure with no re-open condition is a
                # permanent closure wearing a temporary label, and 7 of 18 were.
                if (result.closure_kind in ("slope", "cell")
                        and not result.reopen_condition.strip()):
                    raise TransitionError(
                        f"{self.id}: a {result.closure_kind!r} closure holds only "
                        "within what was measured, so it must state the "
                        "re-open condition -- name the band you measured and "
                        "what leaving it would cost (H136).")
            scope = result.applicability or self.context
            if not self.context.matches(scope):
                raise TransitionError("result applicability cannot broaden or contradict entry context")
            result.applicability = Applicability.from_dict(dataclasses.asdict(scope))
            result.mechanisms = list(self.mechanisms)
            self.result = result
        elif dest == machine.initial and self.result is not None:
            # H137: reopen left the old Result in place and the linter then
            # refused the entry it had just created. The verdict moves into
            # history, where it stays readable, and the live field is cleared.
            self.history.append(Event(
                _now(), "result-archived", who,
                f"{self.result.verdict}: {self.result.summary or self.result.memo}",
                snapshot=dataclasses.asdict(self.result)))
            self.result = None

        self.history.append(Event(_now(), f"{self.status}->{dest}", who, why))
        self.status = dest
        self.updated = _now()

    def reopen(self, machine, who: str, why: str, reason: str | None = None) -> None:
        """Return the entry to the queue. A closed entry must say which kind of
        reopen this is (REOPEN_REASONS), recorded on the transition event; a
        live one (blocked) carries no verdict to categorise and refuses one."""
        closed = machine.status(self.status).terminal
        if closed and reason not in REOPEN_REASONS:
            raise TransitionError(
                f"{self.id} is {self.status!r}: reopening a verdict needs "
                "--reopen-reason, one of:\n"
                + "\n".join(f"  {k}: {v}" for k, v in REOPEN_REASONS.items()))
        if not closed and reason is not None:
            raise TransitionError(
                f"{self.id} is {self.status!r}, not a verdict: --reopen-reason "
                "categorises a reopened verdict and has nothing to say here")
        self.apply(machine, machine.initial, who=who, why=why)
        if closed:
            self.history[-1].snapshot = {"reopen_reason": reason}

    def relabel(self, kind: str, who: str, why: str,
                reopen_condition: str | None = None) -> None:
        """Correct a closure label without disturbing the verdict.

        H133: a label that turns out wrong had no writer, so correcting it meant
        reopening an entry whose verdict was not in doubt.
        """
        if self.result is None:
            raise TransitionError(f"{self.id} has no result to relabel")
        if kind not in CLOSURE_KINDS:
            raise TransitionError(
                f"closure kind {kind!r} not in {sorted(CLOSURE_KINDS)}")
        if not why.strip():
            raise TransitionError("relabelling requires a reason")
        # A relabel to cell/slope must meet the same H136 rule a fresh close
        # does; the flag used to be accepted and discarded (falcon1024 H1, #27).
        if reopen_condition is not None and reopen_condition.strip():
            self.result.reopen_condition = reopen_condition
        if kind in ("slope", "cell") and not self.result.reopen_condition.strip():
            raise TransitionError(
                f"{self.id}: relabelling to {kind!r} requires --reopen-condition: "
                "a closure that holds only within what was measured must say "
                "what would re-open it (H136)")
        old, self.result.closure_kind = self.result.closure_kind, kind
        self.history.append(Event(
            _now(), "relabel", who, f"{old} -> {kind}: {why}"
            + (f" (reopen: {reopen_condition})" if reopen_condition else "")))
        self.updated = _now()

    def set_evidence_class(self, value: str, who: str, why: str) -> None:
        """Declare which evidence class backs an existing result.

        The escape hatch the field corpora asked for (pvfast-stwo-simd H10:
        closures accepted before the faithfulness strictness landed, and
        closures whose evidence is evaluator-owned or measurement-free by
        design, had no writer that could say so -- hand-editing the YAML is
        the move H8 exists to forbid). Like `relabel`: one field, the verdict
        untouched, the reason mandatory, the change appended to history.
        """
        if self.result is None:
            raise TransitionError(f"{self.id} has no result to classify")
        if value not in EVIDENCE_CLASSES:
            raise TransitionError(
                f"evidence class {value!r} not in {sorted(EVIDENCE_CLASSES)}")
        if not why.strip():
            raise TransitionError(
                f"{self.id}: setting an evidence class without a reason is an "
                "unchecked reclassification; pass --why")
        old, self.result.evidence_class = self.result.evidence_class, value
        self.history.append(Event(
            _now(), "evidence-class", who, f"{old} -> {value}: {why}"))
        self.updated = _now()

    def correct_result_text(self, summary: str | None, reopen_condition: str | None,
                            who: str, why: str) -> None:
        """Correct a closure's own prose, in place, from a terminal state.

        A closure that recorded a wrong number could not be corrected: `close`
        refuses a terminal-to-terminal transition, `claim` refuses a terminal
        entry, `release` needs a claim to release, and `amend` reached every
        field of the entry EXCEPT the two that carry the verdict's meaning. So
        the error outlived the session that found it, and the rendered row --
        which shows the summary, truncated -- showed the wrong figure to every
        later reader (QSB H82).

        The status and the verdict are untouched: this is the closure's text
        being corrected, not its outcome being rewritten. A correction without
        a reason is an unaccountable edit of the record, so the reason is
        mandatory, and the change is appended to history like every other
        mutation.
        """
        if self.result is None:
            raise TransitionError(f"{self.id} has no result to correct")
        if not why.strip():
            raise TransitionError(
                f"{self.id}: correcting a closure without a reason is an "
                f"unaccountable edit of the record; pass --why")
        if summary is None and reopen_condition is None:
            raise TransitionError(
                f"{self.id}: nothing to correct; pass --summary and/or "
                f"--reopen-condition")
        parts = []
        if summary is not None:
            self.result.summary = summary
            parts.append(f"summary={summary!r}")
        if reopen_condition is not None:
            self.result.reopen_condition = reopen_condition
            parts.append(f"reopen_condition={reopen_condition!r}")
        self.history.append(Event(
            _now(), "correction", who, f"{'; '.join(parts)}: {why}"))
        self.updated = _now()


    # -- derived ----------------------------------------------------------

    @property
    def number(self) -> int:
        m = ID_RE.match(self.id)
        if not m:
            raise SchemaError(f"entry id {self.id!r} is not <PREFIX><digits>")
        return int(m.group(2))

    @property
    def prefix(self) -> str:
        m = ID_RE.match(self.id)
        if not m:
            raise SchemaError(f"entry id {self.id!r} is not <PREFIX><digits>")
        return m.group(1)

    def is_open(self, machine) -> bool:
        return not machine.status(self.status).terminal

    # -- io ---------------------------------------------------------------

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        return {k: v for k, v in data.items() if v not in (None, [], "")}

    @classmethod
    def from_dict(cls, data: dict) -> Entry:
        entry_id = data.get("id")
        _known_fields(cls, data, f"entry {entry_id!r}")
        data = dict(data)
        if isinstance(data.get("claim"), dict):
            data["claim"] = Claim(**_known_fields(
                Claim, data["claim"], f"entry {entry_id!r} claim"))
        if isinstance(data.get("result"), dict):
            data["result"] = Result(**_known_fields(
                Result, data["result"], f"entry {entry_id!r} result"))
        data["history"] = [Event(**_known_fields(Event, e, f"entry {entry_id!r} event"))
                           if isinstance(e, dict) else e
                           for e in data.get("history", [])]
        return cls(**data)


#: The core version each record field first appeared in (#45). A core older
#: than that refuses the record; `ar validate` names the floor a domain's
#: records need so `[upstream] min_core` can refuse the stale install first.
FIELDS_SINCE = {"claim.token_sha": "0.11.0", "derived_from": "0.11.0",
                "result.pin": "0.11.0"}


def _known_fields(cls, data: dict, what: str) -> dict:
    """`data`, or a refusal naming any field this core does not know.

    Fail closed, never round-trip: an older core that carried `claim.token_sha`
    along unread would grant the claim by session name again (#33). The
    refusal says why, so a mixed-version fleet sees a version problem rather
    than a raw `TypeError` from a constructor (#45)."""
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        from .skills import core_version
        raise SchemaError(
            f"{what} has unknown field(s) {unknown}: this core "
            f"({core_version()}) does not know them -- the record was written "
            f"by a newer core. Upgrade "
            f"this install (`ar harness update`), and set `[upstream] "
            f"min_core` so `ar doctor` refuses the stale one first. "
            f"Known: {sorted(known)}")
    return data


#: Per-file parse cache, keyed by resolved directory then filename:
#: `(size, mtime_ns) -> the parsed record dict`. `all()` runs ~n+k+6 times per
#: coordinator iteration plus once per `next_id`, and re-reading and re-parsing
#: every file each time dominated large corpora. The glob still runs on every
#: call and the signature is re-stat'ed per file, so a file changed by hand or
#: by another process (`ar entry new` from native-dispatch agents) re-parses on
#: the next read. Parsed errors are never cached: an unparsable file raises on
#: every call until it is fixed or removed, and a removed file leaves no ghost
#: row. Hits are rebuilt through `from_dict`, so callers receive fresh objects
#: exactly as a re-read would produce.
_ENTRY_CACHE: dict[str, dict[str, tuple[tuple[int, int, str], dict]]] = {}


def _signature(path: pathlib.Path) -> tuple[int, int, str]:
    """Size, mtime and a short digest of the bytes.

    The first two are a cheap filter; the digest is what makes two writes of
    the *same length* inside one filesystem timestamp granularity
    distinguishable. Without it the guard below silently missed them: eight
    concurrent amends that all produced an identically sized record left four
    events on disk for six successful writers, two corrections gone with no
    error (sig.golf H16). An entry file is a few kilobytes; reading it is
    cheaper than losing a write.
    """
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest()[:16])


class Store:
    """Every entry, addressed by id, one file each.

    H79: ids were allocated by reading a shared document, so two unpublished
    branches took the same id and the loser was renumbered *after* every
    citation to it had been written. One file per entry does not remove the
    race, but it changes the failure into a git merge conflict on a filename --
    loud, at merge time, and resolvable -- instead of a silent renumber.
    """

    def __init__(self, directory):
        self.dir = pathlib.Path(directory)

    def path(self, entry_id: str) -> pathlib.Path:
        return self.dir / f"{entry_id}.yaml"

    def exists(self, entry_id: str) -> bool:
        return self.path(entry_id).exists()

    def load(self, entry_id: str) -> Entry:
        path = self.path(entry_id)
        if not path.exists():
            raise SchemaError(f"no entry {entry_id!r} at {path}")
        return self._read(path)

    def _read(self, path: pathlib.Path) -> Entry:
        """One entry file, named for what is wrong with it if anything is.

        `all()` and `load()` share this because the residue check and the
        cache belong to the read, not to the caller: a marker-bearing file
        that `ar entry show` parses happily while `ar board` refuses is the
        same defect wearing two faces (QSB H83).

        The read also registers what it read, because that is the signature
        `save`'s optimistic-concurrency guard compares against (QSB H71).
        `all()` filled that cache and `load()` did not, so every verb that
        loads an entry by id -- amend, close, claim, release, reprice, gates --
        wrote with no guard at all: eight concurrent amends all reported
        success and two corrections were destroyed with no error and nothing to
        rebase, which is the QSB H71 failure on the one path that needed it
        most (sig.golf H16). The signature is taken BEFORE the read, so a write
        landing during the read is caught too.
        """
        from . import merge as merge_mod
        try:
            signature = _signature(path)
            body = path.read_text()
        except OSError as exc:
            raise SchemaError(f"{path}: {exc}") from exc
        try:
            data = merge_mod.checked_load(path, body, "resolve the conflict in the file")
        except merge_mod.Residue as exc:
            raise SchemaError(str(exc)) from None
        except yaml.YAMLError as exc:
            raise SchemaError(f"{path}: {exc}") from exc
        try:
            entry = Entry.from_dict(data or {})
        except (SchemaError, TypeError, yaml.YAMLError) as exc:
            raise SchemaError(f"{path}: {exc}") from exc
        _ENTRY_CACHE.setdefault(str(self.dir.resolve()), {})[path.name] = \
            (signature, entry.to_dict())
        return entry

    def save(self, entry: Entry, force: bool = False) -> pathlib.Path:
        """Write the record, guarded and exclusive.

        The exclusivity is the newer half of the guard. Comparing a signature
        and then replacing the file are two steps, so two writers could both
        find the file unchanged, both pass, and both replace it -- the loser's
        correction gone, with a success message on both sides (sig.golf H16:
        eight concurrent amends destroyed up to three corrections in roughly a
        quarter of runs). Inside one process it is worse still: two writers
        share the cache the guard reads, so the second write refreshes the
        evidence the first is about to compare against.

        So the guard and the replace run under one named lock, per entry. The
        windows are short on purpose -- a save takes microseconds, so a lock
        older than two minutes was abandoned by a session that died holding it,
        and a writer that cannot get in within ten seconds refuses loudly
        rather than writing over a peer."""
        import os

        from .claims import Lock
        entry.__post_init__()
        self.dir.mkdir(parents=True, exist_ok=True)
        lock = Lock(self.dir.parent / ".locks" / f"{entry.id}.lock",
                    holder=f"entry write (pid {os.getpid()})",
                    timeout=10.0, stale_after=120.0)
        with lock:
            return self._write(entry, force)

    def _write(self, entry: Entry, force: bool = False) -> pathlib.Path:
        """The guarded write `save` holds the lock across. Not a public entry
        point: without the lock the compare and the replace are two steps and
        the guard is advisory."""
        import os
        path = self.path(entry.id)
        # Optimistic concurrency. Two sessions editing the entry set in one
        # checkout can each destroy the other's committed record, and the
        # toolkit reported the mutation as successful -- a closure printed as
        # done was simply gone from disk, with no error and nothing to rebase
        # (QSB H71). A file whose on-disk signature no longer matches the one
        # this process read has been written by somebody else in between, and
        # the write is refused rather than silently merged away.
        cached = _ENTRY_CACHE.get(str(self.dir.resolve()), {}).get(path.name)
        if not force and cached is None and path.exists():
            # A save over a file this process never read is a blind write:
            # it replaced an entry another session (or `create`) had just
            # filed under the same id (#48).
            raise SchemaError(
                f"{path}: exists, but this session never read it -- a save "
                f"here would replace another session's entry unseen. Load it "
                f"first (`store.load`), or pass force=True to overwrite "
                f"deliberately")
        if (not force and cached is not None and path.exists()
                and cached[0] != _signature(path)):
            raise SchemaError(
                f"{path}: changed on disk since this session read it "
                f"(size/mtime/digest {cached[0]} -> {_signature(path)}) -- another "
                f"session wrote this entry concurrently and this save would "
                f"destroy it. Re-read the entry and redo the change, or pass "
                f"force=True to overwrite deliberately (QSB H71)")
        body = yaml.safe_dump(entry.to_dict(), sort_keys=False,
                              allow_unicode=True, width=100)
        # Atomic replace, not truncate-then-write: a concurrent `all()` (a
        # CLI in one terminal, the coordinator in another) must never read a
        # half-written YAML -- the same window `loop._record` closes for
        # iteration records, and the same failure class the defect log
        # exists because of.
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(body)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        # Re-stat after the write: the cached signature must describe the bytes
        # actually on disk, or the next all() would re-parse (harmless) or,
        # worse, trust a signature the write already invalidated.
        _ENTRY_CACHE.setdefault(str(self.dir.resolve()), {})[path.name] = \
            (_signature(path), entry.to_dict())
        # Post-write re-read assertion: the write reported success, so confirm
        # the bytes on disk are the ones written. A tool that says it saved and
        # did not is the failure nobody can see.
        if _signature(path) != _ENTRY_CACHE[str(self.dir.resolve())][path.name][0]:
            raise SchemaError(f"{path}: write reported success but the file on "
                              f"disk does not match what was written")
        return path

    def filing_lock(self, holder: str):
        """One store-wide lock around check-and-create (#48): a sibling cap
        or a dedupe read before filing must still hold when the entry lands.
        Short-held, like the per-entry write lock."""
        from .claims import Lock
        return Lock(self.dir.parent / ".locks" / "filing.lock", holder=holder,
                    timeout=30.0, stale_after=120.0)

    def create(self, entry: Entry, renumber: bool = False) -> pathlib.Path:
        """File a new entry: write it only where no entry exists yet.

        #21: filing used to be `next_id`, an existence check, then `save` --
        three steps. Two sessions filing on one track both computed the same
        number, both found it free, and the second save replaced the first
        session's entry, with success printed on both sides. Here the record
        is written to a temporary file and hard-linked into place, and a link
        never replaces an existing name, so the claim on an id and the record
        under it land in one step -- and a crash leaves either the whole entry
        or no entry, never a reserved number with nothing behind it.

        A taken id is refused, unless `renumber`: an id the store allocated
        moves to the next free number and the entry's `id` is updated. An id
        the caller chose is never moved, because it may already be cited."""
        import os
        entry.__post_init__()
        self.dir.mkdir(parents=True, exist_ok=True)
        while True:
            path = self.path(entry.id)
            body = yaml.safe_dump(entry.to_dict(), sort_keys=False,
                                  allow_unicode=True, width=100)
            # Its own name, never `save`'s pid-only one: two writers in one
            # process must not share a temporary file (review of #21).
            import uuid
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(body)
                os.link(temporary, path)
            except FileExistsError:
                if not renumber:
                    raise SchemaError(
                        f"{entry.id} already exists at {path}. Ids are filenames, "
                        "so a collision is loud here rather than a silent "
                        "renumber after every citation was written (H79).") from None
                taken = int(self.next_id(entry.prefix)[len(entry.prefix):])
                entry.id = f"{entry.prefix}{max(taken, entry.number + 1)}"
                continue
            finally:
                temporary.unlink(missing_ok=True)
            _ENTRY_CACHE.setdefault(str(self.dir.resolve()), {})[path.name] = \
                (_signature(path), entry.to_dict())
            return path

    def all(self) -> list[Entry]:
        return self._scan(None)

    def read_all(self) -> tuple[list[Entry], list[str]]:
        """Every entry that loads, and one problem per file that does not.

        `all()` refuses on the first malformed file, which is right for a verb
        about to act on the store. `ar validate` exists to name what is wrong,
        and stopping at the first file made a multi-session repair a loop of
        fix one, re-run, find the next (#32, QSB H320)."""
        problems: list[str] = []
        return self._scan(problems), problems

    def _scan(self, problems: list[str] | None) -> list[Entry]:
        if not self.dir.exists():
            return []
        cache = _ENTRY_CACHE.setdefault(str(self.dir.resolve()), {})
        out, seen = [], set()
        for path in sorted(self.dir.glob("*.yaml")):
            # A file another session removes between the listing and the stat
            # or read is not part of the store any more: skip it rather than
            # let one rename kill every store-reading verb (#31, QSB H323).
            try:
                sig = _signature(path)
            except FileNotFoundError:
                continue
            cached = cache.get(path.name)
            if cached is not None and cached[0] == sig:
                seen.add(path.name)
                out.append(Entry.from_dict(copy.deepcopy(cached[1])))
                continue
            try:
                entry = self._read(path)
            except SchemaError as exc:
                if not path.exists():
                    continue
                if problems is None:
                    raise
                problems.append(str(exc))
                continue
            seen.add(path.name)
            cache[path.name] = (sig, entry.to_dict())
            out.append(entry)
        # list() first: a concurrent all() may insert while we prune, and a
        # dict that changes size during comprehension iteration raises.
        for name in [n for n in list(cache) if n not in seen]:
            del cache[name]
        return sorted(out, key=lambda e: (e.prefix, e.number))

    def by_track(self, track) -> list[Entry]:
        return [e for e in self.all() if track.is_id(e.id)]

    def next_id(self, prefix: str) -> str:
        used = [e.number for e in self.all() if e.prefix == prefix]
        return f"{prefix}{(max(used) + 1) if used else 1}"

    def duplicates(self) -> list[str]:
        """Always empty by construction -- ids are filenames. Kept as an
        explicit, asserted property so the guarantee is tested rather than
        assumed, which is what H81's vacuous duplicate-id guard was missing."""
        seen, dupes = set(), []
        for entry in self.read_all()[0]:
            if entry.id in seen:
                dupes.append(entry.id)
            seen.add(entry.id)
        return dupes


#: The prices a reprice may move. `confidence` is a probability -- a curator
#: that answers 1.5 is not enthusiastic, it is out of range -- and the score
#: multiplies all three, so a typo here silently reorders the queue.
REPRICE_FIELDS = ("confidence", "impact", "cost")


def reprice(store, entry, *, machine, changes, session, why, at=None):
    """Fold a price correction into one entry: the one writer, shared by the
    loop's curator phase and the out-of-band `ar entry reprice`.

    H140: never reprice a closed entry -- a terminal verdict is the record's
    last word on those numbers. A re-price without a `why` is a guess moving
    the ranking, and a re-price that moves nothing is refused rather than
    recorded as if it happened. Returns the names of the fields that moved.
    """
    if machine.status(entry.status).terminal:
        raise AutoresearchError(
            f"{entry.id} is terminal ({entry.status}); a closed entry is never "
            "repriced (H140)")
    if not why or not why.strip():
        raise AutoresearchError(
            "a re-price without a reason is a guess moving the ranking; pass --why")
    moved = []
    for name in REPRICE_FIELDS:
        value = changes.get(name)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise AutoresearchError(
                f"{name} must be a number, got {value!r}") from None
        if not math.isfinite(value):
            raise AutoresearchError(f"{name} must be finite, got {value!r}")
        if name == "cost" and value <= 0:
            raise AutoresearchError(
                f"cost must be positive, got {value:g}: a zero cost divides "
                "the ranking by nothing")
        if name == "confidence" and not 0.0 <= value <= 1.0:
            raise AutoresearchError(
                f"confidence must be a probability in [0, 1], got {value:g}")
        setattr(entry, name, value)
        moved.append(name)
    if not moved:
        raise AutoresearchError(
            "nothing to reprice: pass at least one of "
            + ", ".join(f"--{f}" for f in REPRICE_FIELDS))
    # `updated` is what staleness measures ("what has the board learned since
    # this entry was last priced", rank.py) -- a re-price IS a pricing, so it
    # must move the timestamp or the correction never lifts the penalty.
    entry.updated = at or _now()
    entry.history.append(Event(entry.updated, "repriced", session, why))
    store.save(entry)
    return moved
