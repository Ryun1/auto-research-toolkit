# Contributing to autoresearch

Thanks for contributing. This project extracts a working research harness into
something domain-agnostic, and its 142 self-filed defects are the design input.
Contributions should respect that: the record is the source of truth, and the
tests exist because every rule here was once violated by the code itself.

## Setup

```bash
uv sync --extra dev            # .venv/, from uv.lock, with pytest, pytest-xdist, ruff
./scripts/install-hooks.sh
```

Without uv, `python -m venv .venv && .venv/bin/pip install -e ".[dev]"` builds
the same environment, unpinned. Either way it must be `.venv/` at the repo
root: the hooks run `.venv/bin/python` and `.venv/bin/ruff` when they exist,
and fall back to whatever `python3` and `ruff` are on `PATH` — which may not
have this package or pytest installed.

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

Follow the existing style — a short area prefix, a colon, then a declarative
statement of what is now true, not an instruction to make it so. Read the
subject as the sentence the record should say after the commit lands:

```
CLI: a reader that closes the pipe is not a traceback
Budget: a zero money ceiling with nothing spent is not a stop (#15)
Claims: one session may present several claim tokens (#44)
```

Rules:

- The whole subject line, area prefix included, is at most 88 characters
  (aim shorter); no trailing period. The hook counts the line, not the part
  after the colon. Several subjects in the log run longer; the hook
  would refuse them, so do not copy their length.
- An issue the change answers is cited at the end, as `(#44)` above.
  Releases are `Release: vX.Y.Z`.
- The prefix names the subsystem touched (`Ranking`, `Budgets`, `Docs`,
  `Tests`, `Validate`, `Review fixes`, …). Invent one only if none fits.
- Start the summary lowercase when it reads naturally; digits and proper
  nouns are fine (`Migration: 412 prose entries -> records`).
- The body (if any) explains *why*, not *what* — the diff says what. Cite
  defect numbers from `docs/EVALUATION.md` when a change fixes one.
- Git's own `Merge …` and `Revert "…"` subjects are exempt.

The `commit-msg` hook enforces the prefix and the length; whether the summary
is declarative is for review. `--no-verify` is the escape hatch and should be
rare.

## Field defects from downstream projects

Projects running this toolkit export the defects they find in the core with
`ar harness export`; a person publishes the bundle (attach it to the issue or
PR that carries it). Ingest it into a receiving domain — one whose
`domain.toml` declares a track for field defects:

```bash
python scripts/ingest-defects.py bundle.json \
  --into /path/to/receiving-domain/state/entries --track field --prefix F
```

`--into` must be that domain's configured entry store: the script finds the
`domain.toml` enclosing it and refuses, before writing anything, a missing
domain, an undeclared track, a different store or a prefix the track does not
use. This repository's root is not a domain, and no shipped domain declares a
`field` track, so the command has to point at one that does (see "Filing
defects upstream" in `README.md`).

Each defect becomes an ordinary entry tagged `from:<project>/<id>` — cite that
tag, not the issue number, when you fix one, so the record and the upstream
tracker stay resolvable in both directions. A defect arriving without its
evidence is skipped with the missing field named, while the rest of its bundle
is filed, and the script exits 1 so the gap is not mistaken for a clean
ingest: an entry that cannot be reproduced is an opinion, and the gate lives
upstream at export for the same reason.

## Testing

- Full suite, fast: `uv run --extra dev pytest -n auto` (about 40 seconds on
  8 cores). A single file runs faster without `-n`.
- The pre-push hook and CI both run the serial `pytest -q` — nearly three
  minutes, the slow path, and the one that has to pass. `-n auto` is for the
  edit loop, not a substitute.
- Every behaviour change needs a test that fails without the change.
- New record fields, validators, or gate logic need tests for the invalid
  case, not just the valid one — invalid-but-tolerated data is how the
  original harness drifted.
- If you add a policy surface, test that it actually blocks.

## Lint and style

Ruff is the only linting tool (`ruff check .`). `[tool.ruff.lint]` in
`pyproject.toml` selects `E`, `F`, `W`, `I` (import sorting), `B` (bugbear) and
`UP` (pyupgrade), and ignores `E501`: prose-heavy docstrings and prompts are
judged by eye, not by a line limit. No style debates beyond that. The
pre-commit hook, the pre-push hook and CI all run the same config.

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
