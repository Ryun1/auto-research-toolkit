"""`ar init` -- scaffold a new research domain.

The four things a domain owes the core are easy to describe and fiddly to write
from scratch, and a half-written domain fails at load rather than at use. So
this writes a complete, *valid, immediately runnable* domain: `ar validate`
passes on it, `ar board` runs, and the measurement command works before you have
edited anything.

It also writes `bin/ar` (H149): the toolkit's entry point is named `ar`, which
on macOS collides with `/usr/bin/ar`, the BSD archiver -- a bare `ar board`
there runs plausibly instead of failing. The shim probes the domain's venvs
for the real toolkit, then `python3 -m autoresearch`, and fails loudly naming
the remedy if neither carries it.

It also writes `AGENTS.md`: the toolkit's own docs live in the toolkit
repository, which an installed project does not contain, so the claim ->
measure -> close protocol an agent in a terminal must follow ships where
agents look first.

That last property is the point. A scaffold whose first act is to fail is a
scaffold that teaches you to ignore the validator. The generated `bin/measure`
measures something real but trivial, so the loop is exercisable from minute one
and you replace it with the real experiment when you have one.

It also refuses to overwrite. Scaffolding over a live domain would destroy the
records, and "it seemed empty" is not a check.
"""
from __future__ import annotations

import pathlib
import sys
import textwrap

from .config import DomainConfig
from .entries import Store
from .errors import ConfigError
from .render import write_views

DIRS = ("bin", "guides", "docs/log", "docs/skills", "inbox",
        "state/entries", "state/claims", "state/iterations", "data/runs")


def _domain_toml(name: str) -> str:
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
        # The hypothesis tree drawn from the records: a generated mermaid view
        # that renders in Obsidian or GitHub. Delete the line (or set it empty)
        # to turn the graph off; either way `ar validate` stops checking it.
        graph_view = "docs/log/Hypothesis Graph.md"
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

        [upstream]
        # Where the core comes from, for `ar harness check` (what changed
        # upstream?) and `ar harness update` (take it, validated, with
        # rollback). A fork or a mirror changes the url; pin `ref` to a tag to
        # make updates opt-in.
        url = "https://github.com/Ryun1/auto-research-toolkit"
        ref  = "main"

        # [challenge]
        # A public challenge's shared intel: its leaderboard, its published
        # measurement policy, what other solvers tried. Adds `ar challenge
        # pull|show|check|target` and a `challenge` block to the generator,
        # judge and scout briefs. The loop never fetches: `bin/probe-target`
        # can be one line -- `exec ar challenge target` -- and `Target`'s own
        # refresh TTL governs the cadence.
        # source          = "provablyfast"   # or "yukon"
        # url             = "https://provably.fast/data/index.json"
        # refresh_seconds = 1800

        [coordinator]
        workers_per_iteration    = 3
        generators_per_iteration = 2
        max_parallel             = 3
        # Share of each shortlist aimed at novel branches (entries with no
        # parent -- new territory) versus refining the incumbent (branches off
        # work already in the record). 0.5 is the neutral stance; raise it
        # while the frontier is moving, lower it once a direction is winning
        # and every slot is better spent deepening it.
        risk                     = 0.5
        # The tree. Branches are filed with a `parent`; these caps keep one
        # lineage from growing unbounded. 0 disables either cap.
        tree_max_depth           = 3
        tree_max_children        = 4
        # Prune stalled lineages: when this many of a parent's children have
        # closed `refuted` and none `confirmed`, its remaining children drop
        # out of ranking and a new child is refused at filing. 0 disables.
        # branch_stagnation       = 3
        # Valid run rows a `confirmed` experiment closure must leave in the
        # ledger before it is accepted; 2+ demands a replication run. 0 or 1
        # keeps the default contract.
        # confirm_runs            = 2
        # Distil closed work into skills every N iterations. Not every one:
        # a librarian asked to distil after a single verdict writes a skill
        # that says what one entry already says. 0 turns distillation off.
        distil_every             = 5
        ''')


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
    # With [challenge] declared, bin/probe-target can be one line:
    #   exec ar challenge target

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

AR = '''#!/usr/bin/env python3
"""Run the auto-research-toolkit's `ar` from this domain (H149).

    bin/ar <toolkit arguments...>

The toolkit installs its entry point as **`ar`**, which is also `/usr/bin/ar`,
the BSD archiver shipped with macOS -- and this domain's venv is gitignored, so
a fresh clone has no toolkit on PATH. Typing bare `ar board` there runs the
archiver, which answers with a plausible-looking usage message about archive
files and leaves the agent debugging the wrong harness. This shim makes the
toolkit reachable the way every other tool here is (`bin/<name>`), and
`bin/ar` cannot collide with the archiver.

Probes this domain's venvs -- `.venv312/bin/ar` first, the toolkit needs
Python >= 3.11 and many machines ship an older system `python3` -- then
`.venv/bin/ar`, and execs the first found with all arguments. Otherwise it
falls back to `python3 -m autoresearch`; if no interpreter has the module the
failure is LOUD and names the remedy, never a quiet fall-through to the
archiver.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANDIDATES = (".venv312/bin/ar", ".venv/bin/ar")


def main():
    for candidate in CANDIDATES:
        ar = ROOT / candidate
        if ar.is_file():
            raise SystemExit(subprocess.run(
                [str(ar), *sys.argv[1:]]).returncode)
    try:
        probed = subprocess.run(
            ["python3", "-c", "import autoresearch"], capture_output=True)
    except FileNotFoundError:
        probed = None
    if probed is not None and probed.returncode == 0:
        raise SystemExit(subprocess.run(
            ["python3", "-m", "autoresearch", *sys.argv[1:]]).returncode)
    sys.exit(
        "bin/ar: the auto-research-toolkit is not installed anywhere this "
        f"shim can reach (probed {', '.join(str(ROOT / c) for c in CANDIDATES)}"
        ", then `python3 -m autoresearch`).\\n"
        "  Remedy: install the toolkit into a venv at this domain root:\\n"
        "    python3.12 -m venv .venv312 && .venv312/bin/pip install "
        "<path-to-auto-research-toolkit>\\n"
        "  (Without it, bare `ar` on macOS is /usr/bin/ar, the BSD archiver -- "
        "not the research harness. H149.)")


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

## Start a harness-debt map early

The first real domain lost an afternoon to a defect that had already been
found, fixed, and *forgotten* — nothing turned its closed harness entries into
onboarding knowledge for the next session. It compensated by hand-writing one
skill, `harness-debt-map`, and checking it before ever filing or debugging a
toolkit command again. Adopt the same pattern as soon as your first harness
entries close (the `H` track):

```markdown
---
name: harness-debt-map
description: "use when a toolkit command misbehaves in this domain — a crash,
  a wrong display, a missing onboarding path, or an unexpected selection — to
  check whether the defect is already recorded before filing it again or
  debugging the wrong layer."
cites: [H1, H2]
distilled: {at: <re-stamp when the cited entries move>, core: <core version>}
---

# Known harness debt and where it landed

- **[H1] <one-line title>**: status here, remedy there, reopen condition.
```

One bullet per defect: what it was, whether it is fixed (and in which core
version), and the standing rule it taught. File new findings on the harness
track with evidence (`ar entry new --track harness --repro ... --observed
...`) — the map points at the records, it does not replace them.
"""


