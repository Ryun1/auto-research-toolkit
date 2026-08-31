# Evaluating the auto-research harness

*What to port into `auto-research-toolkit`, what to leave behind, and what the record
says about why.*

Method: read every tool in `bin/` (24 executables, 7,165 lines), the two work queues,
the runbooks and architecture notes, the 13 guides, and — the most valuable artefact in
the repository — all 143 entries of `docs/log/Harness Debt.md`, which is a candid,
dated, evidence-backed log of this harness failing at itself. Where a claim below has a
number, it came from the record, not from an impression.

---

## 1. The record in numbers

| | |
|---|---|
| Elapsed | 2026-08-23 → 2026-08-31 (8 days) |
| Commits | 2,129 |
| Distinct agent sessions | 92 |
| Hypotheses filed | 271 — 151 refuted, 106 confirmed, 7 queued, 6 blocked |
| Harness defects filed | 143 — 134 fixed, 6 queued, 3 wontfix |
| Inbox memos | 918 files, 98,355 lines |
| Docs | 20,653 lines, of which the four append-only logs are 18,274 |
| Tooling | `bin/` 7,165 lines; `tests/` 7,460 lines; `guides/` 4,190 lines |

Two of these deserve to be read together.

**257 hypotheses reached a verdict in eight days.** That is a working research loop, and
the loop is the thing worth generalising. Very little agent scaffolding gets this far.

**143 harness defects in the same eight days, against 7,165 lines of tooling.** That is
roughly one filed defect per 50 lines of `bin/`. The scaffolding was, by volume of
recorded work, competitive with the research as a consumer of agent time. This is not an
indictment — a harness that *notices* 143 of its own defects is far ahead of one that
notices none. But it is the central design input for the core: **the failure classes are
knowable in advance, and most of them are structural rather than incidental.**

---

## 2. What is load-bearing — port it

These earn their place, and the record shows what happened when each was missing.

**Claim-before-work with published serialisation.** `bin/claim-next` + `claimlock.py`.
An agent may not start until a `queued → in-progress` transition has landed on `main`.
This is the single mechanism that lets 92 sessions share one corpus. Port wholesale.

**Evidence-backed closure with a typed closure kind.** `bin/close` refuses a status the
queue does not use and refuses to close anything whose evidence memo does not exist. A
refutation must additionally declare `mechanism | slope | cell` — whether it holds
outside the measured range, only inside it, or at one point. This is the sharpest idea
in the whole harness. It exists because the board twice asserted a direction was dead
from evidence that could not support it, and it converts "this is closed" from an
opinion into a claim with a stated re-open condition. **Port exactly, including
`--relabel` and `--reopen`.**

**Never-delete, append-only results.** A refutation is a result, and it is the thing
agents most reliably lose. 151 of 271 verdicts here are refutations; without them the
loop re-runs dead work forever. `guides/ecadd-explore` opens with a map of closed axes
precisely so the next agent does not re-open them. Port as a hard invariant — and in
core it becomes cheap, because records are addressed by id rather than by document
position (H117: a closed entry with no section at all).

**Two-lane publish, findings unreviewed.** Research output lands on `main` immediately
so the next agent sees it; changes to the harness itself go through a proposal branch.
The insight is not the review — you have said agents may self-merge — it is that
**changing the scaffolding changes every later session, and findings do not.** Port as
config-driven lanes.

**Disjoint-path concurrency instead of locking.** One writer per path, enforced by
`config/project.toml`'s `findings_paths` regex read by both `bin/publish` and `bin/stage`
so they cannot disagree. Port, with the boundary in domain config.

**Provenance on every measurement.** `provenance.py` records what was built and where.
The corpus contains a run row that claimed the harvesting session's identity (H22) and
rows whose builder had been patched with no record of it (H37, H63) — one of which could
"destroy a pooled analysis". Port and tighten.

**One reader per parse.** `bin/board`'s docstring states the rule outright: every section
reuses the reader that already owns its parse, because a second parse "would make this a
digest that can be wrong". Port the *rule*; delete the regexes it was protecting.

**Every reader reports how many things it read.** From H81/H96: a board that says "no
live claims" must not be indistinguishable from a board that parsed nothing. This is a
one-line discipline that catches an entire failure class. Port as a core convention and
test it.

---

## 3. Seven failure classes, and the invariant each becomes

I classified all 143 debt titles. Entries overlap classes; the point is the shape, not a
partition.

### 3.1 State stored twice, in two formats (~12 entries)

