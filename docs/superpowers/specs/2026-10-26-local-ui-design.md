# `ar ui` — a local, read-only UI for one campaign

Status: draft for approval, 2026-10-26.

## Why

A campaign's record is already rich: ECDSA Fail (`~/amanita/edcsa-fail-research`) holds
470 entries, 245 run files, 291 claims and 1,270 inbox items. But the only ways to see it
are `ar board` (text), the generated mermaid views, and the static `ar entry graph --html`
snapshot. What the user wants to see at a glance is: **what the campaign knows and how
that knowledge connects**, which agents are doing what, which experiments are running,
and how scores are moving.

Prior art (see "Prior art" below) shows the same handful of views everywhere: a lineage
graph coloured by outcome, a frontier plot, a runs table. What almost none of them do is
treat agents as visible entities. That is the distinctive part here.

## Goals (in priority order)

1. **Knowledge graph** — the first thing built and the thing to get right.
2. **Agents** — one lane per agent, with fine-grained activity where transcripts exist.
3. **Live board** — live claims with elapsed time, newest runs, distance to target.
4. **Frontier plot** — every run over time, best-so-far step line.

All four update live as files change.

## Non-goals (user's decisions)

- **No writes.** The UI never mutates the record. Release, promote, reap, close all stay
  in the CLI.
- **One campaign at a time.** `ar ui` serves the domain it was started in, like every
  other `ar` verb. No multi-repo panel.
- **Single user, localhost.** No auth, no sharing, no hosted mode, no export.

## Anti-goals (would make it a failure even if it works)

- **The UI disagreeing with the CLI.** Frontier, statuses, noise floor, suspect rows,
  rank — every number comes from the same core functions `ar board` / `ar rank --json` /
  `ar budget --json` / `ar validate` use. The UI computes layout, never verdicts.
- **A hairball.** 470+ entries plus skills, guides and memos must stay readable.
- **Weighing down core.** The toolkit depends only on PyYAML. A domain that never runs
  `ar ui` must not pay for it.

## Decided shape

### Command

`ar ui [--port 8484] [--host 127.0.0.1] [--open] [--no-branches] [--transcripts PATH...]`

Runs a server until interrupted. Binds to localhost by default.

### Backend — `src/autoresearch/ui/`

- **Server:** stdlib `http.server.ThreadingHTTPServer`. JSON endpoints plus static files,
  plus one Server-Sent Events stream that says "these resources changed" so the page
  refetches. No third-party Python dependency unless the builder finds one is truly needed;
  if so it goes in an optional `[ui]` extra.
- **API layer:** thin functions that call existing loaders (entries, runs, claims,
  iterations, skills, epochs, noise, rank, budget, tree) and return JSON. Where a CLI
  `--json` document already exists, reuse its builder rather than re-deriving.
- **Watcher:** mtime polling of `state/`, `data/runs/`, `inbox/`, `docs/skills/`,
  `guides/`, plus the branch and transcript sources. Polling (not fsevents) keeps it
  dependency-free; a 1–2 s interval is fine.
- **Source: session branches and worktrees (uncommitted work).** Enumerate `session/*`
  branches and `git worktree list`. For each, read entries, claims and run rows that differ
  from the checkout (via `git show`/`git ls-tree` for branches; directly from disk for live
  worktrees, including uncommitted files). These are overlaid on the main record and
  marked **uncommitted** with the branch name. `--no-branches` turns this off. Prunable
  worktrees (directory gone) fall back to their branch.
- **Source: agent transcripts (optional).** Claude Code transcripts in
  `~/.claude/projects/*/*.jsonl` are joined to an agent by `cwd` (a session worktree path or
  the domain root) and `gitBranch` (`session/<name>`). Extra roots via `--transcripts`
  (e.g. copied from another machine). Missing transcripts degrade to record-only data —
  today, ECDSA Fail's session transcripts are not on this Mac, so this is the common case.

### Frontend — static, no build step

- One `index.html` + plain JS modules + vendored libraries, shipped as package data. No
  CDN (works offline), no npm build.
- Libraries: **Cytoscape.js** with a layered layout (dagre or ELK) for the graph;
  **uPlot** for the frontier. Builder may substitute if something better fits, keeping
  "vendored, no build".
- Four tabs: Graph (default), Agents, Board, Frontier. A persistent header shows the goal,
  best, distance to target and a live/disconnected indicator.
- Selection is shared: picking an agent filters the graph to its entries; clicking a dot
  on the frontier opens that run's entry; etc.

### View 1: Knowledge graph

