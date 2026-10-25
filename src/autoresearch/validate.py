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
    out = []
    for entry in Store(config.paths.entries).all():
        if entry.result is None:
            continue
        if not config.track_for(entry.id).machine.status(
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