AGENTS_MD = '''\
# AGENTS.md -- driving this research domain by hand

This repository is an `autoresearch` domain: a research record with enforced
invariants. You -- an agent in a terminal, or a person -- move the queue
through the `ar` verbs. There is no coordinator process to wait for.
`bin/ar` is the entry-point shim (bare `ar` collides with the BSD archiver on
macOS). Start every session with the board:

    bin/ar board         # goal, distance to target, queues, live claims
    bin/ar rank          # why the queue is ordered the way it is
    bin/ar entry list    # the entries themselves
    bin/ar budget        # every meter, and the stop decision

The record draws its own map: `bin/ar entry graph` prints the hypothesis tree
-- branch lineage, supersessions, related work, coloured by status -- as a
mermaid diagram, the same markdown `ar render` writes into the graph view. A
diagram of your own (a mechanism sketch, a decision tree) is a ```mermaid
fence in an entry body or a memo: markdown renders it (Obsidian, GitHub) and
nothing parses it back, so a picture can never drift from the record the way
a hand-maintained map would.

## The protocol: claim -> work -> measure -> close

- **A claim is the only write ticket.** `bin/ar claim ID --why "..." --max-runs N`
  before you work on an entry; `bin/ar release ID --why "..."` to hand it back.
  A verdict applied without a claim is exactly the race the claim lock exists
  to prevent.
- **Run experiments through `bin/ar measure`**, never around it: it writes the
  validated run rows that ranking and every meter read.
- **Close with evidence**: `bin/ar close ID <status> --memo path/to/memo.md`.
  The memo must exist, and any gates the track declares are enforced. A
  hand-applied closure gets no discount for being applied by a person.
- Stuck past your ceiling: `bin/ar release` with a reason. A claim abandoned
  by a dead session is freed with `bin/ar reap ID` once past its TTL.

## Researching

`bin/ar research "your question"` spins up scout agents whose proposals land
as ordinary entries, priced by the next `ar rank` -- **but only if domain.toml
names a `[brain]`**: scouts are model asks behind the brain seam, and the
command refuses without one. The same goes for `bin/ar skill distil`. In a
hand-driven domain without a brain, file ideas yourself instead:

    bin/ar entry new "title" --hypothesis ... --prediction ...

and let the next `ar rank` price them.

## Set the harness goal from goal.yaml

You are probably running inside a harness with a goal feature (`/goal <text>`
in most coding harnesses): use it. Read `goal.yaml` first -- the metrics, the
objective over them, the possibly-moving target, and `stop_when` -- and
translate it into a goal the harness will drive, e.g.:

    /goal iterate until a solution scoring +1% over the current best
    objective is measured: claim the top-ranked entry, work it, `ar measure`
    it, `ar close` it with a memo, re-run `ar rank`. Stop when goal.yaml's
    stop_when holds, or `ar budget` says the meters do.

A goal turns the loop's cadence into your persistence; the record keeps the
loop honest underneath it:

- An improvement is only real once `ar measure` has written the run row and
  `ar close` has accepted the evidence. A harness goal is motivation, never
  a licence: no claim, no verdict.
- `stop_when` and the yield floor in goal.yaml are the domain's own stop
  decision; `ar budget` shows every meter and the same decision. When either
  fires, say so and stop -- do not let the goal text push past a ceiling,
  which is the one thing this harness refuses to negotiate.
- The +1% is an example, not a rule: pick the step size goal.yaml implies
  (the target moves, `bin/probe-target` refreshes it) and restate the goal
  when the target moves under you.

## Fan out: you are probably a harness that can spawn subagents

This domain was built for concurrent drivers -- the record's claim protocol
drained one queue from 92 concurrent sessions. If your harness can spawn
subagents, use that instead of working entries one at a time:

- **One subagent per entry it should progress.** Do not split one entry across
  agents: a claim is per-entry and single-holder, and `ar claim` names the
  holder when it refuses. The claim **is** the coordination primitive -- two
  subagents claiming different entries need no other locking.
- **Each subagent passes its own `--session <name>`** (a global flag, before
  the verb: `bin/ar --session scout-3 claim Q-12 --why ...`). Claims, usage
  rows and history all attribute to that name; a dead subagent's claim is
  freed with `bin/ar reap Q-12` once past its TTL, and nothing else is
  touched.
- **The orchestrating agent keeps the judgement-heavy verbs**: `board`,
  `rank`, `entry new`, `close` (a closure's memo and gates are a verdict --
  one writer, not a fan-out), `budget`. Subagents claim, measure and report
  back; the orchestrator closes.
- **Subagent model spend is still out-of-band spend** -- record it under the
  subagent's session name (see below), or the ceiling refuses metered spend.

## Worktree isolation when the harness has none

One worktree per terminal or agent: `bin/ar session create NAME`, work inside
it, then `bin/ar session destroy NAME --harvest` when done -- the harvest
appends run rows to the primary record, deduped by run id. Destroy refuses on
drifted configuration or diverged entries; `bin/ar session prune` proposes
idle sessions and nothing is destroyed without those guards. If your harness
already isolates each subagent (its own checkout or worktree), that counts --
record the same name as the claim's `--session` either way.

## Money you spend is still money

Model or API calls you make are out-of-band spend, invisible to the budget
ceiling unless recorded -- and an unrecorded row eventually refuses further
metered spend:

    bin/ar usage record --tool <harness-name> --kind api --cost 0.42 \\
        --session <your-session> --note why

## Policy binds you too

`[policy.human_only]` patterns in domain.toml are checked on every command
path: you may prepare such a command, never run it. `bin/ar policy` shows the
never-rules and proves each refuses something. `bin/ar validate` checks
records, views, runs and policy before you go.

## When a toolkit command misbehaves

A crash, a wrong display, or a command acting on the wrong layer is a defect
in the core, not a puzzle to work around -- and it may already be known. Check
`docs/skills/` for a harness-debt map first (see that directory's README), then
`docs/log/Harness Debt.md`. New findings go on the harness track with
evidence, never into prose:

    bin/ar entry new --track harness '<title>' --core auto --repro '<command>' \\
        --observed '<what happened>' --expected '<what should happen>'

`bin/ar harness export` packages the open defects for upstream; publishing is
a person's act. Closing uses this track's terminal verdicts (`fixed`/`wontfix`,
not `confirmed`): `bin/ar close H-1 fixed --memo inbox/...`.

## Do not start the coordinator

`ar loop` drives this domain autonomously and refuses to start unless
domain.toml names an explicit `[brain]` (and authorizes any spend). This
domain is driven by hand; leave `ar loop` alone.
'''


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
# retained evidence is recovered through `autoresearch workspace inspect NAME`
# and `autoresearch workspace recover`, never by bulk branch deletion.
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
        "domain.toml": _domain_toml(name),
        "goal.yaml": _goal_yaml(name, objective, metrics, target),
        "bin/measure": MEASURE,
        "bin/ar": AR,
        "bin/autoresearch": f"#!{sys.executable}\nfrom autoresearch.cli import main\nraise SystemExit(main())\n",
        "guides/landscape.md": GUIDE,
        "AGENTS.md": AGENTS_MD,
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
    (root / "bin" / "ar").chmod(0o755)
    (root / "bin" / "autoresearch").chmod(0o755)
    domain = DomainConfig.load(root)
    written.extend(write_views(domain, Store(domain.paths.entries).all()))
    return written
