#!/usr/bin/env python3
"""List verdicts closed soon after they were filed, for an audit pass.

The time from filing to first closure is the one closure-time feature found to
predict a weak verdict. In qsb (1,295 first closures, 28 later reopened because
the verdict did not survive, labelled by hand and checked by a blind second
labelling at kappa 0.95), it ranked weak verdicts at AUC 0.78, stable at
0.73-0.84 with any one closing session held out. Closed within an hour of filing
flagged 24% of closures and held 20 of the 28. A fitted model over twenty
closure-time features did no better and collapsed when the largest session was
held out, so this is a rule, not a model.

    python scripts/audit-fast-closures.py /path/to/domain [--hours 1] \\
        [--track research] [--labels reopens.tsv]

It prints two things:

  back-test  how the rule did on this domain's own reopens, wherever a reopen
             says why (`ar close --reopen --reopen-reason`, or --labels for a
             record that predates the flag: a TSV with id, history_index and
             label columns, label one of weak/overclaim/context/clerical)
  queue      every verdict first closed inside --hours of filing and never
             reopened since, fastest first

The label is "weak and caught", not "weak": most of the qsb weak verdicts were
reopened within hours by a curator pass, so an unreopened fast closure is
unreviewed, not cleared. Times are the entry's own (`created`, history),
never commit times. It reads; it writes nothing.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from autoresearch.config import DomainConfig, discover  # noqa: E402
from autoresearch.errors import AutoresearchError  # noqa: E402
from autoresearch.states import REOPEN_REASONS  # noqa: E402

LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _time(value):
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _edge(event):
    kind = str(event.get("kind", ""))
    return kind.split("->", 1) if "->" in kind else (None, None)


def load_labels(path):
    """{(id, history_index): label} from a TSV with a header row."""
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    missing = {"id", "history_index", "label"} - set(rows[0] if rows else {})
    if missing:
        raise SystemExit(f"--labels {path}: missing column(s) {sorted(missing)}")
    return {(r["id"], int(r["history_index"])): r["label"].strip() for r in rows}


def closures(config, track, labels):
    """One row per entry whose first terminal transition was a verdict."""
    machine = config.tracks[track].machine
    terminal = machine.terminal_names
    # Retirement is not a verdict: `superseded` names a replacement and
    # `wontfix` is how a duplicate or a declined defect leaves the queue.
    verdicts = terminal - {"superseded", "wontfix"}
    rows = []
    for path in sorted(config.paths.entries.glob("*.yaml")):
        doc = yaml.load(path.read_bytes(), Loader=LOADER)
        if not isinstance(doc, dict) or doc.get("track") != track:
            continue
        history = [e for e in doc.get("history") or [] if isinstance(e, dict)]
        first = next((i for i, e in enumerate(history) if _edge(e)[1] in terminal), None)
        if first is None or _edge(history[first])[1] not in verdicts:
            continue
        reopen = next((i for i in range(first + 1, len(history))
                       if _edge(history[i])[0] in terminal), None)
        reason = None
        if reopen is not None:
            reason = ((history[reopen].get("snapshot") or {}).get("reopen_reason")
                      or labels.get((str(doc["id"]), reopen)))
        created, closed = _time(doc.get("created")), _time(history[first].get("at"))
        hours = (closed - created) / 3600 if created and closed else None
        rows.append({
            "id": str(doc["id"]), "status": doc.get("status"),
            "verdict": _edge(history[first])[1], "closer": history[first].get("who"),
            "hours": hours if hours is not None and hours >= 0 else None,
            "reopened": reopen is not None, "reopen_reason": reason,
            "closure_kind": (doc.get("result") or {}).get("closure_kind") or "",
            "title": str(doc.get("title") or ""),
        })
    return rows


def backtest(rows, hours) -> list[str]:
    timed = [r for r in rows if r["hours"] is not None]
    labelled = [r for r in timed if r["reopen_reason"]]
    if not labelled:
        return ["back-test: no reopen in this record says why yet "
                "(`ar close --reopen --reopen-reason`, or --labels); nothing to test"]
    weak = [r for r in timed if r["reopen_reason"] == "weak"]
    flagged = [r for r in timed if r["hours"] < hours]
    caught = [r for r in flagged if r["reopen_reason"] == "weak"]
    reasons = {k: sum(r["reopen_reason"] == k for r in labelled) for k in REOPEN_REASONS}
    out = [f"back-test over {len(timed)} timed first closures; reopens by reason: "
           + ", ".join(f"{k} {v}" for k, v in reasons.items())]
    if not weak:
        return out + ["  no weak verdict among them: nothing to test the rule on"]
    base = len(weak) / len(timed)
    precision = len(caught) / len(flagged) if flagged else 0.0
    out.append(f"  closed within {hours:g} h: flags {len(flagged)} "
               f"({100 * len(flagged) / len(timed):.0f}%), holds {len(caught)} of "
               f"{len(weak)} weak; weak rate {100 * precision:.1f}% flagged vs "
               f"{100 * base:.1f}% overall")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("domain", nargs="?", default=".", type=pathlib.Path)
    ap.add_argument("--track", default="research")
    ap.add_argument("--hours", type=float, default=1.0,
                    help="flag verdicts first closed within this many hours "
                         "of filing (default: 1)")
    ap.add_argument("--labels", type=pathlib.Path,
                    help="hand labels for reopens that predate --reopen-reason")
    args = ap.parse_args()
    try:
        config = DomainConfig.load(discover(args.domain.resolve()))
    except (AutoresearchError, OSError, ValueError, TypeError, KeyError,
            AttributeError, yaml.YAMLError) as exc:
        ap.error(f"cannot load domain at {args.domain}: {exc}")
    if args.track not in config.tracks:
        ap.error(f"unknown track {args.track!r}; declared tracks: {sorted(config.tracks)}")
    labels = load_labels(args.labels) if args.labels else {}

    rows = closures(config, args.track, labels)
    print("\n".join(backtest(rows, args.hours)))
    queue = sorted((r for r in rows if not r["reopened"] and r["hours"] is not None
                    and r["hours"] < args.hours), key=lambda r: r["hours"])
    untimed = sum(r["hours"] is None for r in rows)
    print(f"\nqueue: {len(queue)} verdict(s) first closed within {args.hours:g} h "
          f"of filing and never reopened ({len(rows)} first closures, "
          f"{untimed} untimed)")
    for r in queue:
        print(f"  {r['id']:<7} {r['status']:<10} {60 * r['hours']:5.0f} min  "
              f"{r['closer'] or '?':<24} {r['closure_kind']:<9} {r['title'][:70]}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
