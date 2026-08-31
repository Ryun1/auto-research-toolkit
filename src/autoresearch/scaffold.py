"""`ar init` -- scaffold a new research domain.

The four things a domain owes the core are easy to describe and fiddly to write
from scratch, and a half-written domain fails at load rather than at use. So
this writes a complete, *valid, immediately runnable* domain: `ar validate`
passes on it, `ar board` runs, and the measurement command works before you have
edited anything.

That last property is the point. A scaffold whose first act is to fail is a
scaffold that teaches you to ignore the validator. The generated `bin/measure`
measures something real but trivial, so the loop is exercisable from minute one
and you replace it with the real experiment when you have one.

It also refuses to overwrite. Scaffolding over a live domain would destroy the
records, and "it seemed empty" is not a check.
"""
from __future__ import annotations

import pathlib
import textwrap

from .errors import ConfigError

DIRS = ("bin", "guides", "docs/log", "docs/skills", "inbox",
        "state/entries", "state/claims", "state/iterations", "data/runs")


def _domain_toml(name: str, objective: str, metrics: list[str]) -> str:
    metric_block = "\n".join(
        f'  {m}:\n    field: {m}\n    direction: minimise' for m in metrics)
    return textwrap.dedent(f'''\
        # {name} -- an `autoresearch` domain.
        #
        # A domain supplies four things and the core owns the rest:
        #   1. measurement      bin/measure
        #   2. goal + constants goal.yaml
        #   3. safety policy    [policy] below
        #   4. knowledge        guides/, listed in `knowledge`, plus the
        #                       skills the loop distils under [skills]

        knowledge = ["guides/landscape.md"]

        # Distilled skills: one directory each, cited to the entries that back
        # them, checked by `ar skill check` and by `ar validate`. Written by the
        # loop's distil phase and by `ar skill distil`; read by every role, as a
        # name-and-description index rather than inlined prose.
        [skills]
        dir       = "docs/skills"
        max_lines = 500

        [domain]
        name = "{name}"
        description = "TODO: one line on what winning looks like."
        goal = "goal.yaml"

        [state]
        entries    = "state/entries"
        claims     = "state/claims"
        runs       = "data/runs"
        memos      = "inbox"
        iterations = "state/iterations"

        [[tracks]]
        id     = "research"
        prefix = "Q"
        title  = "Hypothesis Queue"
        view   = "docs/log/Hypothesis Queue.md"
        description = "Leads on the score. Nothing here is about the harness."

        [[tracks]]
        id     = "harness"
        prefix = "H"
        title  = "Harness Debt"
        view   = "docs/log/Harness Debt.md"
        description = "The scaffolding's own defects, kept separate so an agent booting into research never reads them as a lead on the score."
        # A defect here must carry `core`, `repro` and `observed`, so it can be
        # reproduced by someone who has never seen this project. `ar validate`
        # refuses an entry missing them; `ar harness export` refuses to publish
        # one -- naming the missing field -- until `ar entry amend` completes it.
        requires_defect_evidence = true
        [tracks.terminal]
        fixed   = {{ requires_evidence = true }}
        wontfix = {{ requires_evidence = true }}
        refuted = {{ requires_evidence = true, requires_closure_kind = true }}

        [lanes]
        # Default-deny: anything not matching is scaffolding and takes review.
        findings = "^(data/runs|data/artifacts|inbox|docs|state/entries|state/iterations)/"

        [policy]
        # Above this, a person approves. Rented compute is charged here.
        spend_ceiling = 0.0
        currency = "USD"

        # Defects found in the core itself are recorded here and exported with
        # `ar harness export`; a person publishes the bundle upstream. An agent
        # may prepare it, never take the publishing step.
        [[policy.human_only]]
        pattern = "gh issue create"
        reason  = "filing upstream is public and irreversible; run `ar harness export`, prepare the issue body, and hand both to a person"
        example = "gh issue create -R Ryun1/auto-research-toolkit --title defect --body '...'"

        # Declare anything else an agent must never do. Every rule states WHY, and
        # `ar policy` proves each one refuses something -- a rule whose
        # enforcement can be deleted without a test failing is a comment.
        # [[policy.human_only]]
        # pattern = "mytool submit"
        # reason  = "irreversible and public; an agent may prepare it, never run it"
        # example = "mytool submit"

        [budgets]
        claim_wall_clock_hours = 4
        claim_max_runs         = 12
        claim_ttl_hours        = 6
        iteration_fanout       = 3
        iteration_max_spawns   = 10
        iteration_max_seconds  = 3600
        domain_max_runs        = 500

        [commands]
        measure = "bin/measure"
        # probe_target = "bin/probe-target"   # only if your target moves

        [coordinator]
        workers_per_iteration    = 3
        generators_per_iteration = 2
        max_parallel             = 3
        # Share of each shortlist reserved for the largest `impact`, ignoring
        # confidence and cost. The score is expected value per unit cost, which
        # is risk-neutral, so without a reserve a cheap certain increment always
        # beats an honest long shot and the loop never attempts a big swing.
        # Raise it while the frontier is moving; 0 disables it entirely.
        explore_fraction         = 0.2
        # Distil closed work into skills every N iterations. Not every one:
        # a librarian asked to distil after a single verdict writes a skill
        # that says what one entry already says. 0 turns distillation off.
        distil_every             = 5
        ''').replace("{metric_block}", metric_block)


