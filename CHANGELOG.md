# Changelog

One section per release, newest first, taken from each `Release:` commit.
v0.2.0 and v0.8.0 onward are git tags (`git tag`); v0.3.0 to v0.7.1 exist
only as their `Release:` commits, named beside each heading. What a domain
must change to take a release is in [docs/MIGRATION.md](docs/MIGRATION.md).

## v0.12.0 — 2026-10-26

Agent-speed pass: the verbs a session runs at boot are fast and sized for a
context window.

- One strict YAML parse per entry, on libyaml when available: board, rank,
  budget, epochs and render on a 1,542-entry domain take 1-2 s, not ~20 s;
  validate 4 s, not 31 s.
- `ar rank` counts closed entries and shows only the candidates' calibration
  rows (3,528 lines -> 97); `--all` prints the full table.
- `ar entry list` lists open entries by default (1,543 lines -> 81); `--all`
  or `--status` lists the rest, and `--json` is unchanged.
- Piping any verb into `head` ends without a traceback.
- `--claim-token` before `measure` no longer blocks domain-flag passthrough.
- Help for every subcommand and the claim/close/measure/entry-new flags.
- pytest-xdist in the dev extra: the suite runs in ~40 s with `-n auto`.

## v0.11.0 — 2026-10-25

Fixes #19-#48. The October research report's measurement-integrity work:

- the evaluator is scored from the primary checkout, and rows are admitted
  only with a measurement receipt;
- measurement epochs;
- noise-aware promotion;
- suspect wins wait for a second source;
- a holdout the search never sees.

Field-report fixes: exclusive entry filing; claim tokens; reap liveness;
`ar entry collisions` and duplicate-closure warnings; a merge driver for
generated views; `--derived-from` lineage; record-floor and lineage warnings.

The review pass fixed 18 confirmed defects in that work. The frontier read is
now ~100x faster on a qsb-sized corpus.

Records written by this core can carry `claim.token_sha`, `derived_from` and
`result.pin`, which older cores refuse to read. Domains should set
`[upstream] min_core = "0.11.0"`; `ar validate` says so when it applies.

## v0.10.0 — 2026-10-25

Fixes #9-#17: frontier eligibility (goal constraints, row-level scaffold,
closed-entry measure refusal), zero-objective rows, the superseded-only
terminal table, the zero money ceiling, mechanism-closure exclusions named on
close, external records for superseded, measure flag pass-through, run-id
citation warnings, and `ar external list/show/cancel`. `ar doctor` refuses a
dirty or untagged editable core.

## v0.9.0 — 2026-10-25

Lands the sig.golf H1-H26 and QSB H71/H82/H83/H87 fixes, the superseded
retirement and record amend (H15), and origin's `evidence_class` tolerance
(absence reads as unlabelled, never as a stronger class). Until now the
editable tree reported 0.9.0 uncommitted, so a `min_core` pin of 0.9.0 pinned
nothing; domains installing from GitHub sat on 0.8.0.

## v0.8.0 — 2026-09-20

Two features, both demanded by first-participant field use and reproduced
against the field corpora before they were built.

- **A closure names its evidence class** (pvfast-stwo-simd H10/H11). The
  faithfulness strictness made the qc phase permanently red on a corpus that
  legitimately holds evaluator-owned numbers and measurement-free censuses:
  17-29 problems per iteration, 27 of them accepted legacy closures. A
  terminal result now declares `ledger` (default, full fabrication net),
  `official`, or `census`; the net also reads `run_detail` legs retained on
  settled external reports, so a scratch bench's confirmed summary must still
  trace its figures. Writers: close, amend, and the external output contract.
- **`ar relocate`** (QSB H32). `ar validate`'s oversized-inbox warning has a
  path that acts on it: one command moves the file and rewrites every
  recorded pointer — closed entries included — through the atomic record
  save, then re-renders, so validate follows clean.

## v0.7.1 — 2026-09-19 (commit ab766ae)

One defect fix, found by first-participant use in a running campaign and
reproduced end to end before it was fixed.

A refused external completion no longer poisons the retry. A close-gate
refusal applied no verdict, so it now writes no verdict receipt; the claim
stays with the worker and the row returns to assigned, and a corrected report
re-runs the command on the same assignment. Refusal receipts already on disk
no longer settle retries or block cancel, and the refusal to accept
out-of-contract verdict strings closes the hole this fix exposed: the
settled-verdict path only takes the vocabulary the output contract declares.

## v0.7.0 — 2026-09-19 (commit af504b7)

`ar entry graph --html` writes a standalone snapshot of the generated views
in which every hypothesis is a small status-coloured circle flowing left to
right, and hovering one shows the full title and its summary. The page embeds
the payload per entry and builds the tooltip from `textContent`, so record
prose cannot inject markup or break out of the embedded JSON. The verb covers
the tracks that declare `graph_view`, with `--track` overriding. The generated
`.md` view is unchanged.

## v0.6.0 — 2026-09-19 (commit 67ac9f5)

