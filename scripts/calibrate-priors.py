#!/usr/bin/env python3
"""Check whether a domain's priors predict its verdicts, from git history.

The ranking multiplies `confidence x impact / cost`, numbers the filing agent
guessed. This script asks whether those guesses were any good: for each prior,
how well it separates entries that closed `confirmed` from entries that closed
`refuted`, measured as AUC (the chance a confirmed entry's value beats a
refuted one's; 0.5 is no signal, below 0.5 points the wrong way).

The entry file alone cannot answer that. A re-price records only its reason,
and an amend can rewrite a prior after the verdict, so the stored numbers may
know the outcome. Every domain commits its entry store, so this replays git
history and scores each prior at three points:

    filed        the first committed version
    pre_verdict  the last committed version before the first terminal status
    final        the working tree

An entry created and closed in one commit has no pre_verdict version; it is
counted as born closed and left out of the scores, never silently dropped.

    python scripts/calibrate-priors.py /path/to/domain [more domains ...]
    python scripts/calibrate-priors.py . --track research --jsonl rows.jsonl

The report also splits the filed priors by how long each entry waited for its
verdict (from the entry's own `created` and history timestamps -- commit
times trail them, by hours at worst), and pools the AUC within each filing
session, which separates "agents rank well within a batch" from "some batches
were weak". It reads; it writes nothing but the optional --jsonl.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import pathlib
import random
import subprocess
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from autoresearch.config import DomainConfig, discover  # noqa: E402
from autoresearch.errors import AutoresearchError  # noqa: E402

LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
PRIORS = ("confidence", "impact", "cost")
SNAPSHOTS = ("filed", "pre_verdict", "final")
WAITS = ((0, 3600, "<1h"), (3600, 86400, "1h-1d"), (86400, float("inf"), ">1d"))


def git(repo, *args, stdin=None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin,
                          capture_output=True, check=True).stdout


def entry_versions(entries_dir: pathlib.Path) -> dict[str, list[dict]]:
    """Every committed version of every entry, oldest first, keyed by the id
    inside the file: entries move between paths, ids do not."""
    repo = pathlib.Path(git(entries_dir, "rev-parse", "--show-toplevel").decode().strip())
    rel = entries_dir.resolve().relative_to(repo.resolve()).as_posix()
    log = git(repo, "log", "--reverse", "--no-renames", "--format=C %H",
              "--name-only", "--", rel + "/").decode()
    specs = []
    sha = None
    for line in log.splitlines():
        if line.startswith("C "):
            sha = line[2:].strip()
        elif line.endswith(".yaml"):
            specs.append(f"{sha}:{line}")
    if not specs:
        return {}
    out = git(repo, "cat-file", "--batch", stdin=("\n".join(specs) + "\n").encode())
    versions: dict[str, list[dict]] = {}
    i = 0
    for _ in specs:
        nl = out.index(b"\n", i)
        header = out[i:nl].split()
        if header[-1] == b"missing":  # deleted in this commit
            i = nl + 1
            continue
        size = int(header[2])
        doc = _parse(out[nl + 1:nl + 1 + size])
        i = nl + 1 + size + 1
        if doc:
            versions.setdefault(str(doc["id"]), []).append(doc)
    return versions


def _parse(blob):
    try:
        doc = yaml.load(blob, Loader=LOADER)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) and doc.get("id") else None


def _time(value):
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _priors(doc):
    return {k: doc.get(k) for k in PRIORS} if doc else None


def replay(config: DomainConfig, track: str) -> list[dict]:
    """One row per entry on `track` in the working tree."""
    terminal = set().union(*(t.machine.terminal_names for t in config.tracks.values()))
    versions = entry_versions(config.paths.entries)
    rows = []
    for path in sorted(config.paths.entries.glob("*.yaml")):
        final = _parse(path.read_bytes())
        if not final or final.get("track") != track:
            continue
        seen = versions.get(str(final["id"]), [])
        first = next((i for i, d in enumerate(seen) if d.get("status") in terminal), None)
        if first is None:
            pre = seen[-1] if seen else None
        else:
            pre = seen[first - 1] if first > 0 else None
        history = [e for e in final.get("history") or [] if isinstance(e, dict)]
        verdict_event = next((e for e in history
                              if "->" in str(e.get("kind", ""))
                              and str(e["kind"]).split("->")[1] in terminal), None)
        created, decided = _time(final.get("created")), _time((verdict_event or {}).get("at"))
        rows.append({
            "domain": config.paths.root.name,
            "id": final["id"],
            "status": final.get("status"),
            "first_verdict": seen[first].get("status") if first is not None else None,
            "session": history[0].get("who") if history else None,
            "versions": len(seen),
            "uncommitted": not seen,
            "born_closed": first == 0,
            "verdict_without_event": first is not None and verdict_event is None,
            "wait_seconds": decided - created if created and decided else None,
            "filed": _priors(seen[0] if seen else None),
            "pre_verdict": _priors(pre),
            "final": _priors(final),
        })
    return rows


def auc(pairs):
    """pairs: [(confirmed: bool, value)]. None when either class is empty."""
    pos = [v for y, v in pairs if y]
    neg = [v for y, v in pairs if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def interval(pairs, reps=300, seed=0):
    """2.5-97.5% bootstrap over entries."""
    rng = random.Random(seed)
    stats = sorted(a for a in (auc([rng.choice(pairs) for _ in pairs])
                               for _ in range(reps)) if a is not None)
    if not stats:
        return None
    return stats[int(0.025 * len(stats))], stats[int(0.975 * len(stats)) - 1]


def _cell(pairs):
    a = auc(pairs)
    if a is None:
        return "n/a"
    lo, hi = interval(pairs)
    return f"{a:.2f} [{lo:.2f},{hi:.2f}]"


def _pairs(rows, snapshot, prior):
    return [(r["first_verdict"] == "confirmed", r[snapshot][prior]) for r in rows
            if r[snapshot] and r[snapshot][prior] is not None]


def report(rows) -> list[str]:
    decided = [r for r in rows if r["first_verdict"] in ("confirmed", "refuted")]
    scored = [r for r in decided if r["pre_verdict"]]
    out = [f"{rows[0]['domain'] if rows else '?'}: {len(decided)} decided "
           f"({sum(r['first_verdict'] == 'confirmed' for r in decided)} confirmed), "
           f"{sum(r['born_closed'] for r in decided)} born closed, "
           f"{sum(r['uncommitted'] for r in rows)} never committed, "
           f"{sum(r['verdict_without_event'] for r in rows)} verdict(s) with no "
           "transition in history"]
    if not scored:
        return out + ["  nothing to score: no decided entry has a pre-verdict version"]

    out.append(f"  AUC by snapshot, {len(scored)} entries:")
    for k in PRIORS:
        moved = sum(r["pre_verdict"][k] != r["final"][k] for r in scored)
        cells = "  ".join(f"{s} {_cell(_pairs(scored, s, k))}" for s in SNAPSHOTS)
        out.append(f"    {k:<10} {cells}  changed after verdict {moved}/{len(scored)}")

    timed = [r for r in scored if r["wait_seconds"] is not None and r["wait_seconds"] >= 0]
    out.append(f"  filed AUC by wait for a verdict ({len(scored) - len(timed)} untimed "
               "or negative wait left out):")
    for lo, hi, label in WAITS:
        bucket = [r for r in timed if lo <= r["wait_seconds"] < hi]
        if not bucket:
            continue
        rate = sum(r["first_verdict"] == "confirmed" for r in bucket) / len(bucket)
        cells = "  ".join(f"{k} {_cell(_pairs(bucket, 'filed', k))}" for k in PRIORS)
        out.append(f"    {label:<6} n={len(bucket):<5} confirmed {100 * rate:4.1f}%  {cells}")

    by_session = collections.defaultdict(list)
    for r in scored:
        by_session[r["session"]].append(r)
    out.append("  filed AUC pooled within filing sessions (weighted by pairs):")
    for k in PRIORS:
        num = den = used = 0
        for group in by_session.values():
            pairs = _pairs(group, "filed", k)
            a = auc(pairs)
            if a is not None:
                pos = sum(y for y, _ in pairs)
                weight = pos * (len(pairs) - pos)
                num, den, used = num + a * weight, den + weight, used + 1
        out.append(f"    {k:<10} {num / den:.2f} over {used} sessions with both outcomes"
                   if den else f"    {k:<10} n/a: no session has both outcomes")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("domains", nargs="*", default=["."], type=pathlib.Path)
    ap.add_argument("--track", default="research",
                    help="track to score (default: research)")
    ap.add_argument("--jsonl", type=pathlib.Path,
                    help="also write one replayed row per entry here")
    args = ap.parse_args()

    all_rows = []
    for domain in args.domains:
        try:
            config = DomainConfig.load(discover(domain.resolve()))
        except (AutoresearchError, OSError, ValueError, TypeError, KeyError,
                AttributeError, yaml.YAMLError) as exc:
            ap.error(f"cannot load domain at {domain}: {exc}")
        if args.track not in config.tracks:
            ap.error(f"{domain}: unknown track {args.track!r}; declared tracks: "
                     f"{sorted(config.tracks)}")
        try:
            rows = replay(config, args.track)
        except subprocess.CalledProcessError as exc:
            ap.error(f"{domain}: the entry store is not readable as git history "
                     f"({exc.stderr.decode().strip()})")
        print("\n".join(report(rows)) + "\n")
        all_rows += rows
    if args.jsonl:
        with open(args.jsonl, "w") as fh:
            for r in all_rows:
                fh.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