GOAL_TEMPLATE = """# The goal, typed. Derived values are EXPRESSIONS, never stored numbers:
# a stored copy goes stale and nothing notices.
goal:
  id: beat-baseline
  description: >
    TODO: what winning means for __NAME__, in one or two sentences.
  direction: minimise

  metrics:
__METRICS__

  objective: "__OBJECTIVE__"

  # derived:
  #   ratio: "__FIRST__ / __LAST__"

  target:
    value: __TARGET__
    # Or, if your target moves (a leaderboard, a competitor, a frontier):
    # source: bin/probe-target
    # moving: true
    # refresh_seconds: 1800

  stop_when: "objective < target"

  # The third exit: stop when the loop stops learning, not only when a human
  # notices.
  yield_floor:
    confirmed_per_iteration: 0.15
    over_iterations: 10
"""


def _goal_yaml(name: str, objective: str, metrics: list[str], target: float) -> str:
    """Fill the template by substitution, never by f-string interpolation into
    an indented block.

    An f-string indents only the FIRST line of a multi-line substitution, so
    interpolating a metric block into an indented template silently produces
    invalid YAML from line two onward. The first version of this did exactly
    that, and `ar init` shipped a domain that could not be loaded -- the one
    thing a scaffold must never do."""
    block = "\n".join(
        f"    {m}:\n      field: {m}\n      direction: minimise" for m in metrics)
    return (GOAL_TEMPLATE
            .replace("__NAME__", name)
            .replace("__METRICS__", block)
            .replace("__OBJECTIVE__", objective)
            .replace("__FIRST__", metrics[0])
            .replace("__LAST__", metrics[-1])
            .replace("__TARGET__", repr(target)))


MEASURE = '''#!/usr/bin/env python3
"""One experiment, one validated record on stdout.

REPLACE THE `evaluate` FUNCTION with your real experiment. Everything else is
the contract the core depends on:

  * emit ONE JSON object on stdout, the last line
  * `metrics` carries every metric goal.yaml declares
  * `provenance` identifies what was actually run -- a measurement that cannot
    be reproduced is not evidence
  * status "invalid" when the run did not measure the thing (never "ok" with
    zeroes -- that scores as a perfect result)

The core decides where the row lands and what makes it valid; you decide how to
measure. Run it directly to check: `bin/measure --knob x=2`
"""
import argparse, hashlib, json, os, platform, sys, time, uuid

DEFAULTS = {"x": 1}


def evaluate(knobs):
    """REPLACE ME. Must return a dict with one entry per declared metric."""
    x = max(1, int(knobs["x"]))
    return {"cost": round(1000.0 / x + 10.0 * x, 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--knob", action="append", default=[], metavar="K=V")
    ap.add_argument("--session", default=os.environ.get("AR_SESSION", "unknown"))
    ap.add_argument("--entry", default=os.environ.get("AR_ENTRY"))
    args = ap.parse_args()

    knobs = dict(DEFAULTS)
    for item in args.knob:
        if "=" not in item:
            sys.exit(f"--knob wants K=V, got {item!r}")
        key, value = item.split("=", 1)
        if key not in DEFAULTS:
            # An unrecognised knob must never read as a successful run of the
            # defaults -- that failure mode looks exactly like a clean null.
            sys.exit(f"unknown knob {key!r}; known: {sorted(DEFAULTS)}")
        knobs[key] = int(value)

    started = time.time()
    metrics = evaluate(knobs)
    json.dump({
        "schema": "ar-run-1", "id": uuid.uuid4().hex[:12],
        "session": args.session, "entry": args.entry,
        "status": "ok" if all(v > 0 for v in metrics.values()) else "invalid",
        "started": started, "finished": time.time(),
        "metrics": metrics, "config": {"knobs": knobs},
        "provenance": {
            "host": platform.node(), "python": platform.python_version(),
            "measure_sha": hashlib.sha256(open(__file__, "rb").read()).hexdigest()[:12],
        },
        "outputs": [], "cost": {"seconds": time.time() - started}, "notes": "",
    }, sys.stdout, sort_keys=True)
    sys.stdout.write("\\n")


if __name__ == "__main__":
    main()
'''

