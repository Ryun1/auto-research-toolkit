# Upgrading

`ar harness update` installs a newer core and writes a memo into the domain's
memos directory (`inbox/` by default), `core-update-<date>-<commit>.md`: the
old and new versions, where the core came from, and — when the upstream clone
is reachable — the one-line commit log between the two. The memo records what
moved; it does not say what that means for your domain. This page does: per
release, newest first, what a domain must do and what it should know.
[CHANGELOG.md](../CHANGELOG.md) lists everything each release changed; a
release that asks nothing of a domain appears here only in the summary line
at the end.

Each change is marked **required** (the domain, a verb or a script refuses or
breaks without it), **behavioural** (it loads, but acts differently), or
**optional**.

---

## 0.12.0

- **Behavioural: two tables got shorter.** `ar entry list` shows open
  entries and counts the closed ones; `--all` or `--status` lists the rest.
  `ar rank` counts closed entries and shows only the candidates' calibration
  rows; `--all` prints the full table. `--json` on either is unchanged, and
  is what a script should parse.

## 0.11.0

- **Required (shared or multi-machine records): declare the record floor.**
  Records this core writes can carry `claim.token_sha`, `derived_from` and
  `result.pin`, and an older core refuses to read them — by name, not
  silently. Set `[upstream] min_core = "0.11.0"` so `ar doctor` refuses an
  older install before it meets one; `ar validate` warns when the record
  carries those fields and the floor is lower.
- **Required (measure scripts): workers are measured from the primary.** A
  worker, an external assignment and a session measure with `ar --domain
  <primary> measure --candidate <slot>`: the measure command, `[policy]` and
  the evaluator files (`[evaluator] files`, default the measure script) come
  from the primary, run with the slot as `cwd` and in `$AR_CANDIDATE`. A
  script that finds the tree under test through `__file__` now finds the
  primary's copy; find it through the working directory or `$AR_CANDIDATE`.
  Harvest — in the loop, in `ar external complete` and in `ar session
  destroy --harvest` — admits only rows `ar measure` left a receipt for.
- **Behavioural: claims are owned by a token.** `ar claim` prints a token,
  and acting on the claim — `ar release`, `ar close --reopen` on a live
  claim, `ar exec`, `ar external assign` — needs it: `--claim-token` or
  `AR_CLAIM_TOKEN`, comma-separated when one session holds several. A bare
  session name no longer proves ownership of a token-bound claim, and
  `ar measure --entry` without the token stamps its row `unclaimed`, which
  is no source for `confirm_independently` (below). `ar close` is not gated, so an
  orchestrator still closes its subagents' entries. Claims from before tokens
  keep name ownership; `[coordinator] claim_tokens = false` restores it for
  every claim.
- **Behavioural: `confirm_independently` counts sources, not names.** The
  second row must come from another workspace slot or a proven claim (core
  stamps `provenance.measured_by`); an `unclaimed` row is no source, and a
  fresh session name is not a second one. A passing holdout also satisfies
  it. Rows from before the stamp are still told apart by session and entry.
- **Behavioural: the frontier reads one epoch.** An epoch is a distinct
  `(evaluator_sha, benchmark_revision)` pair. The frontier, `ar budget`, the
  stop decision and independent confirmation read only the current one. Rows
  from before the stamp form an `unpinned` epoch, which stands as current
  only until a stamped row exists — so the first measurement after upgrading
  starts a new frontier, and the old best moves to "Previous epochs" on
  `ar board`. `ar epochs` lists closures pinned to a superseded epoch; it
  never reopens them.
- **Optional, off unless declared:** a `noise` block in `goal.yaml` (a gain
  inside measurement noise neither moves the frontier nor confirms an
  entry), `suspect: {k: N}` (an anomalously large win waits for a second
  source), and a holdout (`[commands] holdout` with `[holdout]`) a win must
  pass. `ar entry new --derived-from` keeps a follow-up's lineage past
  `tree_max_depth`.

## 0.10.0

- **Behavioural: `ar doctor` refuses a dirty or untagged editable core.** A
  version string on an uncommitted tree names no code; install from a tag,
  or commit and tag the tree you serve.
- **Behavioural: `ar measure` passes domain flags through** without a `--`
  separator.

## 0.9.0

