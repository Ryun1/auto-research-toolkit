"""One-way migration: append-only prose queues -> entry records.

This is the only place in core that parses prose, and it is deliberately a
**one-way, one-time** conversion with a fidelity gate rather than a reader the
system depends on. Everything after it reads records.

The corpus this was written for is 271 hypotheses and 143 harness-debt entries
across ~9,100 lines of markdown, with three partly-overlapping sources of truth
per entry:

* the `## Qnn — title` section, whose `- **Status:**` line carries state, date
  and session;
* the `## Closed` table, which carries the evidence memo;
* `state/claims/**/Qnn.json`, which carries the claiming session, the budget it
  registered, the verdict, and -- for refutations -- the `closure` kind.

They disagree. That is the point: two encodings of one fact diverge, and this
corpus has the divergences on record (a fix landing on `main` while its entry
still read `queued`; six entries showing a closed status against a Result of
"--"). So the migration **reports** every disagreement rather than silently
preferring one source, and the report is the audit.

What is deliberately NOT inferred: `mechanisms`, `confidence`, `impact`, `cost`.
Those are what ranking sorts on, and guessing them from prose would put numbers
nobody measured into the one place the loop trusts. Closed entries do not need
them; open entries are listed in the report as needing a price.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from dataclasses import dataclass, field

from ..entries import Entry, Event, Result

SECTION = re.compile(r"(?m)^## ([QH]\d+) — (.*)$")
FIELD = re.compile(r"(?m)^- \*\*([^:*]+):\*\*[ \t]*(.*)$")
CLOSED_ROW = re.compile(
    r"(?m)^\|\s*([QH]\d+)\s*\|\s*([a-z-]+)\s*\|\s*([\d-]+)\s*\|\s*`?([^|`]*)`?\s*\|")
STATUS_LINE = re.compile(r"^(?P<state>\S+)(?P<rest>.*)$")
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
SESSION = re.compile(r"·\s*([A-Za-z0-9][\w.-]*)\s*$")

#: Prose labels that map onto a typed field. Everything else is kept in `body`,
#: verbatim -- 56 entries carry a **Known trap:**, 8 a **Falsifier:**, and a
#: migration that dropped them would be losing exactly the reasoning the
#: never-delete rule exists to preserve.
FIELD_MAP = {
    "Hypothesis": "hypothesis",
    "Prediction": "prediction",
    "Why it is filed": "why_filed",
    "Why it matters": "why_filed",
    "Defect": "hypothesis",
    "What is wrong": "hypothesis",
    "Gate": "gate",
}

BAR = re.compile(r"(\*\*)?(Confirmed|Refuted)\s+if\b", re.IGNORECASE)


@dataclass
class Disagreement:
    entry_id: str
    field: str
    sources: dict

    def line(self) -> str:
        parts = ", ".join(f"{k}={v!r}" for k, v in self.sources.items())
        return f"{self.entry_id}: {self.field} disagrees — {parts}"


@dataclass
class CorpusMigration:
    entries: list[Entry] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    read: dict = field(default_factory=dict)

    def report(self) -> str:
        out = [f"migrated {len(self.entries)} entries"]
        for key, value in sorted(self.read.items()):
            out.append(f"  read {value} {key}")
        out.append(f"  {len(self.disagreements)} source disagreement(s)")
        for d in self.disagreements[:40]:
            out.append(f"    {d.line()}")
        if len(self.disagreements) > 40:
            out.append(f"    ... and {len(self.disagreements) - 40} more")
        out.append(f"  {len(self.missing_evidence)} closed entr(ies) whose "
                   f"evidence memo does not exist on disk")
        for e in self.missing_evidence[:15]:
            out.append(f"    {e}")
        out.append(f"  {len(self.unpriced)} open entr(ies) with no price "
                   f"(confidence/impact/cost/mechanisms) — ranking needs these")
        out.append("    " + ", ".join(self.unpriced) if self.unpriced else "")
        return "\n".join(line for line in out if line)


def _split_sections(text: str) -> list[tuple[str, str, str]]:
    matches = list(SECTION.finditer(text))
    if not matches:
        # A parse that finds nothing must be an error, never an empty result.
        # This corpus has an entry on record about exactly that: swapping the
        # em dash for an en dash took every reader from 80 sections to zero and
        # every guard passed vacuously (H81).
        raise ValueError(
            "no `## Xnn — title` sections found. The em dash is the delimiter; "
            "an en dash or a hyphen takes this to zero matches, and zero "
            "matches is a parse failure, not an empty queue.")
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1), m.group(2).strip(), text[m.end():end]))
    return out


def _parse_status(raw: str) -> tuple[str, str | None, str | None]:
    m = STATUS_LINE.match(raw.strip())
    if not m:
        return "queued", None, None
    state = m.group("state").strip()
    rest = m.group("rest")
    date = DATE.search(rest)
    session = SESSION.search(rest.strip())
    return state, (date.group(1) if date else None), (session.group(1) if session else None)


def _iso(date: str | None) -> str:
    if not date:
        return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    return f"{date}T00:00:00+00:00"


REFERENCE = re.compile(
    r"(?:(?:inbox|docs|guides|bin|data|state|schemas|src|scripts|tests)/[\w./+-]+"
    r"|\b[QH]\d{1,3}\b)")


def _references(text: str) -> list[str]:
    """Paths and entry ids named in a prose field, in order, deduplicated.

    Deliberately conservative: a reference this misses is still in `body`, but a
    fragment of a sentence in a typed field is worse than nothing -- it looks
    like a path and is not one.
    """
    seen, out = set(), []
    for match in REFERENCE.finditer(text):
        value = match.group(0).rstrip(".,;:`)")
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out[:12]


def _verdict_token(raw, terminal) -> str | None:
    """The status a claim record's `verdict` asserts, or None if it asserts none.

    The field holds anything from a bare `"fixed"` to a paragraph beginning
    "Refuted 2026-08-23: the depth axis is fold-bound...". A first attempt
    compared the raw strings and produced 137 "disagreements" that were nothing
    of the kind -- which is the H138 failure exactly: a warning nobody can act
    on trains the reader to skip the ones that matter. So only a leading word
    that IS one of this track's statuses counts as an assertion; prose that
    merely describes the verdict is not compared.
    """
    if not raw:
        return None
    head = re.sub(r"[^a-z-]", "", str(raw).strip().lower().split()[0] if str(raw).split() else "")
    return head if head in terminal else None


def _claim_record(claims_root: pathlib.Path, entry_id: str) -> dict:
    prefix = "harness" if entry_id.startswith("H") else ""
    for candidate in (claims_root / prefix / f"{entry_id}.json",
                      claims_root / prefix / f"{entry_id[0]}{int(entry_id[1:]):02d}.json"):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except ValueError:
                return {}
    archive = claims_root / prefix / "archive"
    if archive.exists():
        for path in sorted(archive.glob(f"{entry_id}-*.json")):
            try:
                return json.loads(path.read_text())
            except ValueError:
                continue
    return {}


def migrate(queue_path, closed_status_map=None, *, track: str,
            claims_root=None, root=None, terminal=()) -> CorpusMigration:
    """Convert one queue document into entry records.

    `terminal` names the statuses this track treats as terminal, so the
    migration does not have to guess which vocabulary it is reading.
    """
    queue_path = pathlib.Path(queue_path)
    root = pathlib.Path(root or queue_path.parent)
    text = queue_path.read_text()
    result = CorpusMigration()

    closed_rows = {m.group(1): {"status": m.group(2), "date": m.group(3),
                                "evidence": m.group(4).strip()}
                   for m in CLOSED_ROW.finditer(text)}
    sections = _split_sections(text)
    result.read = {"sections": len(sections), "closed rows": len(closed_rows)}

    for entry_id, title, body in sections:
        if not entry_id[1:].isdigit():
            continue
        fields = {m.group(1).strip(): m.group(2).strip()
                  for m in FIELD.finditer(body)}
        state, date, session = _parse_status(fields.get("Status", "queued"))

        row = closed_rows.get(entry_id)
        claim = _claim_record(pathlib.Path(claims_root), entry_id) if claims_root else {}

        # Three sources, compared rather than silently reconciled.
        if row and row["status"] != state:
            result.disagreements.append(Disagreement(
                entry_id, "status",
                {"section": state, "closed-table": row["status"]}))
        # A claim record with `reopened_at` keeps its PRE-reopen verdict in
        # `verdict`, so comparing it to the current status manufactures a
        # contradiction out of an ordinary history. The reopen is preserved as
        # an event below instead -- it is some of the best reasoning in the
        # corpus and there is nowhere else it survives.
        reopened = bool(claim.get("reopened_at"))
        claim_state = None if reopened else _verdict_token(claim.get("verdict"), terminal)
        if claim_state and state in terminal and claim_state != state:
            result.disagreements.append(Disagreement(
                entry_id, "verdict",
                {"section": state, "claim-record": claim_state}))

        entry = Entry(
            id=entry_id, track=track, title=title, status=state,
            created=_iso(claim.get("claimed_at", "")[:10] if claim.get("claimed_at") else date),
            updated=_iso(date),
            body=body.strip())

        for label, attr in FIELD_MAP.items():
            if label in fields and not getattr(entry, attr):
                setattr(entry, attr, fields[label])
        if "Prediction" in fields and BAR.search(fields["Prediction"]):
            # The bar was registered inside the prediction prose. Lifting it into
            # its own field is the whole reason a worker can be told not to move it.
            entry.bar = fields["Prediction"]
        if "Null-check requirement" in fields:
            entry.null_checks = [fields["Null-check requirement"]]
        if "Source" in fields:
            # Extract the REFERENCES, not the prose. A first version split the
            # Source line on commas and semicolons, which inside a sentence like
            # "`inbox/mined/x.md` (the grid laws, the per-bit rates, the
            # multipliers)" produced eight fragments of English and no usable
            # path. The raw line survives verbatim in `body`; what belongs in a
            # typed field is what a tool can follow.
            entry.sources = _references(fields["Source"])
        # `Lane:` is the one genuinely structured classifier in the prose, so it
        # becomes a **tag**. It must NOT become a mechanism: mechanisms are the
        # exclusion vocabulary, and a first version put the lane there -- so a
        # single `mechanism`-kind refutation in lane C hard-excluded every
        # queued entry in lane C, which on the real corpus left the whole
        # research queue unrankable. A lane is a category; a refutation does not
        # close a category. `mechanisms` stays empty and is reported as unpriced.
        if "Lane" in fields:
            lane = fields["Lane"].split()[0].strip("*:").lower()
            if lane:
                entry.tags = [f"lane:{lane}"]

        if state in terminal:
            evidence = (row or {}).get("evidence", "")
            entry.result = Result(
                verdict=state,
                memo=evidence,
                at=_iso((row or {}).get("date") or date),
                session=session or claim.get("session", "") or "migrated",
                summary=(fields.get("Result", "") or "")[:600],
                closure_kind=claim.get("closure"),
                reopen_condition=fields.get("Re-open", ""))
            if evidence and not (root / evidence).exists():
                result.missing_evidence.append(f"{entry_id}: {evidence}")
            if not evidence:
                result.missing_evidence.append(f"{entry_id}: (no evidence recorded)")
        else:
            result.unpriced.append(entry_id)
            if claim.get("session") and state == "in-progress":
                from ..entries import Claim
                entry.claim = Claim(
                    session=claim["session"],
                    at=claim.get("claimed_at", _iso(date)),
                    why=claim.get("why") or "",
                    budget=claim.get("budget") or "")

        if claim.get("claimed_at"):
            entry.history.append(Event(
                claim["claimed_at"], "queued->in-progress",
                claim.get("session", "unknown"), claim.get("why") or ""))
        if reopened:
            entry.history.append(Event(
                claim["reopened_at"], "reopened", claim.get("session", "unknown"),
                f"from {claim.get('reopened_from', '?')}: "
                f"{claim.get('reopened_why', '')}"))
        entry.history.append(Event(
            _iso(date), "migrated", "ar migrate",
            f"from {queue_path.name}; prose section preserved verbatim in body"))
        result.entries.append(entry)

    return result
