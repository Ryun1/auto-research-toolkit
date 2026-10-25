"""Validate-side conventions that are nudges, not failures.

`cmd_validate` (cli.py) owns the problems that fail `ar validate`. This module
holds the checks that must never change the exit code: a domain whose inbox
doubles as an evidence store still validates clean -- the warning is a
convention nudge printed alongside the problems, never counted as one.
"""

import pathlib

#: An inbox file past this size is almost certainly terminal evidence (a
#: dataset, a binary, a core dump) that belongs in data/artifacts/, not the
#: handoff queue. Chosen above any plausible memo: the field corpora carry
#: 95KB binaries in inbox/ and must keep validating clean.
INBOX_WARN_BYTES = 64 * 1024


def inbox_warnings(config) -> list[str]:
    """Warnings (not problems) about inbox files that look like evidence.

    Reads config.paths.memos -- the inbox, the handoff queue between agents --
    and names any file over INBOX_WARN_BYTES with the remedy. An absent or
    empty inbox is the normal case and yields nothing.
    """
    memos = pathlib.Path(config.paths.memos)
    if not memos.is_dir():
        return []
    warnings = []
    for path in sorted(memos.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size <= INBOX_WARN_BYTES:
            continue
        rel = path.relative_to(config.paths.root)
        warnings.append(
            f"{rel} is {size / 1024:.1f} KiB -- terminal evidence belongs in "
            "data/artifacts/ (the findings lane already covers it); inbox/ is "
            "the handoff queue")
    return warnings


def closure_warnings(config) -> list[str]:
    """Terminal entries whose closure states nothing, named with the remedy.

    `ar close` refuses a verdict with no `--summary` (sig.golf H18), so these
    are closures that predate the gate or came in by a path that never had one.
    A warning, not a problem, because validate's contract is that a convention
    nudge never changes the exit code -- and because the remedy exists
    (`ar entry amend <id> --summary ... --why ...`), so this is work to do, not
    a broken record.
    """
    from .entries import Store
    from .errors import AutoresearchError
    out = []
    # Files that will not load, and ids no track owns, are validate's
    # problems already; a warning pass must not stop on them (#32, #18).
    for entry in Store(config.paths.entries).read_all()[0]:
        if entry.result is None:
            continue
        try:
            track = config.track_for(entry.id)
        except AutoresearchError:
            continue
        if not track.machine.status(
                entry.result.verdict).terminal:
            continue
        if (entry.result.summary or "").strip():
            continue
        out.append(
            f"{entry.id}: closed as {entry.result.verdict} with no summary -- "
            f"the record states no verdict (renders as a dash, and the "
            f"fabrication net has nothing to read). Complete it with "
            f"`ar entry amend {entry.id} --summary ... --why ...`")
    return out


def output_warnings(config) -> list[str]:
    """Run rows whose declared evidence is not in the tree.

    `RunRecord.outputs` is the only pointer from a number to what produced it,
    and nothing in core ever read that declaration back: a row whose outputs
    were never retained, or were deleted afterwards, reads exactly like one
    whose evidence is present (sig.golf H22 -- a hand-written row declaring two
    paths that do not exist left `ar validate` clean).

    A warning, never a problem: validate's contract is that a convention nudge
    does not change the exit code, and a domain that retires scratch outputs
    would otherwise be unable to validate at all. What core *can* check is
    whether the declared path is **inside the tree**; whether a retained file is
    still the bytes that were measured is the domain's own question (it keeps
    digests for exactly that).

    Containment, not existence: an absolute or `../` declaration resolves to a
    real file often enough that an existence test passes it, while bytes that
    live outside the root survive neither a clone, an `ar evidence pack`, a
    bundle nor an `ar harness update` -- so the evidence a run row points at is
    not the evidence that travels with the record (sig.golf H26 -- a row
    declaring `/etc/hosts` validated clean because the file was there).
    """
    from .runs import read_all
    out = []
    try:
        records = read_all(config.paths.runs)
    except Exception as exc:            # the ledger's own check names bricked rows
        return [f"runs: {exc}"]
    root = config.paths.root.resolve()
    for record in records:
        for name in record.outputs:
            path = (config.paths.root / name).resolve()
            if not path.is_relative_to(root):
                out.append(
                    f"run {record.id}: declares output {name!r}, which resolves "
                    f"outside the domain tree -- evidence that does not travel "
                    f"with the record cannot be the evidence a number rests on "
                    f"(pack it into the domain, or drop the declaration)")
            elif not path.exists():
                out.append(
                    f"run {record.id}: declares output {name!r}, which is not "
                    f"in the tree -- the measurement's evidence is gone "
                    f"(retain it with `ar evidence pack`, or drop the "
                    f"declaration)")
    return out


#: How far a row's self-reported duration may fall short of the wall clock
#: `ar measure` kept around it before validate says so. Generous on purpose:
#: interpreter start-up and imports sit outside `started`/`finished` and are
#: real, so only a gap that is both large and most of the run is named. A
#: duration longer than the wall clock is impossible and needs no margin
#: beyond rounding.
WALL_CLOCK_SLACK_SECONDS = 30.0
WALL_CLOCK_SLACK_SHARE = 0.5


def measurement_warnings(config) -> list[str]:
    """Rows whose own timestamps disagree with the wall clock core kept.

    #36: `started` and `finished` are written by the measure script, inside
    the candidate's process -- a patched `time.time` (METR found o3 doing
    exactly this) moves them. `cost.wall_seconds` is timed by core around the
    subprocess and does not move. A row with no `wall_seconds` predates the
    stamp and is not judged."""
    from .runs import read_all
    try:
        records = read_all(config.paths.runs)
    except Exception:                   # output_warnings already names it
        return []
    out = []
    for record in records:
        cost = record.cost if isinstance(record.cost, dict) else {}
        wall = cost.get("wall_seconds")
        if not isinstance(wall, (int, float)) or record.finished is None:
            continue
        try:
            reported = float(record.finished) - float(record.started)
        except (TypeError, ValueError):
            continue
        short = wall - reported
        if reported > wall + 1.0 or (short > WALL_CLOCK_SLACK_SECONDS
                                     and short > WALL_CLOCK_SLACK_SHARE * wall):
            out.append(
                f"run {record.id}: reports a {reported:.1f}s run, but `ar "
                f"measure` timed {wall:.1f}s around it -- the row's own clock "
                f"disagrees with core's (a patched timer inside the candidate "
                f"reads like this); inspect the candidate before trusting the "
                f"row")
    return out


def revision_warnings(config, limit: int = 10) -> list[str]:
    """Cell and slope closures that record no revision (#34).

    Those are the closures whose reach is bounded -- "refuted at this width",
    "on this slope" -- so they are the ones a benchmark move can invalidate.
    One that names no revision, in its pin or its declared applicability,
    cannot be listed by `ar epochs` when the source moves. One line, however
    many: a corpus closed before the stamp has hundreds."""
    from .entries import Store
    missing = []
    for entry in Store(config.paths.entries).read_all()[0]:
        result = entry.result
        if result is None or result.closure_kind not in ("cell", "slope"):
            continue
        declared = result.applicability.source_revision if result.applicability else ""
        if (result.pin or {}).get("source_revision") or declared:
            continue
        missing.append(entry.id)
    if not missing:
        return []
    shown = ", ".join(missing[:limit]) + (" ..." if len(missing) > limit else "")
    return [f"{len(missing)} cell/slope closure(s) record no source revision "
            f"({shown}) -- a benchmark move cannot say whether they still "
            f"hold; name it with `ar close ... --source-revision` when closing"]


def epoch_warnings(config) -> list[str]:
    """One line when rows from a superseded epoch are in the ledger (#36,
    #40): they no longer compete for best, and closures decided on them may
    need a second look."""
    from . import epochs
    from .runs import read_all
    try:
        listed = epochs.segment(config, read_all(config.paths.runs))
    except Exception:                   # a failing revision command is named elsewhere
        return []
    old = [e for e in listed if not e.current and e.rows]
    if not old or not any(e.current for e in listed):
        return []
    count = sum(len(e.rows) for e in old)
    return [f"{count} row(s) were measured under a superseded evaluator or "
            f"benchmark ({len(old)} earlier epoch(s)); they no longer compete "
            f"for best -- `ar epochs` lists closures that may need reopening"]


def git_warnings(config, limit: int = 15) -> list[str]:
    """Entry and run files the last commit does not hold (#25).

    provably.fast found 13 entry files from a night's measurements untracked
    while `validate` read clean: the record validated was not the record a
    clone would get. Named in one warning, never a problem -- mid-session the
    working tree is legitimately ahead of the last commit. Gitignored paths
    are the domain's choice and are not named; outside git there is nothing
    to say."""
    import subprocess
    root = config.paths.root
    scopes = [str(p.relative_to(root)) for p in (config.paths.entries,
                                                 config.paths.runs)
              if p.is_relative_to(root)]

    def git(*args):
        try:
            proc = subprocess.run(["git", *args, "--", *scopes], cwd=root,
                                  capture_output=True, text=True)
        except OSError:              # no git on PATH: nothing to say
            return None
        return proc.stdout.splitlines() if proc.returncode == 0 else None

    untracked = git("ls-files", "--others", "--exclude-standard")
    if untracked is None or not scopes:
        return []
    modified = git("ls-files", "--modified") or []
    staged = git("diff", "--cached", "--name-only", "--relative") or []
    names = sorted(set(untracked) | set(modified) | set(staged))
    if not names:
        return []
    shown = ", ".join(names[:limit]) + (" ..." if len(names) > limit else "")
    return [f"{len(names)} record file(s) are not committed ({shown}) -- the "
            f"record validated here is not the one a clone gets; commit them "
            f"(or gitignore what is meant to stay local)"]


def _normalised(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", (text or "").lower()).strip().rstrip(".!?;:,")


def duplicate_warnings(config, limit: int = 15) -> list[str]:
    """Closed entries that are the same claim twice (#35).

    qsb's dupe sweep found 54 duplicate claim pairs, 10 with contradictory
    terminals: one closed `confirmed`, its twin `refuted`, and a reader shown
    either one. Two closures on one track are duplicates when their titles
    or their hypotheses match once case, spacing and trailing punctuation
    are set aside. Named in one warning; the pair marked contradictory is
    the one to settle first (`ar close ... superseded --superseded-by`)."""
    from .entries import Store
    from .errors import AutoresearchError
    groups: dict = {}
    for entry in Store(config.paths.entries).read_all()[0]:
        if entry.result is None:
            continue
        try:
            if not config.track_for(entry.id).machine.status(entry.result.verdict).terminal:
                continue
        except AutoresearchError:
            continue
        if (entry.result.verdict == "superseded"
                or entry.result.disposition == "superseded"):
            continue                  # already retired: the remedy was applied
        for field in ("title", "hypothesis"):
            key = _normalised(getattr(entry, field, ""))
            if key:
                groups.setdefault((entry.track, field, key), []).append(entry)
    pairs = {}
    for members in groups.values():
        members = sorted(members, key=lambda e: (e.prefix, e.number))
        for i, first in enumerate(members):
            for second in members[i + 1:]:
                pairs[(first.id, second.id)] = (first.result.verdict,
                                                second.result.verdict)
    if not pairs:
        return []
    shown = []
    for (a, b), (va, vb) in sorted(pairs.items(), key=lambda kv: kv[1][0] == kv[1][1]):
        shown.append(f"{a}/{b}" + (f" (contradictory: {va} vs {vb})" if va != vb else ""))
    listed = ", ".join(shown[:limit]) + (" ..." if len(shown) > limit else "")
    return [f"{len(pairs)} duplicate closure pair(s): {listed} -- the same claim "
            f"closed twice; retire one: `ar close ID --reopen`, claim it, then "
            f"`ar close ID superseded --superseded-by OTHER`"]


def evaluator_warnings(config) -> list[str]:
    """A measure command that names no script in the domain -- `python -m
    bench`, `uv run bench` -- has no evaluator for #36 to protect: a worker's
    edited copy in its slot is what runs. Said once, with the remedy."""
    from . import evaluator
    if "measure" not in config.commands or evaluator.files(config):
        return []
    return [f"the measure command ({config.commands['measure']!r}) names no "
            f"script in the domain, so nothing protects the evaluator from a "
            f"worker's slot; declare `[evaluator] files` (and, for a module "
            f"or entry point, run it by path)"]


def holdout_warnings(config) -> list[str]:
    """A committed holdout is not held out (#38): every worktree slot can
    `git show HEAD:<path>` it, however its working copy was stripped."""
    from . import holdout
    tracked = holdout.tracked_files(config)
    if not tracked:
        return []
    return [f"holdout file(s) committed to git: {', '.join(tracked)} -- any "
            f"worktree can read them with `git show`; remove them from the "
            f"index (`git rm --cached`) and gitignore them"]


def core_warnings(config) -> list[str]:
    """Warning (not a problem) when the installed core is below the domain's
    declared `min_core` floor.

    A stale copied install resurrects fixed defects (the first field domain
    verified its install by hand, md5 against the checkout). `ar doctor`
    makes it a failure; `ar validate` only names it, because validate's
    contract is that convention nudges never change the exit code."""
    from .upstream import min_core_problem
    problem = min_core_problem(config)
    return [problem + " (run `ar doctor` for the full diagnosis)"] \
        if problem else []


def citation_warnings(config, limit: int = 20) -> list[str]:
    """Run ids cited in memos and entries that have no row in the ledger.

    A phantom id travelled memo -> entry history -> another entry's hypothesis
    with nothing checking it (eip8200 H16/H22/H23, ecdsa H167). Prose has no
    citation syntax, so this reads the ledger's own id shape: when every row id
    is fixed-width hex, a token of that shape within a line's reach of the word
    "run" is a citation.
    """
    import re
    ids = set()
    for path in pathlib.Path(config.paths.runs).glob("**/*.jsonl"):
        ids.update(re.findall(r'"id":\s*"([^"]+)"', path.read_text(errors="replace")))
    widths = {len(i) for i in ids}
    # ponytail: shape heuristic; a domain with non-hex or mixed-width ids gets
    # no check until runs carry a citation syntax
    if not ids or len(widths) != 1 or not all(re.fullmatch(r"[0-9a-f]+", i) for i in ids):
        return []
    width = widths.pop()
    if width < 8:
        return []
    # the id(s) directly after "run"/"runs", as a list: `runs X and Y`, `run
    # `X`` -- not any hex later on the line (a patch sha is not a run)
    hexid = rf"`?[0-9a-f]{{{width}}}\b`?"
    cite = re.compile(rf"\bruns?\b(?:\s+ids?)?[\s:(]*{hexid}"
                      rf"(?:\s*(?:,|and|/|&)\s*{hexid})*")
    token = re.compile(rf"\b[0-9a-f]{{{width}}}\b")
    out = []
    root = pathlib.Path(config.paths.root)
    runs = pathlib.Path(config.paths.runs)
    ledger = runs.relative_to(root) if runs.is_relative_to(root) else runs
    sources = [*pathlib.Path(config.paths.memos).rglob("*.md"),
               *pathlib.Path(config.paths.entries).glob("*.yaml")]
    for path in sorted(p for p in sources if p.is_file()):
        text = path.read_text(errors="replace")
        missing = sorted({t for m in cite.finditer(text)
                          for t in token.findall(m.group()) if t not in ids})
        if missing:
            shown = path.relative_to(root) if path.is_relative_to(root) else path
            out.append(f"{shown} cites run(s) {', '.join(missing)} with no "
                       f"row in {ledger}")
    if len(out) > limit:
        out = out[:limit] + [f"... and {len(out) - limit} more file(s) citing unrecorded runs"]
    return out