H01 is literally titled *"Two encodings of claim state"*. Status lives in the queue
prose **and** in `state/claims/**`, and the header has to instruct agents not to edit it
by hand. The consequences run the length of the log: a fix lands on `main` while its
entry still reads `queued` (H72); an entry carries two `Result` fields, with `bin/close`
writing the first and `bin/lint-entry` reading the first, leaving six live entries
showing a closed status against a Result of "—" (H93); `--reopen` leaves the old Result
in place so `lint-entry` refuses the entry it just created (H137); a closure label cannot
be corrected without reopening the entry (H133).

> **Core invariant.** One record per entry is the sole truth. Every markdown view is
> generated and never parsed back. `ar validate` fails if a generated view is dirty in
> git — a hand-edit becomes a build failure instead of a silent divergence.

### 3.2 Prose parsed by regex, failing silently (~17 entries)

The flagship is H81: the queue heading regex was derived in four places, and swapping
the em dash for an en dash took every copy from 80 matches to **0** — reported as "no
duplicates" and a clean pass. `bin/queuedoc.py` was created to own that parse; H106 then
found the heading derived in **six** independent places while `bin/validate`'s own
warning comment said four, and H132 found the *Status-line* regex derived in six places
across five scripts, two byte-identical. Meanwhile H52 (a superseded constant followed by
a comma), H41 and H47 (all-digit short SHAs), and H55 (two-digit constants) are all the
same defect wearing different clothes.

> **Core invariant.** No regex over prose reaches a decision. State is schema-validated
> structured data; prose is a rendering target. The class is not mitigated, it is
> deleted.

### 3.3 Guards that pass vacuously (~10 entries)

H89: *nothing asserts either pre-commit hook refuses anything; deleting rule 1's
enforcement leaves the whole gate green.* H87: a commit deleted the whole of H78's
`--release`, tests included, and `bin/validate` passed **because the guards went with
it**. H05: `bin/validate`'s prose greps were being counted as tests. H101: a guide's
verification block reported OK having checked strictly less than the gate.

> **Core invariant.** Every guard ships with a **negative test** proving it refuses
> something. A guard with no proof-of-refusal is not a guard, and `ar validate` rejects
> the suite if one exists. Assertions on match counts are mandatory wherever a search can
> return zero.

### 3.4 Constants copied instead of referenced (~14 entries)

The docs already state the right rule — the newest *Derived constants* table is the only
place `T`, `Q` and break-even are written, and everywhere else states the relationship
and links. `bin/gate` even substitutes `{break_even}` rather than letting an author type
a number. And yet: H21 (`bin/gate` re-derived the constants instead of reusing the
existing parser), H102 (the parser only understood heading-form tables, so the newest
table was invisible and the board was policed against a **superseded `T` and a retired
break-even rate**), H138 (the tool printed "4 constants rows are unpoliced" on every push
and let a retired number from exactly those rows through, because the warning read as
noise), H141 (a four-heads-stale gap printed beside a current row for three days), H126
(the clone two promotions behind what the docs called the head).

> **Core invariant.** Constants are a typed goal object with one computed source.
> Derived values are expressions, never stored numbers. A stale read is an error, not a
> warning — H138 is the proof that a warning nobody can act on is worse than nothing.

### 3.5 Ownership and liveness guessed rather than known (~22 entries)

The largest class. The claim lock recorded no holder, so a crashed session wedged
claiming for every agent on the machine (H32). `wt_pool.sh destroy` had no ownership
check, so one session's teardown removed every other session's slots (H57), and `init`
took no hold, so a slot being measured in read `held:-` and was destroyed under its owner
(H59). The reaper could never reap a session that committed once and died (H30), read a
`--reserve` commit as proof of life forever (H114), and kept claims alive because prose
merely *cited* a session (H115). H107 found two definitions of a dead session 8× apart in
two different files, neither in config.

Every one of these is the same root cause: **liveness was inferred from artefacts,
because nothing observed the worker directly.**

> **Core design.** The coordinator owns the worker pool lifecycle and therefore *knows*
> when a worker finished. TTL reaping survives only for sessions running outside a
> coordinator, and both numbers live in config with their predicates documented as
> distinct — the existing `config/project.toml` comment on this is excellent and should
> be carried over as prior art.

### 3.6 Results escaping the record (~17 entries)

H142 is the one to read: `bin/harvest` is *documented* as the guard against a worktree
vanishing with its data, but reads only `results.tsv` — so a driver writing its own
layout is outside the net, and **470 MB of a completed corpus survived by 11 hours of
luck.** Beside it: λ counts never reaching `data/runs` so a sweep could not be
re-analysed (H24); the circuit source behind a *confirmed* result living only in the
gitignored clone (H39); a run row unable to say its builder diverged from the base
(H63); undeclared dumps invisible to harvest (H64); harvest destinations colliding
across sessions so two sessions' rows overwrote each other (H100, H129); four rows in
the corpus claiming `correct = OK` with **zero Toffoli**, which would score as a perfect
0, uncaught by validation (H134).

