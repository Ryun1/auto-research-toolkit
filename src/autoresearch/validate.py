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
