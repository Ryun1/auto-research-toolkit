# AGENTS.md -- driving this research domain by hand

This repository is an `autoresearch` domain: a research record with enforced
invariants. You -- an agent in a terminal, or a person -- move the queue
through the `ar` verbs. There is no coordinator process to wait for.
`bin/ar` is the entry-point shim (bare `ar` collides with the BSD archiver on
macOS). Start every session with the board:

    bin/ar board         # goal, distance to target, queues, live claims
    bin/ar rank          # why the queue is ordered the way it is
    bin/ar entry list    # the entries themselves
    bin/ar budget        # every meter, and the stop decision

## The protocol: claim -> work -> measure -> close

- **A claim is the only write ticket.** `bin/ar claim ID --why "..." --max-runs N`
  before you work on an entry; `bin/ar release ID --why "..."` to hand it back.
  A verdict applied without a claim is exactly the race the claim lock exists
  to prevent.
- **Run experiments through `bin/ar measure`**, never around it: it writes the
  validated run rows that ranking and every meter read.
- **Close with evidence**: `bin/ar close ID <status> --memo path/to/memo.md`.
  The memo must exist, and any gates the track declares are enforced. A
  hand-applied closure gets no discount for being applied by a person.
- Stuck past your ceiling: `bin/ar release` with a reason. A claim abandoned
  by a dead session is freed with `bin/ar reap ID` once past its TTL.

## Researching

`bin/ar research "your question"` spins up scout agents whose proposals land
as ordinary entries, priced by the next `ar rank` -- **but only if domain.toml
names a `[brain]`**: scouts are model asks behind the brain seam, and the
command refuses without one. The same goes for `bin/ar skill distil`. In a
hand-driven domain without a brain, file ideas yourself instead:

    bin/ar entry new "title" --hypothesis ... --prediction ...

and let the next `ar rank` price them.

## Set the harness goal from goal.yaml

You are probably running inside a harness with a goal feature (`/goal <text>`
in most coding harnesses): use it. Read `goal.yaml` first -- the metrics, the
objective over them, the possibly-moving target, and `stop_when` -- and
translate it into a goal the harness will drive, e.g.:

    /goal iterate until a solution scoring +1% over the current best
    objective is measured: claim the top-ranked entry, work it, `ar measure`
    it, `ar close` it with a memo, re-run `ar rank`. Stop when goal.yaml's
    stop_when holds, or `ar budget` says the meters do.

A goal turns the loop's cadence into your persistence; the record keeps the
loop honest underneath it:

- An improvement is only real once `ar measure` has written the run row and
  `ar close` has accepted the evidence. A harness goal is motivation, never
  a licence: no claim, no verdict.
- `stop_when` and the yield floor in goal.yaml are the domain's own stop
  decision; `ar budget` shows every meter and the same decision. When either
  fires, say so and stop -- do not let the goal text push past a ceiling,
  which is the one thing this harness refuses to negotiate.
- The +1% is an example, not a rule: pick the step size goal.yaml implies
  (the target moves, `bin/probe-target` refreshes it) and restate the goal
  when the target moves under you.

## Fan out: you are probably a harness that can spawn subagents

This domain was built for concurrent drivers -- the record's claim protocol
drained one queue from 92 concurrent sessions. If your harness can spawn
subagents, use that instead of working entries one at a time:

- **One subagent per entry it should progress.** Do not split one entry across
  agents: a claim is per-entry and single-holder, and `ar claim` names the
  holder when it refuses. The claim **is** the coordination primitive -- two
  subagents claiming different entries need no other locking.
- **Each subagent passes its own `--session <name>`** (a global flag, before
  the verb: `bin/ar --session scout-3 claim Q-12 --why ...`). Claims, usage
  rows and history all attribute to that name; a dead subagent's claim is
  freed with `bin/ar reap Q-12` once past its TTL, and nothing else is
  touched.
- **The orchestrating agent keeps the judgement-heavy verbs**: `board`,
  `rank`, `entry new`, `close` (a closure's memo and gates are a verdict --
  one writer, not a fan-out), `budget`. Subagents claim, measure and report
  back; the orchestrator closes.
- **Subagent model spend is still out-of-band spend** -- record it under the
  subagent's session name (see below), or the ceiling refuses metered spend.

## Worktree isolation when the harness has none

One worktree per terminal or agent: `bin/ar session create NAME`, work inside
it, then `bin/ar session destroy NAME --harvest` when done -- the harvest
appends run rows to the primary record, deduped by run id. Destroy refuses on
drifted configuration or diverged entries; `bin/ar session prune` proposes
idle sessions and nothing is destroyed without those guards. If your harness
already isolates each subagent (its own checkout or worktree), that counts --
record the same name as the claim's `--session` either way.

## Money you spend is still money

Model or API calls you make are out-of-band spend, invisible to the budget
ceiling unless recorded -- and an unrecorded row eventually refuses further
metered spend:

    bin/ar usage record --tool <harness-name> --kind api --cost 0.42 \
        --session <your-session> --note why

## Policy binds you too

`[policy.human_only]` patterns in domain.toml are checked on every command
path: you may prepare such a command, never run it. `bin/ar policy` shows the
never-rules and proves each refuses something. `bin/ar validate` checks
records, views, runs and policy before you go.

## Do not start the coordinator

`ar loop` drives this domain autonomously and refuses to start unless
domain.toml names an explicit `[brain]` (and authorizes any spend). This
domain is driven by hand; leave `ar loop` alone.