> **Core invariant.** A result is not produced by convention, it is *returned* through a
> typed interface. `bin/measure` emits a validated record or fails; the core, not the
> domain, decides where it lands. Anything a worker writes outside its declared outputs
> is not evidence and cannot back a closure.

### 3.7 The loop unable to express a state it needed (~11 entries)

This class is the most interesting, because each entry is the loop discovering it had no
legal move. There was no supported way to release a claim, so an agent that claimed
out-of-lane work had to "close it dishonestly or squat" (H78). `blocked → queued` had no
writer, so a stale block was permanent by construction (H111). Claim order could not be
overridden, so the loop could not act on its own findings (H10). A cross-cutting finding
could not re-price the entries it invalidated (H09). The loop had no way to report
"audited, found nothing" (H26). The harness-review loop could never drain a queue of one
or two, because "fewer than three queued, audit instead" forbade the drain — so H90 was
skipped by every iteration after it was filed (H95). `claim-next` re-handed an entry to
the session that had just released it, looping the same agent on the same block (H113).
And H125, still open: **a claim can be worked indefinitely; nothing says when to stop.**

> **Core design.** The state machine is declared up front and total: every state has an
> entry and an exit writer, and `ar validate` proves reachability — no state may be
> terminal by omission. Budgets are meters, not free text. Ranking is a computation the
> loop can re-run on its own findings, not a prose essay only a human can write.

---

## 4. The kill list — do not port

Core should not inherit 24 tools by default. Each of these stays behind, with the reason.

| Not ported | Why |
|---|---|
| `bin/run`, `bin/harvest`, `bin/grind-gate`, `bin/mine-notes` | Entirely ECDSA. They become the domain's `bin/measure` and domain tools. `mine-notes` is additionally the subject of H130 **and** H131 — it dies without a binary that rule 3 guarantees the curator cannot have, which is why four consecutive curations recorded "not run". A tool the loop structurally cannot run is not a tool. |
| `bin/queuedoc.py` | Its whole purpose is to own a regex over prose. Structured state deletes the need. Port the *lesson*, not the module. |
| `bin/staleness` (25k) | Exists to police copied constants in prose. A computed goal object removes the problem it solves. Its *gap arithmetic self-test* is worth keeping as a pattern. |
| `bin/reprice` | Answers "which entries quote numbers a frontier move invalidated" — a report only needed because entries store numbers as prose. In core, a re-price is a recomputation of ranking. |
| `bin/lint-entry` (32k) | Schema validation of prose. Replaced by the record schema. |
| `bin/gate` | The `Gate:` precondition is a genuinely good idea and becomes a **typed field** on the entry, evaluated by ranking's hard filters. The string-substitution tool does not survive. |
| `bin/orphans` | "Memos reachable from nothing" (H109: 71 of them, and a front-matter index would miss 79%). Reachability is a graph property of records; it stops being a discoverable defect. |
| `bin/board` | Superb docstring, wrong shape: it exists because a cold session had to read ~8,000 lines of append-only prose (H108). Core renders a board from records. **Keep its two rules verbatim** — reuse the owning reader, and report how many things you read. |
| `bin/qarton-trace` | Already dead by the repo's own finding: 183 lines serving a closed programme, sitting in `bin/` where every session reads the set of live tools (H103). Cited here as the standard core should apply to itself. |
| The "fewer than three queued → generate instead" rule | H95 proved it deadlocks: it forbids draining a short queue. Replaced by continuous generation running *in parallel* with the work. |
| `--budget` as free text | H125. Replaced by real meters. |
| Prose re-rank passes | ~30 of them, ~1,500 lines in `Research Directions.md`, and H140 caught one ranking a terminal entry four hours after it closed. Replaced by a scored ranking with a recorded justification. |

---

## 5. What is ECDSA-shaped, and where the seams are

Domain coupling is more concentrated than it first appears, which is good news.

- **Fully domain:** `bin/run`, `harvest`, `grind-gate`, `mine-notes`; `schemas/run-v1`;
  all of `docs/`; 10 of 13 guides.
- **Domain constants leaking into generic tools:** `T`, `Q`, break-even and the *Derived
  constants* table are parsed by `staleness`, and consumed by `gate`, `reprice` and
  `board`. This is the single seam that makes four otherwise-generic tools
  un-reusable — and it is exactly what the typed goal object replaces.
