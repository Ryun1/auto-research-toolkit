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
| `ar loop` | the coordinator: orient → generate → rank → dispatch → curate → distil → qc | unattended campaigns where the brain seam is authorized |
| native dispatch (`[coordinator] dispatch = "native"`) | the loop claims each shortlisted card and reserves an external assignment for it, then hands a manifest to the surrounding harness's own subagents, which settle with `ar external complete` | you want the loop's ranking and budgets but your agents live in an outer coding harness |
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
all; `ar claim` names the entry, the ceiling and the session, and prints a
token. The token, not the session name, proves the claim is yours: pass it
as `--claim-token` (or `AR_CLAIM_TOKEN`; comma-separate several when one
session holds several claims) to release, update or reopen the
claim, or to `ar exec` / `ar external assign` under it, under whatever
session name you act as. `ar close` is deliberately not gated: an
orchestrator closes the entries its subagents claimed. A domain that wants name ownership
back sets `[coordinator] claim_tokens = false`. A verdict applied without a
claim is exactly the race the claim lock exists to prevent.

**Every spend is still a spend.** Model calls made by an outer harness are
out-of-band spend and invisible to the money ceiling unless recorded with
`ar usage` ([below](#out-of-band-spend-ar-usage)). This is not bureaucracy —
the corpus this core came from had free-text budgets and no ledger at all
("nothing meters it"), which is why `ar usage` exists.

**Closures carry evidence.** `ar close` demands the memo, the closure kind and
— on tracks that declare them — the gates. A hand-applied closure gets no
discount for being applied by a person.

**Human-only policy binds the outer harness too.** `[[policy.human_only]]`
patterns are checked on every command path; an outer agent may prepare a
submission, never run it. If your harness's agents shell out, they go through
the same policy check as a worker does.

## Fan out: a harness that can spawn subagents should

The hand-driven driver is not one agent in one terminal. The claim protocol
exists because 92 sessions drained one queue (`src/autoresearch/claims.py`);
the same shape works one level down, inside your harness:

- **One subagent per entry it should progress.** Never split one entry across
  agents: a claim is per-entry and single-holder, and `ar claim` names the
  holder when it refuses. The claim is the coordination primitive -- two
  subagents claiming different entries need no other locking.
- **Each subagent names itself.** The global `--session` flag attributes
  claims, usage rows and history:
  `ar --session scout-3 claim Q12 --why ... --max-runs N`. A dead
  subagent's claim is freed with `ar reap Q12` once past its TTL -- per
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
  subagent's session name or the ceiling refuses further metered spend
  ([below](#out-of-band-spend-ar-usage)).

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
... work in the worktree; measure from the primary:
ar --domain <primary> measure --candidate <worktree> -- ...
ar session destroy fixes --harvest
```

- **create** records the session: path, base commit, a hash of `domain.toml`,
  created time, settle window (`[session] settle_hours`).
- **destroy checks everything before it writes anything**, so a refused
  destroy leaves the primary untouched. Each refusal was learned from a field
  defect, and each names what it found:
  - **config drift** — `domain.toml` no longer matches the hash recorded at
    create. The hr-iter lesson: drifted config split one ledger across two run
    paths. `--accept-drift` proceeds once the drift is understood.
  - **rows the core reader does not recognise** in a worktree run file —
    destruction would lose them uninterpreted; reconcile the file by hand.
  - **entries that differ on both sides** — harvest never overwrites a primary
    entry; reconcile with `ar entry amend`, then destroy again.
  - **memos that diverge from the primary** — memos are never auto-harvested;
    fold them in by hand.
  - **without `--harvest`: anything the primary lacks** — run rows absent from
    its ledger and entries that exist only in the worktree. A vanished
    worktree once took its rows with it.
  - **with `--harvest`: unreceipted rows** — only rows `ar --domain <primary>
    measure --candidate <worktree>` receipted are harvested (#41).
    `--discard-unreceipted` archives the rest beside the session record, out
    of the ledger, instead of refusing.

  `--harvest` appends the receipted rows to the primary's lanes, deduped by
  run id, and copies worktree-only entries into the primary. The session
  record is archived under the sessions directory, never deleted.
- **prune reports; `--destroy` acts on what it reports.** `ar session prune`
  lists every session with its age (hours since it was created — reported as
  `idle_hours`, but nothing measures activity) against the settle window,
  whether it is eligible, and its blockers. A session is eligible only when its worktree exists, is clean,
  has nothing unharvested and nothing divergent, and is older than
  `settle_hours`. `ar session prune --destroy` destroys each eligible session
  through `destroy`'s own refusals, without `--harvest` — so a session with
  evidence still to harvest is never pruned; destroy it by hand.

## Native dispatch: the loop ranks, native agents work

`[coordinator] dispatch = "native"` keeps the coordinator's ranking and meters
and moves the work out of its process. The dispatch phase gives each
shortlisted card what a core worker would get — the domain preflight, the
fanout, spawns and runs charges, a claim, and then a bounded external
assignment (`ar external assign`'s reservation: run ceiling, wall clock,
workspace, output contract) — and starts no worker. No model runs in the
coordinator for the work. The runs are charged twice, to two meters: the
iteration's runs meter, so an iteration bounds what it hands out, and the
claim and campaign ceilings at reservation, which a crash never refunds.

The iteration then ends at the handoff. While any assignment is
outstanding, curate, distil and qc do not run — nothing has come back to
close out — and each is recorded as skipped with that reason. The loop stops
with `stop: native-handoff` rather than re-ranking a queue whose claims are
still held, and `ar loop` prints a manifest: one line per assignment with
its entry, run ceiling, expiry and workspace, and `ar external show <id>`
for its brief. The surrounding harness's subagents do the work and settle
each one with `ar --session <session> external complete <id> --report
report.json` (`--session` is global and goes before the verb), which harvests and applies the verdict through the same
ownership checks, close gates and evidence retention as an in-loop worker.
The next `ar loop` invocation's orient sees the applied verdicts. `ar
external list` shows open assignments across sessions; `ar external cancel`
abandons one and keeps its evidence; `ar external reap` frees an expired one
another session holds.

The judgement roles (generate, judge, and curate, distil and qc in an
iteration with nothing outstanding) still run through whatever `[brain]`
routes them to: route them to commands, or let the parent file entries and
prices itself. The loop refuses to start without an explicitly named brain
(`[brain] default`), and the one paid built-in backend stays fail-closed —
the unattended path never reaches for API spend nobody approved. This is a
handoff protocol, not a sandbox: policy checks and human-only gates are
unchanged, and the coordinator that dispatches the work remains responsible
for what it dispatches. The iteration shape is drawn in
[ARCHITECTURE.md §2](ARCHITECTURE.md#2-one-iteration-seven-phases).

## Out-of-band spend: `ar usage`

A spender the brain's cost file never sees — an outer harness's model calls,
a curator's decision API, a hand-paid GPU rental — is invisible to a money
ceiling, and a ceiling that cannot see a spender is not a ceiling.

```
ar usage record --tool <harness> --kind api --cost 0.42 --session S --note why
ar usage record --tool vast --kind rental --unknown --session S --note why
ar usage list
ar usage reconcile <lineno> --cost N --session S --note why
```

`record` appends a row to `state/usage.jsonl`, committed with the corpus like
every meter, and `ar budget` sums the rows into the campaign money ceiling
(`--kind` is `api`, `rental` or `other`). `--unknown` records spend whose
price is not knowable yet: unknown is never free, and a finite ceiling
refuses further metered spend until it is priced. `list` shows every row and
what is still unpriced; `reconcile` prices one by its line number by
appending a correction, never by editing the row. Because the rows feed the
ceiling, a malformed row is a named refusal, never a silent skip.

The run ledger has the same escape hatch. `ar attempt reconcile <id>
--charged-runs N --reason ...` archives an attempt record the meter cannot
trust (a legacy-schema row, for instance) behind a tombstone whose digest
pins the preserved bytes. The spend is asserted by the operator, never
inferred — `0` is a real answer when you can confirm it, never a default —
and feeds the campaign ceiling as stated. Until it is reconciled, the
invalid record refuses metered commands by name, and `ar attempt list`
reports it rather than dying.

## Extending the CLI: plugins

Domains grow tooling the core has no home for — a GPU dev-loop, an evidence
adaptor, a leaderboard probe. Declare it:

```toml
# domain.toml
plugins = ["qsbtools"]
```

Each name is an importable module, resolved with the domain root prepended
to `sys.path` (so a domain's module wins over an installed one), exposing
`register_cli(subparsers, config)`. Its subcommands appear on `ar` and run
through the same policy engine, meters and record as every core verb,
because they are written against core functions, not around them. Before
this seam existed, a domain reached the same place by composing the
toolkit's parser privately — an undeclared seam is one every domain
re-invents, differently.

The honest boundary: the core refuses a plugin that cannot be imported or
does not expose `register_cli`; it cannot prove a plugin never bypasses the
record, and a plugin that does is forking by other means.

## When to prefer which

Choose the coordinator when the queue must not starve and the brain is
authorized to spend. Choose native dispatch when you want the loop's ranking,
claims, run reservations and budget enforcement around agents you did not
have to teach the record. Choose hand-driven when the work is judgement-rich
and irregular — and let `ar board` and the skills line tell you what a
cadence would have been doing while you were away.
