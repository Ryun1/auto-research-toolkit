"""Measurement epochs: which rows were measured by the same instrument (#40).

When the benchmark source or the evaluator moved -- four times in one field
campaign -- old rows went on competing with new ones on a single frontier,
and a refutation made under the old evaluator could not be told from a
current one. Closures recorded no revision either (#34), so a move could not
list what it invalidated.

So every row `ar measure` records names what it was measured under: the
evaluator hash core computed (#36), the benchmark revision, the candidate's
source revision and the core version. Every closure is pinned the same way.
An epoch is a distinct `(evaluator_sha, benchmark_revision)` pair in the
ledger, numbered by first appearance. It is derived from the rows and never
stored (invariant 4), so it cannot drift from them.

The frontier, the stop decision and independent confirmation read only the
current epoch: the one the evaluator and benchmark *now* identify. Rows from
older epochs stay on the board under their own heading. Rows that predate
the stamp form an `unpinned` epoch, which is shown and never merged into a
pinned one -- except in a ledger with no stamped rows at all, where it is the
only epoch there is and the frontier reads it as before.

The source revision is stamped but is not part of the key: it moves with
every commit in the candidate tree, including record commits, so it names
where a row was measured, not which instrument measured it.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .errors import AutoresearchError

UNPINNED = "unpinned"


def _run(config, name, cwd) -> str:
    """Stdout of a domain-declared command, stripped; checked against policy."""
    command = config.commands[name]
    config.policy.check_command(command)
    from . import evaluator
    argv = [str(config.paths.root / token)
            if evaluator._inside(config.paths.root, token)
            and (config.paths.root / token).exists() else token
            for token in command.split()]
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AutoresearchError(
            f"[commands] {name} failed ({proc.returncode}): "
            f"{proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def benchmark_revision(config) -> str:
    """`[commands] benchmark_revision`, run in the domain root; empty when the
    domain declares none -- the evaluator hash alone then keys the epoch."""
    if "benchmark_revision" not in config.commands:
        return ""
    return _run(config, "benchmark_revision", config.paths.root)


def source_revision(config, tree) -> tuple[str, bool | None]:
    """`(revision, dirty)` of the tree a row was measured in.

    `[commands] source_revision` when declared (run in the tree; it says
    nothing about dirtiness, so `dirty` is None). Otherwise git's HEAD and
    whether tracked files differ from it. Outside git: empty, never guessed.
    """
    if "source_revision" in config.commands:
        return _run(config, "source_revision", tree), None
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree,
                          capture_output=True, text=True)
    if head.returncode != 0:
        return "", None
    status = subprocess.run(["git", "status", "--porcelain",
                             "--untracked-files=no"], cwd=tree,
                            capture_output=True, text=True)
    return head.stdout.strip(), bool(status.stdout.strip())


def current_key(config) -> tuple[str, str]:
    from . import evaluator
    return evaluator.identity(config), benchmark_revision(config)


def key_of(record) -> tuple[str, str] | None:
    """A row's epoch key, or None for a row that predates the stamp."""
    provenance = record.provenance if isinstance(record.provenance, dict) else {}
    sha = provenance.get("evaluator_sha")
    if not sha:
        return None
    return str(sha), str(provenance.get("benchmark_revision") or "")


def pin_key(pin) -> tuple[str, str] | None:
    if not isinstance(pin, dict) or not pin.get("evaluator_sha"):
        return None
    return str(pin["evaluator_sha"]), str(pin.get("benchmark_revision") or "")


@dataclass
class Epoch:
    label: str                       # "1", "2", ... or "unpinned"
    key: tuple[str, str] | None
    current: bool
    rows: list = field(default_factory=list)

    @property
    def first_seen(self) -> float:
        return min((r.started for r in self.rows), default=0.0)


def segment(config, records, current=None) -> list[Epoch]:
    """Every epoch in the ledger, unpinned first, then pinned ones by first
    appearance. The current epoch is listed even with no rows yet: an
    evaluator that just changed has an empty frontier, and the board says so."""
    current = current_key(config) if current is None else current
    groups: dict = {}
    for record in records:
        groups.setdefault(key_of(record), []).append(record)
    pinned = sorted((k for k in groups if k is not None),
                    key=lambda k: min(r.started for r in groups[k]))
    if pinned and current not in groups:
        pinned.append(current)
    out = []
    if None in groups:
        out.append(Epoch(UNPINNED, None, not pinned, groups[None]))
    for n, key in enumerate(pinned, 1):
        out.append(Epoch(str(n), key, key == current, groups.get(key, [])))
    return out


def current_rows(config, records, label=None) -> list:
    """The rows the frontier may compare: the current epoch's, or the epoch
    `label` names (`ar board --epoch`)."""
    listed = segment(config, records)
    if label is None:
        chosen = [e for e in listed if e.current]
        return chosen[0].rows if chosen else []
    for epoch in listed:
        if epoch.label == str(label):
            return epoch.rows
    raise AutoresearchError(
        f"no epoch {label!r}; this ledger has "
        f"{', '.join(e.label for e in listed) or 'none'} (see `ar epochs`)")


def pin(config, records, entry_id, *, source_revision_override="",
        declared_revision="") -> dict:
    """What a closure was measured under.

    The entry's latest stamped row, when it has one: that is literally the
    instrument and the tree its evidence came from. Otherwise -- a static
    closure, or rows from before the stamp -- the current epoch and core. The
    source revision is the one the closer names (`--source-revision`, or a
    worker's declared applicability), then the row's; never guessed from the
    primary's HEAD, which moves with every record commit.
    """
    from .skills import core_version
    own = [r for r in records if r.entry == entry_id and key_of(r) is not None]
    if own:
        latest = max(own, key=lambda r: r.started)
        sha, benchmark = key_of(latest)
        revision = latest.provenance.get("source_revision") or ""
        core = latest.provenance.get("core") or ""
    else:
        sha, benchmark = current_key(config)
        revision, core = "", ""
    return {"evaluator_sha": sha, "benchmark_revision": benchmark,
            "source_revision": (source_revision_override or declared_revision
                                or revision),
            "core": core or core_version()}