- **Required (scripts): `ar entry new` needs `--track`.** There is no
  default, because a forgotten track filed harness debt as a research lead.
  A filing now reads `ar entry new --track research "title" ...`.
- **Behavioural: `superseded` is terminal on every track**, without being
  declared. Only `ar close --superseded-by` writes it, the replacement records
  it in its own `supersedes`, and a retirement is not counted as a refuted
  sibling toward `branch_stagnation`.
- **Behavioural: an absent `result.evidence_class` reads as unlabelled.** It
  no longer stops every verb that scans the store; an unlabelled result faces
  the full fabrication net, as `ledger` does. A present but invalid value is
  still refused.
- A `min_core = "0.9.0"` pin means something from this release: before it,
  an uncommitted tree already reported 0.9.0.

## 0.8.0

- **Behavioural: a closure names its evidence class.** `ledger`, `official`
  or `census` (`ar close --evidence-class`, `ar entry amend --evidence-class
  --why`, or the external output contract). `official` and `census` skip the
  mechanical faithfulness checks and the `confirm_runs` gate; anything else
  faces both. If QC is red on legacy closures that hold evaluator-owned
  numbers or measurement-free censuses, label them with `ar entry amend`.

## 0.5.0

- **Behavioural: QC traces every `confirmed` summary to the run ledger.**
  Always on: a number in a closed entry's typed summary that matches no row
  is named in QC's mechanical problems, so existing closures may surface
  there.
- **Optional:** `[coordinator] confirm_runs` (default 1, unchanged) and
  `[coordinator] branch_stagnation` (default 0, off).

## 0.4.0

- **Behavioural: a backend ask is bounded.** `[brain] timeout_seconds`
  defaults to 3600; a backend that runs longer is killed by process group
  and recorded as a phase failure. Raise it, or set `0` to disable, if your
  agents legitimately run longer.
- **Behavioural (scripts):** `ar entry show` prints the current state;
  `--history` opts back in. `ar skill list` exits 0 whatever it finds;
  `ar skill check` keeps the refusing exit.

## 0.2.0 (from 0.1.x)

The one-paragraph version: ranking is now a **tree**, the two ranking
reserves are replaced by one dial, and the loop needs a brain you name. If
your `domain.toml` declares `explore_fraction` or `coverage_fraction`, delete
those lines and set `[coordinator] risk = 0.5` (or your stance), and give
`[brain]` a `default`. Everything else is additive.

### Required: the coordinator table

**Removed: `explore_fraction` and `coverage_fraction`.** A domain carrying
either forward is refused at load — not silently ignored. The refusal says
what replaced them: delete the key and set `risk`.

They are replaced by one dial:

```toml
[coordinator]
# Share of each shortlist aimed at novel branches: entries with NO `parent`
# (new territory). The rest refines the incumbent (entries branched off work
# already in the record). 0.5 is the neutral stance — half the attention on
# improving what you have, half on opening what you have not.
risk             = 0.5
# The tree. Branches are filed with a `parent`; these caps keep one lineage
# from growing unbounded. 0 disables either cap.
tree_max_depth   = 3
tree_max_children = 4
```

What you must decide is the value of `risk`: the share of each shortlist
that goes to novel roots, set to the stance you had tuned the old fractions
toward. The old reserves ranked on `impact` alone and on untested mechanism
tags; nothing reads them now, so a stance that lived in those numbers has to
be restated in this one. How the dial splits a shortlist, and how to pick it,
is in the README's
[Ranking: the tree, the risk dial, and the domain's own score](../README.md#ranking-the-tree-the-risk-dial-and-the-domains-own-score).

### Required: name the brain

The built-in Claude SDK brain is gone. `[brain]` values are commands (the
`ProcessBrain` contract) or `"typesafe"`; anything else, `"claude"`
included, is refused at load. A `[brain]` table without `default` loads,
but `ar loop`, `ar skill distil` and `ar research` refuse to build a brain
from it, naming the remedy:

```toml
[brain]
default = ["bin/agent", "{role}", "{prompt_file}", "{workspace}", "{cost_file}"]
# judge = "typesafe"     # paid: also needs authorize_spend = true
```

`--model` is gone with it; it only chose the SDK brain's model.

### Behavioural: ranking

- **The search is a tree.** Entries carry `parent` (the entry this branches
  from) and optionally `kind` (`improve`, `debug`, `probe`). Old records are
  valid as-is: an absent `parent` reads as a novel root. `ar validate`
  passes on pre-0.2.0 records unchanged.
- **Branches can be filed by humans too.** `ar entry new --track research
  --parent Q12 --kind debug "title" ...` (`--track` and the title are required
  since 0.9.0) runs the same filing gate the loop's generators run (unknown
  parent, depth, sibling caps, cross-track, kind-on-root are all refused
  with reasons).
