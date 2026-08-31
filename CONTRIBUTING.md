# Contributing to autoresearch

Thanks for contributing. This project extracts a working research harness into
something domain-agnostic, and its 143 self-filed defects are the design input.
Contributions should respect that: the record is the source of truth, and the
tests exist because every rule here was once violated by the code itself.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
./scripts/install-hooks.sh
```

The hooks script sets `core.hooksPath` to `.githooks/` for this clone only.
Run it once after cloning; it needs re-running only if you delete the config.

## Ground rules

1. **Core stays domain-agnostic.** Nothing in `src/autoresearch/` may know
   about a specific problem, metric, or domain. Domain knowledge lives in
   `domains/` (or an external project) behind the four documented seams:
   measurement, goal, policy, and guides/skills.
2. **The record is authoritative.** Every code path that observes something
   must write it to the record. If your change learns something at runtime,
   it belongs in a record field, not a log line.
3. **Budgets must be real.** Any new meter you declare must actually be
   spent (see `d9a7a16` for why). No decorative config.
4. **Mechanical before model.** If a check can be a formula, a validator, or
   a gate, write it as one. Reach for a model role only for judgement that
   cannot be mechanised.
5. **No speculative scaffolding.** No TODOs, no future-proofing, no "we might
   want this later". Every commit should leave the tree consistent and the
   full suite green.

## Commit messages

Follow the existing style — a short area prefix, a colon, then an imperative
summary of the change:

```
Ranking: reserve a share of every shortlist for amplitude
Budgets: spend the meters that were only ever declared
Docs: diagram the agent architecture
```

Rules:

- Summary ≤ 88 characters (aim shorter), no trailing period.
- The prefix names the subsystem touched (`Ranking`, `Budgets`, `Docs`,
  `Tests`, `Validate`, `Review fixes`, …). Invent one only if none fits.
- Start the summary lowercase when it reads naturally; digits and proper
  nouns are fine (`Migration: 412 prose entries -> records`).
- The body (if any) explains *why*, not *what* — the diff says what. Cite
  defect numbers from `docs/EVALUATION.md` when a change fixes one.
- Git's own `Merge …` messages are exempt.

The `commit-msg` hook enforces the format; `--no-verify` is your escape hatch
and should be rare.

## Testing

- Full suite: `pytest` (about 30 seconds).
- Every behaviour change needs a test that fails without the change.
- New record fields, validators, or gate logic need tests for the invalid
  case, not just the valid one — invalid-but-tolerated data is how the
  original harness drifted.
- If you add a policy surface, test that it actually blocks.

## Lint and style

Ruff is the only linting tool (`ruff check .`). Default ruleset plus
import sorting; no style debates beyond that. CI runs the same config as
the pre-commit hook.

## Pull requests

- Branch from `main`, keep PRs to one coherent change ("Ranking: …", not
  "a bunch of stuff").
- PR description: what changed, why, how you validated it, and any open
  risks or questions — the same shape the worker agents report in.
- CI must pass before review. Reviewers should check rule 1–5 above before
  reading the diff.
- Squash-merge with the commit-message format; PR number goes in the
  summary in parentheses if the merge produces one.

## Where to look

- `docs/ARCHITECTURE.md` — the seven phases and the model seam
- `docs/EVALUATION.md` — the defect taxonomy this design answers
- `README.md` — the four things a domain supplies