Two features, each removing a way a domain or a record can drift from the
thing it is supposed to track.

- **The record draws its own map.** `tracks.graph_view` is a second generated
  view, the hypothesis tree as mermaid — branch lineage, supersessions,
  related work, nodes coloured by status, wide corpora bucketed. `ar entry
  graph` prints the same markdown the view writes. Undeclared view = nothing
  written and nothing checked.
- **The public challenge's shared intel.** `[challenge]` declares an adapter
  (provablyfast, yukon); `ar challenge pull|show|check|target` normalize the
  leaderboard into `state/challenge/snapshot.json`, and the published record
  becomes the moving target through `bin/probe-target`. The loop never
  fetches; orient reads the cache only. External verdicts ride into
  generator, judge and scout briefs, never into the record. `ar challenge
  check` makes comparability loud — host fingerprint, `[policy]` against the
  challenge's forbidden paths, snapshot age — exit 1.

## v0.5.0 — 2026-09-19 (commit 882767b)

Three guards against a result the record cannot back.

- **Verdict replication:** `[coordinator] confirm_runs` (default 1 =
  unchanged) demands that many valid run rows before a `confirmed` experiment
  closure is accepted, on every close path (`_apply_verdict`, `ar external
  complete`, `ar close`). Refuted verdicts are exempt.
- **Branch stagnation:** `[coordinator] branch_stagnation` (default 0 = off).
  A parent with at least that many experiment-disposition refuted children
  and none confirmed is stagnant: its remaining children drop out of ranking
  by a hard filter, and filing a new child is refused. Recomputed from the
  record every rank.
- **Mechanical faithfulness, always on:** at QC, before any model is asked,
  every closed `confirmed` entry's typed summary is traced against the run
  ledger; a number matching nothing is named in the mechanical problems.

71 new tests; suite at 771.

## v0.4.0 — 2026-09-19 (commit fa21e83)

Agent-efficiency release. The coordinator's briefs are role-scoped and
serialized compact. Backend asks are bounded: `[brain] timeout_seconds` kills
a wedged backend by process group as a named phase failure, with its cost
file still charged, and TypeSafe wire blips retry once per class. Per-file
caches cover entry records, run rows, the attempt ledger, skills and evidence
digests; dispatch concurrency gets one shared `max_parallel` default (3);
entry saves are atomic.

For hand-driven agents: `--json` on rank/budget/validate/entry-list; `entry
show` defaults to state (`--history` opts back in); mutating verbs re-render
the views themselves; `skill find` routes on descriptions; `skill list` exits
0 (`skill check` keeps refusal semantics). 749 tests (37 new); no migration
required from 0.3.0 records.

## v0.3.0 — 2026-09-19 (commit af9822f)

Field feedback from the first participant domain (provably.fast-research):
the lost H2 display fix re-committed with regression tests, one reprice
writer for the loop and the CLI, a non-terminal close warning, `ar doctor`
with a declared `[upstream] min_core` floor, and the harness-debt-map
convention in the scaffold. Additive; no migration required from 0.2.0
records.

## v0.2.0 — 2026-09-18

No `Release:` commit; the tag is on e9e4dca.

- **Ranking is a tree:** entries carry `parent` or not (a novel root); the
  coordinator enforces `tree_max_depth` and `tree_max_children` at filing,
  and a branch names its intent with `kind` (improve|debug|probe).
- **`[coordinator] risk`** (default 0.5) splits the shortlist between novel
  roots and incumbent refinement, replacing `explore_fraction` /
  `coverage_fraction`; a domain still declaring them is refused at load.
- **`[commands] score = "bin/score"`** hands branch pricing to the domain;
  claimable-only and fail-closed.
- **`goal.confirm_independently`** holds a met target until a second,
  independent run row also meets it.
- Breaking: `--explore` and `--coverage` are gone; `ar rank --risk R`
  replaces them.

Also in this release: native dispatch (`[coordinator] dispatch =
"native"`), the out-of-band usage ledger (`ar usage`) and `ar attempt
reconcile`; the built-in Claude SDK brain was removed (route roles
to commands; `[brain] default` is required to build a brain) and the agent
protocol ships as a scaffolded `AGENTS.md`; the TypeSafe (Jev) backend for
judge and qc; a goal's required gates bind the stop decision; `ar session`
worktrees for hand-driven work; declared CLI plugins.

## v0.1.0 — 2026-08-31 to 2026-09-17

Untagged. `pyproject.toml` said 0.1.0 from the first commit (b3b445a) until
e9e4dca bumped it; the commits of 2026-09-18 before that bump are listed
under v0.2.0, which is how the tag groups them. The core skeleton (typed goal, total state machine,
declarative policy, lanes), structured entries with generated views and
claims, ranking and budgets, the seven-phase coordinator, `ar migrate`,
hardware awareness, skills, upstream defect filing and core updates, and —
last — bounded attempts, external handoff, evidence bundles and workspace
recovery.
