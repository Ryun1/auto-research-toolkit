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

import dataclasses
import datetime as dt
import pathlib
import re
from dataclasses import dataclass, field

import yaml

from .errors import SchemaError, TransitionError
from .states import CLOSURE_KINDS

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


@dataclass
class Event:
    at: str
    kind: str
    who: str
    detail: str = ""


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

    sources: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)

    claim: Claim | None = None
    result: Result | None = None
    history: list[Event] = field(default_factory=list)
    body: str = ""                      # free prose, carried verbatim

    # -- transitions ------------------------------------------------------

    def apply(self, machine, dest: str, who: str, why: str = "",
              result: Result | None = None, memo_exists=None) -> None:
        """Move to `dest`, enforcing the declared machine and the evidence rules.

        `memo_exists` is injected rather than read here so this stays pure and so
        core never assumes a filesystem layout the domain owns.
        """
        transition = machine.transition(self.status, dest)
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
            if status.requires_closure_kind:
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
            self.result = result
        elif dest == machine.initial and self.result is not None:
            # H137: reopen left the old Result in place and the linter then
            # refused the entry it had just created. The verdict moves into
            # history, where it stays readable, and the live field is cleared.
            self.history.append(Event(
                _now(), "result-archived", who,
                f"{self.result.verdict}: {self.result.summary or self.result.memo}"))
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
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path(entry.id)
        path.write_text(yaml.safe_dump(entry.to_dict(), sort_keys=False,
                                       allow_unicode=True, width=100))
        return path

    def all(self) -> list[Entry]:
        if not self.dir.exists():
            return []
        out = []
        for path in sorted(self.dir.glob("*.yaml")):
            try:
                out.append(Entry.from_dict(yaml.safe_load(path.read_text()) or {}))
            except (SchemaError, TypeError, yaml.YAMLError) as exc:
                raise SchemaError(f"{path}: {exc}") from exc
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
