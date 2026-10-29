# Skills

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
distilled: {at: '2026-08-31T09:12:04Z', iteration: 14, core: 0.1.0}
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

A coding harness opened at the domain root reads them through `.claude/skills`,
a link `ar init` makes; a domain scaffolded before that runs `ar skill link`.

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
track with evidence (`ar entry new --track harness --core auto --repro ...
--observed ...`) — the map points at the records, it does not replace them.