- **`mechanism_coverage` stays in the briefs.** The generator is still told
  to file at least one untested-mechanism probe per batch; the
  untested-tag *quota* was always generator-side discipline and is
  unchanged.
- **The judge's brief changed shape.** The `explore_reserve` /
  `coverage_reserve` fields are gone; ranking rows now carry a `novel`
  boolean, and the instruction names the dial's share. This matters only if
  you built a custom brain that parses the judge's brief — the built-in
  ProcessBrain and TypeSafe brains are updated with the core.

### Behavioural: CLI

| 0.1.x | 0.2.0 |
| --- | --- |
| `ar rank --explore F` | `ar rank --risk R` |
| `ar rank --coverage F` | (no equivalent — one dial) |
| `ar entry new` (no tree) | `ar entry new --track research --parent Q12 --kind debug "title"` (`--parent`, `--kind` optional) |

### Behavioural: briefs (additive, for custom prompt parsers)

- Generator and scout briefs gain `branch_families`: for each branchable
  parent, its children's verdicts and a `complexity` cue
  (`minimal` <2 children, `moderate` 2–4, `advanced` ≥5). Ignore the key
  and nothing breaks; read it and your agents inherit scoped sibling
  memory — AIRA (arXiv 2507.02554 §4.1) found scoped memory pushes
  diversity where whole-record memory collapses modes.
- Worker briefs gain `lineage`: the entry's ancestral chain, root first,
  the entry last, each row with its verdict and closure kind. On a `debug`
  branch this is the prior fix attempts — the worker is instructed never
  to re-undo a repair an ancestor already made.

### Optional: let the domain price its own branches

```toml
[commands]
score = "bin/score"
```

Nothing to do unless you want it. If you do: write a command that reads
the claimable candidates as JSON on stdin and prints one finite score per
entry, then declare it. The core still applies the hard filters and the risk
partition around it, and a failing scorer skips dispatch for that iteration
rather than falling back to the formula — so test it with `ar rank` before an
unattended loop depends on it. The contract is in the README's
[Ranking](../README.md#ranking-the-tree-the-risk-dial-and-the-domains-own-score)
section; the toy domain ships a working example, `domains/toy/bin/score`.

### Optional: don't trust your own proxy

```yaml
# goal.yaml
goal:
  # ... objective, target, stop_when as before ...
  # Hold a met target until a second, independent run row also meets it:
  # since 0.11.0, one from another workspace slot or a proven claim (a
  # session name is not a source), or a passing holdout.
  confirm_independently: true
```

AIRA (arXiv 2507.02554, §5.3) measured the failure this guards at 9–13
absolute points on MLE-bench: a search guided by its own proxy overfits, and
the agent's perceived score keeps rising after the true metric has plateaued.
The core enforces only that a second *execution* said so; what makes the
re-measurement honest (fresh data split, re-seeded run) is your `bin/measure`.
Without the flag, stopping is exactly as before.

Since 0.11.0 the second row must come from a different *source* — another
workspace slot or a proven claim, not a new session name — and a passing
holdout also satisfies the flag (see 0.11.0 above).

## No action required

0.3.0 (adds `[upstream] min_core` and `ar doctor`; declaring a floor is
worth doing), 0.6.0, 0.7.0 and 0.7.1 are additive: nothing a domain declared
changes meaning, and records from the previous release load unchanged.

## References

- [AIRA: AI Research Agents for Machine Learning](https://arxiv.org/abs/2507.02554)
  — search policies, operators, and the generalization gap (§5.3).
- [AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2) and
  [AIDE](https://github.com/WecoAI/aideml) — the tree-search lineage this
  release's structure comes from.
- [OpenEvolve](https://github.com/codelion/openevolve) — the MAP-Elites view
  the dial is a first step toward.