- **Already generic:** `session`, `claim-next`, `close`, `publish`, `stage`, `validate`,
  `reap-claims`, `claimlock.py`, `provenance.py`, `cliflags.py`, the hooks, and 3 of 13
  guides (`harness-review`, `research-audit`, `research-strategy`).

`cliflags.py` deserves a note: it exists because **eight tools silently ignored an
unrecognised flag and ran their default mode instead, so a mistyped flag read as a
successful run of what you asked for** (H135). That is a one-module fix to a
whole-harness class, and core adopts unknown-flag refusal from the first commit.

---

## 6. The four gaps that block autonomy

Everything above is about correctness. These four are about capability, and they are why
the core exists rather than a refactor.

1. **State is prose.** ~18,000 lines of append-only logs are the system of record,
   addressed by regex. Sections 3.1 and 3.2 are the tax.
2. **Hypothesis selection is document order.** `claim-next` hands out the topmost queued
   entry "regardless of fit" (H92). Re-ranking requires a human writing an essay. There
   is no priority field to sort on.
3. **No goal object and no meter.** The objective is prose in `AGENTS.md`; distance to it
   is recomputed by hand in every re-rank; H125 is open.
4. **Fan-out is trapped in one skill.** `guides/ecadd-explore` dispatches parallel
   subagents well — it even documents that two sessions built the same instrument within
   hours because neither could see the other starting (H60) — but the loop above it is
   strictly one agent, one claim, one iteration.

---

## 7. What this means for the core

The evaluation converts into seven testable invariants, each traceable to entries above:

1. One record per entry; every view generated; a dirty generated view fails validation.
2. No regex over prose reaches a decision.
3. Every guard ships a negative test proving it refuses something.
4. Constants are computed from a typed goal, never stored as prose.
5. Liveness is observed by the coordinator; inference is the fallback, not the design.
6. Results are returned through a typed interface; undeclared output is not evidence.
7. The state machine is total, and every budget is a meter.

Read alongside them, two conventions inherited verbatim from this harness because they
are already right: **reuse the reader that owns the parse**, and **every reader reports
how many things it read.**

The harness being evaluated here is better than its defect count suggests. Almost
everything in section 4 is on the kill list because it was *compensating* for prose being
the system of record — and a harness that filed 143 defects against itself in eight days,
with dated evidence and honest `wontfix`es, is one whose authors were paying attention.
The core's job is to keep the judgement and delete the compensation.

---

## Appendix: how the core's own defects were found

Written after the fact, and reorganised once it became clear that *how* each
defect surfaced was more interesting than what it was. Eleven defects were found
in the core during this work, by three different mechanisms — and **none of them
by reasoning about the code.** Each mechanism found a class the others did not.

### Tier 1 — caught by running it against real data

Four defects survived design, review-by-author, and a passing test suite, and
died the first time the code met 412 real entries and 9,443 real rows. Three are
failure classes this document had just finished cataloguing, reproduced by the
person who catalogued them.

**1. The policy selftest produced a false alarm — the H138 shape exactly.**
Probes were synthesised from each rule's pattern by replacing `*` with `x`. For a
glob that works; for a rule whose pattern is a regex it produced a probe the rule
could not match, so a **working** rule was reported as refusing nothing. A guard
that cries wolf trains the reader to skip the real ones. Fixed by trying several
probe shapes and letting a rule declare the `example` it must refuse.

**2. Mapping `Lane:` into `mechanisms` silently killed the research queue.**
`mechanisms` is the exclusion vocabulary: a `mechanism`-kind refutation sharing a
tag hard-excludes an entry. Migration mapped each entry's `Lane:` there because
it was the one structured classifier in the prose — so a single refutation in a
lane excluded **every queued entry in that lane**, and the real corpus ranked
five harness entries and zero hypotheses. A lane is a category; a refutation does
not close a category. Fixed by separating `tags` (never excludes) from
`mechanisms`. The ranking still looked plausible, which is the argument.

**3. The staleness term was a constant factor wearing a penalty's name.** It
counted total closed entries; against 400 verdicts that put every entry at 0.048
and ordered nothing. A penalty applied equally to everything is not a penalty.

**4. `_map`'s failure path lost which item failed** — in the function whose
docstring said it never loses a failure silently.

### Tier 2 — caught by rechecking finished work

**5. The domain shipped as the getting-started example did not pass the gate.**
`domains/toy` — named in the README as the thing you run first — had accumulated
the debris of a manual walkthrough, and a later `--reopen` left its generated
view out of step with its records, so `ar validate` on it exited 1. Every test
used a fixture that copies the domain and **wipes exactly those directories
first**, so 146 passing tests said nothing about what was actually committed.

