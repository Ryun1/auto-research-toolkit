# Member domains: one checkout, several goals

Status: proposed, 2026-11-02. Source: starks-research, which has run five domains from one
checkout since 29 Oct 2026 without any toolkit support.

## Why

The core runs one goal per domain. A campaign with several goals (starks: a T3 lower floor,
three Lean proof targets and a set of kernel-checked upper bounds) therefore becomes several
domains. starks did this by hand:

```
starks-research/            domain.toml  prefixes Q, L, H   (the "umbrella")
  domains/t3-upper/         domain.toml  prefix U
  domains/t1/               domain.toml  prefix A
  domains/t2/               domain.toml  prefix B
  domains/bounds/           domain.toml  prefix C
  tools/ lean/ third_party/ shared by all five through ../../ paths
```

Each member has its own state, AGENTS.md, `bin/` shims and a full `domain.toml`, driven with
`bin/ar --domain domains/<x> <verb>`. This works. The core's one-domain model holds up, and
`discover()` already finds the nearest `domain.toml`, so running from inside a member works
without `--domain`. What it costs, from the starks record:

1. **Every curator does the cross-domain work by hand.** Twelve curator memos open with
   "read the boards, ranks, validate and budget output of all five domains" and close with a
   hand-written "Ranking across domains" section: twenty commands per pass, then a merge in
   prose.
2. **Ids do not resolve across domains.** AGENTS.md says "cite entries across domains by id
   (Q70, U4)". `ar close Q58 superseded --superseded-by C1` refuses, because C1 is not in Q's
   store (starks H9). Q58 and Q93 now cite a GitHub URL to an entry file, a link that breaks
   on a rename and is not checked by `ar validate`.
3. **Configuration is copied and drifts.** Each member `domain.toml` is about 80% a copy of the
   umbrella's (`[policy]`, `[budgets]`, `[upstream]`, `[coordinator]`, the comments). The
   copies already differ in `min_core` and `iteration_max_spawns`, and a human-only rule added
   to the umbrella does not reach the members.
4. **Harness defects have nowhere to go.** Members declare no defect track; a comment says
   "Harness defects go on the PRIMARY domain's H track". `defect_track()` returns None in a
   member, so `ar harness export` there refuses, and a loop running in a member has no
   defect track to file on.

## What this design adds

A thin layer over the existing domains, not a new kind of domain:

- an umbrella `domain.toml` lists its **members**;
- members **inherit** the umbrella's shared tables and override what differs;
- entry ids **resolve across** the umbrella and its members by prefix;
- `board`, `rank`, `budget`, `validate` and `reap` take **`--all`**;
- members **share the umbrella's defect track**.

A member stays a complete domain: its own goal, measure, state, claims, budgets and loop. A
domain with no `[domains]` table behaves exactly as today.

### Non-goals

- **One ranking across goals.** Members measure different things (counts, proof progress,
  bounds checked); their scores do not compare. `rank --all` shows each member's ranking
  side by side. Choosing which goal gets attention is a curator's judgement, and starks shows
  curators doing it well.
- **A loop that switches between members.** `ar loop` runs one domain. Running several loops
  is a scheduling question for the operator.
- **Nesting deeper than one level**, and members outside the umbrella's directory tree.
- **`ar ui` across members.** #59 is scoped to one campaign per server; revisit once members
  exist.

## Design

### 1. Declaring members

```toml
# umbrella domain.toml
[domains]
members = ["domains/*"]       # globs relative to this file; each match holds a domain.toml
```

- `members` is a list of relative globs. Each match must contain a `domain.toml`; a glob that
  matches nothing is an error (default-deny, like `[lanes]`).
- A member's `domain.toml` does not name its umbrella. The umbrella is found by walking up
  from the member root to the nearest `domain.toml` whose `[domains].members` matches it. This
  keeps a member loadable on its own and keeps the relationship in one place.
- New `Workspace` object (in a new `members.py`, since `workspaces.py` already means worker
  slots): `Workspace.load(root)` returns the umbrella config plus member configs, keyed by
  member path. `Workspace.for_domain(config)` returns the workspace a domain belongs to, or a
  one-domain workspace when there is no umbrella.
- **Prefixes must be unique across the workspace.** Loading refuses two members, or a member
  and the umbrella, that share a prefix, naming both. This is what makes cross-domain ids
  unambiguous with no namespace syntax.
- `ar domains` lists the umbrella and its members: path, name, prefixes, open/closed counts.

The CLI verb is not `workspace`: `ar workspace` already manages worker slots.

### 2. Inheritance

A member inherits these umbrella tables unless it sets them:
`[policy]`, `[budgets]`, `[upstream]`, `[coordinator]`, `[skills]`, `[hardware]`,
`[remote]`, `[brain]`, `[session]`.

Never inherited, because each is the member's identity:
`[domain]`, `[state]`, `[[tracks]]`, `[commands]`, `knowledge`, `[lanes]`, `[challenge]`,
`[evaluator]`, `[holdout]`, `[render]`, `[plugins]`.

Rules:

- **Key-level merge within a table.** A member that sets
  `[budgets] iteration_max_spawns = 10` keeps every other umbrella budget.
