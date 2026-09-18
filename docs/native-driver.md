# Driving the record without the coordinator

`ar loop` is one driver of the record, not the only one. The field proved the
other driver first: the campaign this core came from ran 92 sessions against a
shared corpus with no coordinator process at all, and the domains that followed
ran even fewer loop iterations — four, across six projects, in three weeks —
while filing hundreds of entries, 13,375 measured runs and dozens of harness
defects through the same verbs. The record, the claims, the meters and the
closures are the product; the coordinator is one way to move them.

This document says what the supported no-coordinator shapes are, and what the
meters still demand of each.

## The three drivers

| driver | who moves the queue | when to use it |
|---|---|---|
| `ar loop` | the coordinator: generate → rank → dispatch → curate → distil → qc | unattended campaigns where the brain seam is authorized |
| native dispatch (`[coordinator] dispatch = "native"`) | the loop reserves each shortlisted card, then hands a manifest to the surrounding harness's own subagents, which settle with `ar external complete` | you want the loop's ranking and budgets but your agents live in an outer coding harness |
| the outer harness, by hand | agents (or you) run `ar claim` → work → `ar measure` → `ar close` directly — often one orchestrating agent fanning out subagents, one entry each | the dominant field shape: judgement-rich work, model calls you don't want metered through the loop, or no loop process at all |

The third shape is not a workaround. `ar claim` is serialised, `ar measure`
writes the same validated run rows, `ar close` enforces the same memo,
closure-kind and gate rules, and `ar budget` meters the same ceilings — a
hand-driven campaign reaches every invariant the loop reaches, because they
live in the verbs, not in the coordinator. What the verbs cannot give you is
generation, ranking and distillation on a cadence; run `ar rank` and
`ar skill distil` by hand when you want them, and expect the board
(`ar board`) to show what a cadence would have done for you.

## What the outer harness must honour

**Claims are the only write ticket.** An agent works inside a claim or not at
all; `ar claim` names the entry, the ceiling and the session. A verdict
applied without a claim is exactly the race the claim lock exists to prevent.

**Every spend is still a spend.** Model calls made by an outer harness are
out-of-band spend and invisible to the money ceiling unless recorded:
`ar usage record --tool <harness> --kind api --cost N --session S --note why`,
or `--unknown` when the price is not knowable yet. Unknown is never free; a
finite ceiling refuses further metered spend until `ar usage reconcile` prices
the row. This is not bureaucracy — the corpus this core came from had
free-text budgets and no ledger at all ("nothing meters it"), which is why
`ar usage` exists.

**Closures carry evidence.** `ar close` demands the memo, the closure kind and
— on tracks that declare them — the gates. A hand-applied closure gets no
discount for being applied by a person.

**Human-only policy binds the outer harness too.** `[policy.human_only]`
patterns are checked on every command path; an outer agent may prepare a
submission, never run it. If your harness's agents shell out, they go through
the same policy check as a worker does.

## Fan out: a harness that can spawn subagents should

The hand-driven driver is not one agent in one terminal. The claim protocol
exists because 92 concurrent sessions drained one queue (`driver/claims.py`);
the same shape works one level down, inside your harness:

- **One subagent per entry it should progress.** Never split one entry across
  agents: a claim is per-entry and single-holder, and `ar claim` names the
  holder when it refuses. The claim is the coordination primitive -- two
  subagents claiming different entries need no other locking.
- **Each subagent names itself.** The global `--session` flag attributes
  claims, usage rows and history:
  `ar --session scout-3 claim Q-12 --why ... --max-runs N`. A dead
  subagent's claim is freed with `ar reap Q-12` once past its TTL -- per
  entry, nothing else is touched -- and bare `ar reap` lists what is
  reapable, with the holder names.
- **The orchestrating agent keeps the judgement-heavy verbs**: `board`,
  `rank`, `entry new`, `close` (a closure's memo and gates are a verdict --
  one writer, not a fan-out), `budget`. Subagents claim, measure and report
  back; the orchestrator closes.
- **The orchestrator also carries the persistence.** Most harnesses have a
  goal feature (`/goal <text>`); set it from `goal.yaml` -- metrics,
  objective, moving target, `stop_when` -- as a goal the harness will
  iterate toward, e.g. "iterate until a solution scoring +1% over the
  current best objective is measured; stop when `stop_when` holds or `ar
  budget` says the meters do". The goal supplies the cadence the
  coordinator would have; it never substitutes for the record: an
  improvement is real only after `ar measure` writes the row and `ar close`
  accepts the evidence, and when `stop_when` or a meter fires, the goal
  stops with it.
- **Subagent model spend is out-of-band spend**; record it under the
  subagent's session name or the ceiling refuses further metered spend (see
  below).

`ar session` worktrees are for agents the harness does not isolate; a
subagent with its own checkout does not need one, but should still claim
under the same `--session` name it records usage under.

## Sessions: long-lived worktrees for hand-driven work

When work is driven by hand, workers still need real isolation, and the
workspace pool is the wrong tool — it exists inside a coordinator process that
is not running. `ar session` is the persistent shape the field converged on
after a vanished worktree took run rows with it:

```
ar session create fixes       # git worktree at <parent>/<root>-session-fixes
... work in the worktree ...
ar session destroy fixes --harvest
```

- **create** records the session: path, base commit, a hash of `domain.toml`,
  created time, settle window (`[session] settle_hours`).
- **destroy refuses** on three things, each learned from a field defect:
  config drift (the session was cut from a different configuration — the
  hr-iter lesson: drifted config split one ledger across two run paths),
  unharvested run rows (`--harvest` appends them to the primary's lanes,
  deduped by run id), and entries that diverge on both sides (named and
  refused — reconcile with `ar entry amend`; the core never overwrites a
  primary entry).
- **prune proposes, destroy disposes.** `ar session prune` reports idle,
  clean, unharvested sessions against the settle window; nothing is destroyed
  without the guards above.

## Extending the CLI: plugins

Domains grow tooling the core has no home for — a GPU dev-loop, an evidence
adaptor, a leaderboard probe. Declare it:

```toml
# domain.toml
plugins = ["qsbtools"]
```

Each name is an importable module on the domain root's path exposing
`register_cli(subparsers, config)`; its subcommands appear on `ar` and run
through the same policy engine, meters and record as every core verb, because
they are written against core functions. Before this seam existed, a domain
reached the same place by composing the toolkit's parser privately — an
undeclared seam is one every domain re-invents, differently.

## When to prefer which

Choose the coordinator when the queue must not starve and the brain is
authorized to spend. Choose native dispatch when you want the loop's ranking,
reserves and budget enforcement around agents you did not have to teach the
record. Choose hand-driven when the work is judgement-rich and irregular —
and let `ar board` and the skills line tell you what a cadence would have
been doing while you were away.