H101's shape precisely: a verification that reported OK having checked strictly
less than the gate. The fix is a test that checks the artefact rather than a
cleaned copy of it, and the lesson generalises furthest of any here: **a test
suite that normalises away the state it is meant to inspect is not testing the
artefact.**

### Tier 3 — caught by an independent review

Six more, none of which the author found in two passes over the same code. The
review raised seven; one was checked against the running system and not accepted.

**6. Measurement attributed whichever row finished last.** The domain adapter
took the globally newest run row across every ledger file with a recent mtime.
The same config file sets `max_parallel = 3`, and the harvest step merges *every*
worktree's results — so three parallel workers would all see freshly-modified
files and all claim the same row, two of them attributing another
configuration's numbers to their own knobs. Silently, and in the direction that
looks like a clean result. `timestamp` is integer seconds across all 9,453 rows,
so even sequential runs tie, and the tie broke on filename order. This is
failure class 3.6 — results escaping the record — arriving by a route the
original corpus had not found.

**7. Every failing run was reported as `invalid`.** A run whose experiment fails
its validity gates has still *measured* its axes, and in this domain that is most
of the corpus. Reporting them as unmeasured would make every refutation taken
from a failing configuration unusable as evidence. Three outcomes were needed
where two had been written.

**8. A guard promised in a docstring was never called.** The domain's frontier
probe documented the H102 protection at length and then did not invoke the
function that provides it — which the tool it borrowed from *does* call. Failure
class 3.3, in prose form: the documentation asserted a property the code did not
have.

**9. Two axes were inverted, and the verification could not fail.** The
constants table prices "one Toffoli" in *qubits* and "one qubit" in *Toffolis* —
the label names the thing being priced and the value is in the other unit. The
probe matched on the label and got both backwards. It was invisible because the
only consumer multiplied them, and multiplication commutes.

The author had "verified" this by checking that `903,471 × 1,264` equals the
printed score. **That test cannot fail regardless of whether the axes are
correct.** It is Tier 2's lesson again, one level down: not a suite that
normalises away the state, but a single assertion that could not have detected
the defect it was written to rule out. Worth stating as a rule — *an assertion
that passes under the bug is not evidence, however specific its numbers look.*

**10. The workspace directory was not ignored by version control.** The
publishing tool refuses any non-empty working tree, untracked paths included, so
the first coordinator run would have blocked every later publish — and as a new
top-level path it could not have been committed alongside findings either.

**11. The core's schema id collided with the domain's own.** Both were called
`run-v1`, with entirely different required keys, so the obvious thing — appending
a core record into the existing ledger — produced a row the domain's validator
rejects, under a name asserting it should pass.

### The finding that was not accepted

The review reported that a TOML sub-table bound to the wrong track, leaving the
research queue with no terminal statuses and no closure-kind requirement. Loading
the configuration showed otherwise: the research track falls through to the
default terminal set, and its `refuted` status does require a closure kind. The
binding is intended and the behaviour correct.

Recorded because a review is evidence, not a verdict, and a corpus that logs only
the accepted findings misrepresents what review costs and what it is worth.

### What this says about verification

Eleven defects, three mechanisms, zero found by reading the code:

| mechanism | found | the class it is good at |
|---|--:|---|
| running against real data | 4 | assumptions that are wrong about *scale* — 400 verdicts, 9,443 rows, a real lane structure |
| rechecking finished work | 1 | the gap between what is tested and what is shipped |
| independent review | 6 | concurrency, and defects whose own verification was circular |

The third row is the one to take seriously. Every defect a review found had
already survived the author's own checking twice — and the sharpest of them was
protected by an assertion the author had written and watched pass.

### What the migration says about the source corpus

The migration is also an audit, and the source harness comes out of it well:

| | |
|---|---|
| Entries migrated | 412 (270 hypotheses, 142 harness-debt) |
| Disagreements between section, Closed table and claim record | **0** |
| Closed entries whose evidence memo is missing from disk | **0** |
| Open entries needing a price before ranking | 11 |

Zero missing evidence across 401 closed entries is the strongest single
statement in this document about the discipline of that harness. The rule that
`bin/close` refuses to close anything whose memo does not exist was worth every
line it cost, and it is ported unchanged.

The one apparent contradiction — Q241's section reading `refuted` against a claim
record reading `confirmed` — turned out to be an ordinary history: confirmed,
reopened on a discharged premise, re-closed refuted, with the claim record
keeping its pre-reopen verdict. A first version of the migration reported it as a
conflict. It is now preserved as a reopen event, which is where that reasoning
belonged; it existed nowhere else.