GUIDE = '''# What is known about this problem

Domain knowledge the generator and worker agents read before forming hypotheses.
Keep it measured, and keep the closed directions here -- this file is how the
next agent avoids re-running work the board already paid for.

## The knobs

| Knob | Range | Effect |
|---|---|---|
| `x` | 1-100 | TODO |

## Closed directions

None yet. As entries close, record the ones whose refutation was `mechanism`
here: those hold outside the range measured and should never be re-proposed.

## Open questions

- TODO
'''


SKILLS_README = """# Skills

One directory per skill, each holding a `SKILL.md`. This directory starts empty:
a skill is *distilled from closed work*, and nothing has closed yet.

```
docs/skills/<name>/SKILL.md
```

```markdown
---
name: width-validity-gate
description: "Use when a proposal touches `width` or a run comes back unscored:
  'invalid', validity gate, rows that vanish from a sweep."
cites: [Q7, Q12]
distilled: {at: 2026-08-31T09:12:04Z, iteration: 14, core: 0.1.0}
---

# Width below 16 does not trade -- it deletes the run

Below 16 the run is `invalid` and does not score [Q7]. It is a gate, not a
knob: there is no band in which paying width buys operations back [Q12].
```

Three rules, each enforced by `ar skill check` and by `ar validate`:

- **The description is triggering conditions only**, opening `Use when`. It is
  the entire routing layer -- every role's brief carries the descriptions and
  not the bodies, so an agent reads the one skill that matches instead of all of
  them. A description that summarises the procedure gets followed *instead of*
  the skill.
- **Every claim cites the entry that earned it**, inline and in `cites`. A skill
  is prose, and prose is the one thing in this harness that cannot be
  regenerated from the record -- the citation is what stands in for that.
- **A citation that moves invalidates the skill.** Reopen or relabel a cited
  entry and validation fails until the skill is re-distilled or retired. A skill
  that outlived its evidence is worse than no skill. This is measured from
  `distilled.at`, so a hand-written skill must carry one -- without it there is
  no "since", and the check would pass by never running. `ar skill distil`
  stamps it for you.

Write one with `ar skill distil`, or by hand. `ar skill list` shows what exists,
what is stale, and how many closed entries no skill cites yet.
"""


GITIGNORE = '''# Per-machine state. Stopping work on one machine and resuming on another is a
# supported workflow; these two are exactly what must NOT travel with it.
#
# state/claims/ holds the claim lock. It names a holder and a pid that mean
# nothing on the other machine, so a committed lock buys a stall until it goes
# stale (claim_lock_stale_seconds) and a steal notice naming a session that was
# never running here -- plus a merge conflict on every handoff, over a file
# whose whole purpose is local mutual exclusion.
state/claims/

# .ar/ holds the workspace pool: markers recording absolute paths that exist on
# one machine only. The coordinator destroys its own slots in a `finally`;
# anything left here after a crash is cleared with:
#     git worktree prune
#     git branch --list 'ar/*' | xargs -n1 git branch -D
.ar/
'''


def init(root, name: str, objective: str = "cost", metrics=("cost",),
         target: float = 100.0, force: bool = False) -> list[pathlib.Path]:
    root = pathlib.Path(root).resolve()
    config = root / "domain.toml"
    if config.exists() and not force:
        raise ConfigError(
            f"{config} already exists. `ar init` refuses to scaffold over a "
            f"live domain -- it would destroy the records. Use --force only on "
            f"a directory you are certain is disposable.")

    written = []
    for d in DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)

    metrics = list(metrics)
    files = {
        "domain.toml": _domain_toml(name, objective, metrics),
        "goal.yaml": _goal_yaml(name, objective, metrics, target),
        "bin/measure": MEASURE,
        "guides/landscape.md": GUIDE,
        "docs/skills/README.md": SKILLS_README,
    }
    for rel, content in files.items():
        path = root / rel
        path.write_text(content)
        written.append(path)

    # `.gitignore` is appended, never written over. Every other file here
    # belongs to the domain being scaffolded, but this one routinely exists
    # already in a directory someone is adopting -- and what it protects is
    # exactly the class of path `[policy] forbidden_paths` exists for. The
    # `domain.toml` guard does not cover it: a directory with a `.gitignore`
    # and no `domain.toml` is the normal case, not the refused one.
    ignore = root / ".gitignore"
    existing = ignore.read_text() if ignore.exists() else ""
    if "state/claims/" not in existing:
        joiner = "" if not existing or existing.endswith("\n\n") else (
            "\n" if existing.endswith("\n") else "\n\n")
        ignore.write_text(existing + joiner + GITIGNORE)
        written.append(ignore)
    (root / "bin" / "measure").chmod(0o755)
    return written