- **`[[policy.human_only]]` rules accumulate.** A member gets the umbrella's rules plus its own
  and cannot drop one. A safety rule that a member could quietly shadow would be a comment.
  `spend_ceiling` takes the smaller of the two values.
- **`[upstream] min_core` takes the higher floor**, since one core install serves every member.
- The merge happens in `DomainConfig.load` before validation, so every existing check sees the
  effective config.
- `ar domains show <member>` prints the effective config with where each key came from
  (`umbrella`, `member`, `default`). Without this, inheritance is a debugging trap.
- `[lanes]` is not inherited, because lane regexes are relative to a domain root. The work on
  this item also has to check how git-relative paths reach `lanes.classify`: `tamper.py`
  classifies paths from `git diff`, which are relative to the repository root
  (`domains/bounds/state/...`). A member's lanes are written relative to the member root
  (`^state/entries/`). Paths must be rebased onto the classifying domain's root before
  matching. This is likely a live mismatch today and should get a test either way.

### 3. Ids across members

New `members.resolve(config, entry_id) -> (DomainConfig, Entry)`: find the owning domain by
prefix across the workspace, then load from that domain's store. A plain id that no domain in
the workspace owns stays an error.

Where it is used:

| Surface | Today | With members |
|---|---|---|
| `close X superseded --superseded-by ID` | local store only, else a scheme URL | resolves across members; the replacement in the other member gets `supersedes: [X]` written to its record |
| `sources` items that are entry ids | counted as `cites` links only if local | resolve across members; `links.py` produces a cross-domain `cites` link |
| skill `cites` and inline `[ID]` | "not in the store" | resolve across members; terminal and staleness checks run on the owning domain's entry |
| `ar entry show ID` | local only | shows an entry from any member, saying which |
| `parent`, `derived_from` | local only | **stays local.** Tree caps (`tree_max_depth`, `tree_max_children`) and stagnation pruning are per domain; a cross-domain parent would escape them. Refused with a message pointing at `sources` |
| `validate` | dangling local links | a cross-domain id that no member owns is dangling; one that resolves is clean |

Writing `supersedes` into another member's record changes two stores in one command. Both are
in the same git repository, so they commit together. The other member's entry needs no claim,
because `supersedes` is a back-reference, the same as today's local case.

**Migration:** `ar migrate` (or a one-off `ar entry amend`) rewrites a superseded-by URL that
points into a member's `state/entries/<ID>.yaml` back to `<ID>`. starks has two (Q58→C1,
Q93→C2).

### 4. `--all`

`board`, `rank`, `budget`, `validate` and `reap` take `--all`, run from the umbrella or any
member:

- **Prose:** one section per domain, umbrella first, each headed with its path, name and goal
  line. Each section is what the verb prints today for that domain alone.
- **`--json`:** `{"domains": {"<path>": <today's json for that domain>}}`, so existing parsers
  only need to step into one key.
- **Exit status:** `validate --all` fails if any member fails, and names each one.
- **`rank --all` does not merge.** Each member's own ranking, with its own risk dial and
  budget, under its own heading.
- `reap --all` lists reapable claims across members. Reaping still takes one id, resolved
  across members.

This is the twenty commands a starks curator runs per pass, turned into five.

### 5. A shared defect track

- `defect_track(config)` falls back to the umbrella's defect track when the domain declares
  none.
- `ar entry new` with a prefix the member does not own but the umbrella's defect track does
  (`H`) files into the umbrella store, saying so.
- A loop running in a member files defect proposals on the umbrella's track, stamped with
  `core`.
- `ar harness export` from a member exports the umbrella's track. One bundle per checkout,
  since the core is shared.
- A member that declares its own defect track keeps it. Nothing forces a single track.

### 6. Scaffolding and docs

- `ar init --member domains/<name> --prefix C` from an umbrella scaffolds a member: a minimal
  `domain.toml` (`[domain]`, `[state]`, one `[[tracks]]`, `[commands]`), a `goal.yaml` stub, a
  `bin/measure` stub and an AGENTS.md that points at the umbrella's. It also appends the
  member to `[domains].members` if no existing glob covers it.
- ARCHITECTURE.md gets a "Member domains" section; MIGRATION.md gets the starks migration
  (delete the inherited tables from each member, add `[domains]`, rewrite the two URLs).

## Order of work

```
1  [domains] members, Workspace, prefix uniqueness, `ar domains`
├─ 2  inheritance (+ lanes path rebasing check)
├─ 3  ids across members  (fixes starks H9)
├─ 4  --all on board/rank/budget/validate/reap
└─ 5  shared defect track
6  `ar init --member`, docs, starks migration           (after 2–5)
```

Items 2 to 5 depend only on 1 and can land in any order.

## Acceptance against starks

- The four member `domain.toml` files shrink to their identity tables, and
  `ar domains show domains/t1` reports the same effective budgets and policy as today.
- `bin/ar close Q58 superseded --superseded-by C1` works, and C1 records `supersedes: [Q58]`.
- `bin/ar board --all` and `bin/ar rank --all` replace the five-board read in a curator pass.
- `bin/ar --domain domains/bounds harness export` exports H1–H12.
