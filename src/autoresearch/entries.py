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
is a result, and it is the thing agents most reliably lose (151 of 271 verdicts
in the source corpus were refutations).
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import math
import pathlib
import re
from dataclasses import dataclass, field

import yaml

from .errors import AutoresearchError, SchemaError, TransitionError
from .gates import Gate
from .gates import validate as validate_gates
from .states import CLOSURE_KINDS

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

    def __post_init__(self):
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
        if self.result is not None:
            self.result.__post_init__()
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

    def relabel(self, kind: str, who: str, why: str) -> None:
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
        old, self.result.closure_kind = self.result.closure_kind, kind
        self.history.append(Event(_now(), "relabel", who, f"{old} -> {kind}: {why}"))
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
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise SchemaError(
                f"entry {data.get('id')!r} has unknown field(s) {sorted(unknown)}; "
                f"known: {sorted(known)}")
        data = dict(data)
        if isinstance(data.get("claim"), dict):
            data["claim"] = Claim(**data["claim"])
        if isinstance(data.get("result"), dict):
            data["result"] = Result(**data["result"])
        data["history"] = [Event(**e) if isinstance(e, dict) else e
                           for e in data.get("history", [])]
        return cls(**data)


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
_ENTRY_CACHE: dict[str, dict[str, tuple[tuple[int, int], dict]]] = {}


def _signature(path: pathlib.Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


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
        return Entry.from_dict(yaml.safe_load(path.read_text()) or {})

    def save(self, entry: Entry) -> pathlib.Path:
        import os
        entry.__post_init__()
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path(entry.id)
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
        return path

    def all(self) -> list[Entry]:
        if not self.dir.exists():
            return []
        cache = _ENTRY_CACHE.setdefault(str(self.dir.resolve()), {})
        out, seen = [], set()
        for path in sorted(self.dir.glob("*.yaml")):
            seen.add(path.name)
            sig = _signature(path)
            cached = cache.get(path.name)
            if cached is not None and cached[0] == sig:
                out.append(Entry.from_dict(copy.deepcopy(cached[1])))
                continue
            try:
                raw = yaml.safe_load(path.read_text()) or {}
                entry = Entry.from_dict(raw)
            except (SchemaError, TypeError, yaml.YAMLError) as exc:
                raise SchemaError(f"{path}: {exc}") from exc
            cache[path.name] = (sig, raw)
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
        for entry in self.all():
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
        value = float(value)
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