**Nodes**
- Entries, from all tracks (research `Q…`, harness debt `H…`, any others).
- Skills (`docs/skills/`) and guides (the domain's `knowledge` list).
- Inbox memos and artifacts referenced by entries.

**Edges**
- `parent` (branch lineage), `supersedes`, `related` — as in `ar entry graph`.
- Entry → skill where a skill was distilled from that entry.
- Entry → memo/artifact where the entry records the pointer.
- Entry → run rows is shown in the side panel, not drawn (there are thousands of rows).

**Encoding**
- Fill = status (same palette meaning as the existing mermaid view). Shape = node kind.
- Dashed outline = uncommitted (from a session branch/worktree), labelled with the branch.
- Pulsing ring = currently claimed, labelled with the holder.
- Red halo = **tainted**; amber = **downstream of tainted** (see poison propagation).

**Layout and scale**
- Layered layout on the lineage edges, roots at the top.
- Entries no edge touches are grouped into collapsible buckets by status and track, as
  the mermaid view already does.
- Skills, guides and memos are hidden by default behind toggles; the graph opens on
  entries only.
- Filters: track, status, agent, claimed/uncommitted/tainted, a text search over id,
  title and hypothesis. A "focus" mode shows one entry's ancestors and descendants only.
- Alternate **timeline** layout: x = created/closed time, one row per lineage root.

**Side panel** (on click)
- Title, status, hypothesis, prediction, verdict summary, gates, current claim.
- Its run rows as a small table, with valid/failed/invalid/suspect/excluded kept distinct
  and the noise-floor comparison where one applies.
- Linked memos and artifacts (markdown rendered; other files listed with their size).
- History (`ar entry show --history`) collapsed.

### Poison propagation

A run row is **tainted** when core already says it should not be trusted: it is
`invalid`, excluded by a constraint, held as `suspect`, or pinned to a superseded epoch
(`ar epochs`). An entry is **tainted** when its `confirmed` closure rests on tainted rows.
Everything downstream of a tainted entry along `parent` (and entries that cite it as
`related`) is **downstream of tainted**. The panel says *why*, naming the row and rule. The
UI only highlights; nothing is reopened.

### View 2: Agents

**Agent identity.** The claim's `session` value is the agent (e.g. `loop-lynx-m5z3`,
`grind-pickup-0905`). Joined to its `session/<name>` branch, its worktree, its run files
(e.g. `data/runs/aloof-egret-ek7x-slot0.jsonl`) and any transcripts. Coordinator slots
and `ar external` assignees are agents too, by the same key.

**One lane per agent** with:
- What it is doing now (live claim, entry, elapsed time, the claim's `why`).
- Its record: claims taken (live + archive), closures by verdict, runs measured, best row.
- Spend: from `ar usage` and the claim's budget field.
- A swimlane timeline of its claims and runs.

**From transcripts, where present:** last activity time, turn count, tool calls by name,
token use, and a scrollable recent-activity feed (tool name + one-line arg summary).

Lanes are sorted with live agents first, then by last activity. Finished agents can be
collapsed.

### View 3: Live board

The `ar board` content as panels:
- Goal, target, best, distance to target, and the stop decision.
- Live claims with elapsed timers.
- The newest run rows, with statuses kept distinct and a noise badge (gain ÷ noise floor:
  green ≥ 2, amber 1–2, red < 1).
- Queue heads.

### View 4: Frontier plot

- Every valid row: x = time, y = objective. Dot colour = agent.
- Excluded and suspect rows are greyed and hollow.
- Best-so-far step line, and the target as a horizontal line (it moves when the target
  moves).
- Hover shows the config knobs; click opens the entry.

## Testing

- API functions are unit-tested against `domains/toy` fixtures. Add a populated fixture
  (entries with lineage, runs, a claim, a branch) if the toy domain is too empty.
- An endpoint test boots the server on an ephemeral port.
- The frontend is checked by hand (and with Playwright screenshots) against the ECDSA Fail
  corpus, read-only.
- Run the suite with `uv run --extra dev pytest -n auto`.

## Build order (each step usable on its own)

1. Server, API and **knowledge graph** on committed state, plus the side panel. Live
   refresh.
2. Branch/worktree overlay (uncommitted) and poison propagation.
3. Agents view, record-only; then the transcript source.
4. Board and frontier.

The user wants to build and iterate: ship step 1, look at it on ECDSA Fail, adjust.

## Left to the builder

- Endpoint names and JSON shapes.
- Exact libraries/versions within "vendored, no build".
- Colours (keep the mermaid view's status meanings).
- How skill → entry and entry → memo links are discovered, from whatever fields the record
  already has (if a link isn't recorded, it isn't drawn — no prose parsing, per the
  harness's own rule).
- Poll interval and debounce.

## Prior art

- **Flywheel** (docs.flywheel.paradigma.inc): a canvas/timeline toggle over a node DAG; a
  side panel; a read-only local UI showing uncommitted agent work.
- **OpenEvolve / ShinkaEvolve / AIDE:** lineage trees coloured by score, with click-through.
- **Karpathy's autoresearch / Optuna:** a frontier with a best-so-far line.
- **pi-autoresearch:** the noise-floor badge.
- **research-tree:** poison propagation and superseded edges.
- **Langfuse:** an agent activity timeline.
