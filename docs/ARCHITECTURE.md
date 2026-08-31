# Architecture

Five diagrams. The first says who owns what; the rest expand one iteration,
the roles inside it, the entry lifecycle, and the worker pool.

Everything here is drawn from `src/autoresearch/driver/loop.py` (the
coordinator), `driver/brain.py` (the model seam), `states.py` (the lifecycle)
and `workspaces.py` (the pool).

## 1. The three layers

A domain supplies measurement, goal, policy and knowledge. Core owns the loop,
the record and every meter. The brain is the one seam judgement passes through,
and it is swappable — which is what makes `ar loop` a unit test rather than a
bill.

```mermaid
flowchart TB
    subgraph domain["Domain (yours)"]
        direction LR
        measure["bin/measure<br/>one experiment → one record"]
        goal["goal.yaml<br/>metrics, objective, target"]
        policy["domain.toml<br/>policy, budgets, hardware"]
        guides["guides/<br/>knowledge agents read"]
    end

    subgraph core["Core (autoresearch)"]
        direction LR
        coord["Coordinator<br/>driver/loop.py"]
        store["Store<br/>one record per entry"]
        rank["rank.py<br/>formula over recorded numbers"]
        budget["budget.py<br/>every ceiling is a meter"]
        render["render.py<br/>every view is generated"]
    end

    subgraph brainlayer["Brain (the one seam)"]
        direction LR
        sdk["SDKBrain<br/>Claude Agent SDK"]
        scripted["ScriptedBrain<br/>offline, no model"]
    end

    domain --> core
    core -->|"role + brief (JSON)"| brainlayer
    brainlayer -->|"Reply: JSON + cost_usd"| core
    coord --- store
    coord --- rank
    coord --- budget
    coord --- render
```

Core never calls a model directly and the brain never touches the record: a
role returns JSON, and the coordinator decides what — if anything — that JSON
is allowed to change.

## 2. One iteration, seven phases

Every phase is metered and records what it *read* and what it *did*, so a phase
that did nothing is distinguishable from a phase that did not run.

```mermaid
flowchart LR
    start(["run_iteration(n)"]) --> orient
    orient["orient<br/>read record, resolve target,<br/>reap dead claims"]
    generate["generate<br/>N generators, in parallel"]
    rankp["rank<br/>score, then judge may reorder"]
    dispatch["dispatch<br/>claim → worker → verdict"]
    curate["curate<br/>re-price what moved,<br/>write views"]
    qc["qc<br/>mechanical first, model second"]
    stop{"should_stop?"}

    orient --> generate --> rankp --> dispatch --> curate --> qc --> stop
    stop -->|"running"| orient
    stop -->|"target met /<br/>budget spent /<br/>yield floor"| done(["stop"])

    teardown["pool.release_all()<br/>in a finally"]
    dispatch -.-> teardown
    qc -.-> teardown
```

Three things about this shape are deliberate:

- **Generate runs every iteration**, concurrently with the work, so the queue
  cannot starve behind a rule that forbids draining it.
- **The coordinator owns the pool.** Teardown is a `finally`, not a runbook.
- **QC is mechanical first.** `ar validate`-style checks answer most of it with
  no model; the QC role is asked only about what code cannot check.

## 3. Roles, and what each may do

Five prompts in `src/autoresearch/agents/`. Only the worker and curator get
write tools; everyone else is read-only.

```mermaid
flowchart TB
    coord["Coordinator"]

    coord -->|"brief: goal, open entries,<br/>closed_directions, host, budget"| gen["generator ×N<br/>read-only"]
    gen -->|"proposed entries"| file["file new entries<br/>(dedup by title)"]

    coord -->|"brief + ranking + excluded"| judge["judge<br/>read-only"]
    judge -->|"vetoes: promote/demote/drop"| veto["apply_veto<br/>may not touch the excluded list"]

    coord -->|"brief + entry + workspace"| worker["worker ×K<br/>Read/Grep/Glob/Bash/Write/Edit<br/>in an isolated workspace"]
    worker -->|"verdict, memo, closure_kind"| apply["_apply_verdict<br/>memo must exist;<br/>refutation must name a kind"]

    coord -->|"brief + this iteration's verdicts"| cur["curator<br/>read + write"]
    cur -->|"reprice: confidence/impact/cost"| repr["never a closed entry"]

    coord -->|"brief + mechanical problems"| qc["qc<br/>read-only"]
    qc -->|"problems, harness_debt"| debt["file debt on the<br/>harness track, not research"]

    file --> store[("record<br/>one file per entry")]
    veto --> store
    apply --> store
    repr --> store
    debt --> store
```

The right-hand boxes are the enforcement points. A role proposes; the
coordinator is the only thing that writes, and it refuses anything that breaks
an invariant — a close with a missing memo, a veto against a hard filter, a
re-price of a terminal entry.

## 4. An entry's lifecycle

The state machine is total: no status is terminal by omission, every terminal
status has a route back, and every reason-bearing edge demands a reason.

```mermaid
stateDiagram-v2
    [*] --> queued: filed by the generator
    queued --> in_progress: ar claim
    in_progress --> queued: ar release (why)
    in_progress --> blocked: ar close (why)
    blocked --> queued: ar close --reopen (why)

    in_progress --> confirmed: ar close (memo required)
    in_progress --> refuted: ar close (memo + closure_kind)

    confirmed --> queued: ar close --reopen (why)
    refuted --> queued: ar close --reopen (why)

    note right of refuted
      closure_kind decides reach:
      mechanism → hard-excludes sharers
      slope / cell → penalise only,
      and must name a reopen condition
    end note
```

`in_progress` is spelled `in-progress` in the record; Mermaid will not take the
hyphen in a state id.

## 5. Dispatch and the worker pool

Liveness is observed, not inferred: the coordinator submits the work, so it
knows when a worker finished. TTL reaping in `orient` is the fallback for a
holder that died elsewhere.

```mermaid
sequenceDiagram
    participant C as Coordinator
    participant B as Budget
    participant Cl as Claims
    participant P as Workspace pool
    participant W as worker (thread)
    participant Br as Brain

    loop each entry on the shortlist
        C->>B: spend fanout + spawns
        B-->>C: ok / BudgetExceeded → stop dispatching
        C->>Cl: claim(entry, why="ranked #k")
        Cl-->>C: ok / held by another session → skip
    end

    par up to max_parallel
        C->>P: acquire(slot)
        C->>W: run
        W->>Br: ask(worker, brief, workspace=slot)
        Br-->>W: Reply{verdict, memo, closure_kind, runs, cost}
        W->>P: release(slot) (finally)
    end

    C->>C: _apply_verdict per entry
    Note over C: already closed → success, not a race<br/>close refused → release the claim, do not close
    C->>P: release_all() (finally)
```

A worker that raises is recorded as `failed` *against its entry id* — the item
is part of the answer, because a failure that reads as a result is the exact
shape this harness keeps filing defects about.
