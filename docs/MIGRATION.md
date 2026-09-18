# Migrating to v0.2.0

`ar harness update` takes the core and writes an upgrade memo into `inbox/`.
This page is the human-readable half of that memo: what changed in v0.2.0,
what you must do, and what you may want to do. Every section is ordered by
whether it is **required** (your domain refuses to load without it),
**behavioural** (it loads, but ranking acts differently), or **optional**.

The one-paragraph version: ranking is now a **tree**, and the two ranking
reserves are replaced by one dial. If your `domain.toml` declares
`explore_fraction` or `coverage_fraction`, delete those lines and set
`[coordinator] risk = 0.5` (or your stance). Everything else is additive.

---

## Required: the coordinator table

**Removed: `explore_fraction` and `coverage_fraction`.** A domain carrying
either forward is refused at load — not silently ignored. The refusal names
this page.

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

How to pick `risk`: it is an appetite, not a formula output. Raise it while
your frontier is moving; lower it once a direction is winning and every slot
is better spent deepening it. Both ends are legitimate — `0` refines the
incumbent only, `1` opens new territory only.

What replaced the old semantics, mechanically: the old reserves ranked on
`impact` alone and on untested mechanism tags. With lineage a structural
field, novelty no longer needs a priced proxy — a novel branch is simply an
entry with no `parent`, and the dial splits the shortlist on that. Within
each partition the score decides; an unfilled share returns to the other
partition; a single-slot iteration is never spent on a partition.

## Behavioural: ranking

- **The search is a tree.** Entries carry `parent` (the entry this branches
  from) and optionally `kind` (`improve`, `debug`, `probe`). Old records are
  valid as-is: an absent `parent` reads as a novel root. `ar validate`
  passes on pre-0.2.0 records unchanged.
- **Branches can be filed by humans too.** `ar entry new --parent Q12 --kind
  debug ...` runs the same filing gate the loop's generators run (unknown
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

## Behavioural: CLI

| 0.1.x | 0.2.0 |
| --- | --- |
| `ar rank --explore F` | `ar rank --risk R` |
| `ar rank --coverage F` | (no equivalent — one dial) |
| `ar entry new` (no tree) | `ar entry new --parent Q12 --kind debug` (optional) |

## Behavioural: briefs (additive, for custom prompt parsers)

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

## Optional: let the domain price its own branches

```toml
[commands]
score = "bin/score"
```

The core still applies the hard filters (a `mechanism`-refuted direction
stays dead) and the risk partition; your command decides what a claimable
branch is worth inside them. Contract: argv like `bin/measure`, cwd = domain
root, stdin `{"risk": <float>, "entries": [<entry dict>, ...]}` — claimable
candidates only — stdout `{"scores": [{"id", "score", "reason"}]}`, one row
per input entry, finite numbers. Nonzero exit or a malformed row refuses the
rank phase and skips dispatch that iteration: **there is no fallback to the
formula you replaced.** A multi-objective domain can make its scorer
Pareto-aware — whether the goal is one scalar or a frontier is now a domain
decision. The toy domain ships a working example: `domains/toy/bin/score`.

## Optional: don't trust your own proxy

```yaml
# goal.yaml
goal:
  # ... objective, target, stop_when as before ...
  # Hold a met target until a second, independent run row — different claim
  # (different workspace session or owning entry) — also meets it.
  confirm_independently: true
```

AIRA (arXiv 2507.02554, §5.3) measured the failure this guards at 9–13
absolute points on MLE-bench: a search guided by its own proxy overfits, and
the agent's perceived score keeps rising after the true metric has plateaued.
The core enforces only that a second *execution* said so; what makes the
re-measurement honest (fresh data split, re-seeded run) is your `bin/measure`.
Without the flag, stopping is exactly as before.

## References

- [AIRA: AI Research Agents for Machine Learning](https://arxiv.org/abs/2507.02554)
  — search policies, operators, and the generalization gap (§5.3).
- [AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2) and
  [AIDE](https://github.com/WecoAI/aideml) — the tree-search lineage this
  release's structure comes from.
- [OpenEvolve](https://github.com/codelion/openevolve) — the MAP-Elites view
  the dial is a first step toward.
