"""The findings/scaffolding boundary, default-deny, read from one place.

Two lanes, decided by path rather than by judgement. Research output lands
immediately so the next agent sees it; a change to the harness changes how every
later session works, so it goes out as a proposal. The original harness got the
important part right and said so in its config: both the publisher and the
stager read the boundary from the same file "so they cannot disagree about which
lane a path is in."

It also recorded what happens when the regex is wrong. `bin/harvest` wrote to
`data/artifacts/`, which the regex classified as scaffolding, so a harvested
dump could be neither published nor removed -- the pre-commit hook refused to
drop it because a run row backed it, and publish refused the branch as mixed
(H51). The lane boundary is load-bearing, and a missing prefix deadlocks the
findings lane.

Hence: default-deny (anything unmatched is scaffolding and needs review), one
reader, and `explain()` so a refusal says which rule decided and what to do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ConfigError

FINDINGS, SCAFFOLDING = "findings", "scaffolding"


@dataclass
class Lanes:
    findings_pattern: str

    def __post_init__(self):
        try:
            self._re = re.compile(self.findings_pattern)
        except re.error as exc:
            raise ConfigError(
                f"lanes.findings is not a valid regex: {self.findings_pattern!r} ({exc})"
            ) from exc

    def classify(self, path: str) -> str:
        return FINDINGS if self._re.match(str(path).lstrip("./")) else SCAFFOLDING

    def split(self, paths) -> dict[str, list[str]]:
        out = {FINDINGS: [], SCAFFOLDING: []}
        for p in paths:
            out[self.classify(p)].append(str(p))
        return out

    def is_mixed(self, paths) -> bool:
        buckets = self.split(paths)
        return bool(buckets[FINDINGS]) and bool(buckets[SCAFFOLDING])

    def explain(self, paths) -> str:
        buckets = self.split(paths)
        lines = [f"lane boundary: findings = /{self.findings_pattern}/ (default-deny)"]
        for lane in (FINDINGS, SCAFFOLDING):
            shown = buckets[lane][:12]
            more = len(buckets[lane]) - len(shown)
            lines.append(f"  {lane:12} {len(buckets[lane]):4d} path(s)"
                         + (f": {', '.join(shown)}" if shown else "")
                         + (f" (+{more} more)" if more > 0 else ""))
        if self.is_mixed(paths):
            lines.append("  MIXED -- a branch may not carry both lanes. Move the "
                         "scaffolding half to a separate branch and publish it "
                         "as a proposal; never park a finding behind a review.")
        return "\n".join(lines)
