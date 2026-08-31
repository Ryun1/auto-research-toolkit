# autoresearch

A domain-agnostic harness for running autonomous research loops: typed goals,
structured hypothesis state, ranked selection, a coordinator that dispatches
parallel workers, and budgets that actually stop things.

It is extracted from a harness that drove **271 hypotheses to 257 verdicts in
eight days** on one problem, and — more usefully — filed **143 defects against
itself** in the same period, with dates and evidence. Those defects are the
design input. `docs/EVALUATION.md` classifies all of them and says what each one
becomes here.

## The idea

A research loop needs four things a chat session does not have: somewhere to put
a verdict so the next agent does not repeat the work, a way to decide what is
worth doing next, a way to know whether it is winning, and a way to stop. This
provides those, and delegates judgement to a model through one narrow seam.

```
orient -> generate -> rank -> dispatch -> curate -> qc -> stop?
```

Every phase is metered. Generation runs *every* iteration, concurrently with the
work, so the queue never starves. Ranking is a formula over recorded numbers that
a judge may reorder but not overrule. Workers get isolated workspaces the
coordinator creates and destroys. QC is mechanical first and a model second.

## A domain supplies four things

Everything else is core-owned.

| | |
|---|---|
| **Measurement** | a command that runs one experiment and emits a validated record |
| **Goal** | metrics, an objective over them, a possibly-moving target, derived constants |
| **Policy** | forbidden paths, never-push remotes, human-only commands, a spend ceiling — as data |
| **Knowledge** | the guides agents read to form good hypotheses |

```toml
# domain.toml
[domain]
name = "toy"
goal = "goal.yaml"

[[tracks]]
id = "research"
prefix = "Q"
view = "docs/log/Hypothesis Queue.md"

[lanes]
findings = "^(data/runs|inbox|docs|state)/"

[[policy.human_only]]
pattern = "thing submit"
reason  = "irreversible and public; an agent may prepare it, never run it"

[budgets]
claim_max_runs        = 12
iteration_fanout      = 3
domain_max_runs       = 500

[commands]
measure      = "bin/measure"
probe_target = "bin/probe-target"
```

```yaml
# goal.yaml
goal:
  id: beat-frontier
  direction: minimise
  metrics:
    T: {field: T, direction: minimise}
    Q: {field: Q, direction: minimise}
  objective: "round(T) * Q"
  derived:  {break_even: "T / Q"}     # an expression, never a stored number
  target:   {source: bin/probe-target, moving: true}
  stop_when: "objective < target"
  yield_floor: {confirmed_per_iteration: 0.15, over_iterations: 10}
```

## Install

```bash
pip install -e .
ar --domain domains/toy board
```

## Commands

```
ar board       one screen: goal, distance to target, queues, live claims, measurements
ar rank        score the queue and show the numbers it ranked on
ar budget      every meter, and the stop decision
ar loop        run the coordinator until it stops
ar entry       file, show and list entries
ar claim       take one entry (serialised, always with a ceiling)
ar release     hand a claim back, with a reason
ar reap        free ONE abandoned claim past the TTL
ar close       close, reopen, or relabel a closure
ar measure     run the domain's measurement and record the row
ar migrate     convert an existing prose corpus into records (one way)
ar render      write the generated queue views
ar validate    check records, views, runs and policy
ar policy      show the never-rules and prove each refuses something
```

## The seven invariants

Each comes from a failure class in `docs/EVALUATION.md`, and each has a test.

1. **One record per entry; every document is generated.** A hand-edited view is a
   validation failure, not a divergence found four hours later.
2. **No regex over prose reaches a decision.** Migration is the one exception and
   it is one-way, with a fidelity gate.
3. **Every guard ships a negative test proving it refuses something.** A guard
   whose enforcement can be deleted without a test failing is a comment.
4. **Constants are computed from a typed goal, never stored.** A stale read is an
   error, not a warning.
5. **Liveness is observed, not inferred.** The coordinator owns the pool and
   knows when a worker finished; TTL reaping is the fallback.
6. **Results are returned through a typed interface.** Undeclared output is not
   evidence and cannot back a closure.
7. **The state machine is total and every budget is a meter.** No status is
   terminal by omission; no ceiling is free text.

Two conventions are inherited verbatim from the harness this came from, because
they were already right: **reuse the reader that owns the parse**, and **every
reader reports how many things it read** — a board that says "no live claims"
must not be indistinguishable from one that read nothing.

## The closure taxonomy, made operational

A refutation asserts a direction is dead. That assertion has a *scope*, and
saying which is the sharpest idea in the source harness:

| kind | reach | re-open condition |
|---|---|---|
| `mechanism` | holds outside the range measured | a new mechanism |
| `slope` | holds only inside the band measured | **required** — name the band |
| `cell` | a direct point measurement | **required** — the cell moving |

Ranking reads it. A `mechanism` refutation **hard-excludes** any queued entry
sharing its mechanism — that is what stops a fleet re-running work the board
already paid for. A `slope` or `cell` refutation only **penalises**, because it
re-opens outside its band, and excluding it would be exactly the over-claim the
taxonomy exists to prevent.

## Roles

Prompt per role in `src/autoresearch/agents/`, dispatched by the coordinator:
`generator`, `judge`, `worker`, `curator`, `qc`. The brain is swappable —
`SDKBrain` runs them through the Claude Agent SDK; `ScriptedBrain` runs the whole
loop with no model, which is what makes `ar loop` a unit test rather than a bill.

## Status

The core is complete and tested (150 tests). Two domains exist: `domains/toy`, a
synthetic problem with an interior optimum, a knob interaction and a validity
gate, used to exercise the loop in seconds; and the ECDSA Fail benchmark, wired
up in its own repository.
