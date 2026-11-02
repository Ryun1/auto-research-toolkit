# Member domains (#64–#68) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish member domains (#69) on top of #63: inheritance, ids across members, `--all-domains` on the read verbs, a shared defect track, and `ar init --member` with docs.

**Architecture:** Everything goes through `members.Workspace` (landed in #63). Inheritance is a merge of raw TOML dicts inside `DomainConfig.load`, done before any table is parsed. Cross-member ids are prefix lookups on the workspace. `--all-domains` runs each verb's per-domain body once per domain. A domain with no umbrella takes the single-domain path, with no change to its output or messages.

**Tech Stack:** Python 3.14, stdlib `tomllib`, pytest + xdist (`uv run --extra dev pytest -n auto`).

**Spec:** `docs/superpowers/specs/2026-11-02-member-domains-design.md` (issues #64–#68; tracking #69)

## Global Constraints

- A domain with no `[domains]` table, and no umbrella above it, behaves exactly as it does today: the same output, the same refusal text, the same exit codes. Every task keeps a test for this.
- **Flag spelling (user decision, 2026-11-02): `--all-domains` on every verb**, not `--all`. `rank --all`, `entry list --all` and `harness export --all` keep their current meanings.
- **Policy merge (user decision):** `human_only` and `never_push_remotes` accumulate (umbrella rules plus member rules, de-duplicated by `pattern`). `forbidden_paths` uses key-level member-wins, because its patterns are relative to each domain's root. `spend_ceiling` takes the smaller value.
- Inherited tables: `policy budgets upstream coordinator skills hardware remote brain session`. Never inherited: `domain state tracks commands knowledge lanes challenge evaluator holdout render plugins domains`.
- `parent` and `derived_from` stay local to one domain.
- Commits go straight to `main`, one or more per issue, styled `Members: <what changed> (#N)` with a `Fixes #N` line on the last commit for each issue. The user pushes. Run `git fetch` and check the issue is still open before starting it.
- Tests: `uv run --extra dev pytest -n auto` (about 50 s). There is no `python` on PATH. Use `uv run`.

## Review Focus

1. **A member that inherits `[skills] dir` or `[policy] forbidden_paths`.** Those values are relative paths and resolve against the *member's* root, not the umbrella's. Expected: `ar domains show` makes this visible (it prints the resolved `skills.dir`), and the behaviour is documented. Test in Task 3.
2. **A tracked file modified inside a member, scanned for tamper.** `git diff` prints repo-relative paths. `ls-files --others` prints cwd-relative ones. Both must be rebased the same way. Tests in Task 2 cover both the tracked and the untracked case.
3. **A path outside the member** (starks' shared `tools/`, reached as `../../tools`). It must classify as scaffolding and never reach `Lanes.classify`, because `classify` calls `lstrip("./")`, which turns `../../tools/x` into `tools/x`. Test in Task 2.
4. **A broken sibling while resolving an id.** A malformed sibling `domain.toml` must not turn `skill check` or `close` into a traceback. Cross-member lookups catch `ConfigError` from `Workspace.for_domain` and fall back to local-only (`validate` already reports the workspace problem). Test in Task 6.
5. **`--all-domains` when one member's board or budget raises** (for example, the target probe fails). The other domains still print. The failing section prints `ar: <error>` and the exit status is non-zero. Test in Task 9.

Out of scope, noted for #69: a worker slot created from a member is a worktree of the *whole* repo, so a worker's cwd is the repo root, not the member root. That is a pre-existing gap, unrelated to these issues.

---

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `src/autoresearch/inherit.py` (new) | the pure raw-dict merge and per-key provenance; no I/O | 3 |
| `src/autoresearch/members.py` | umbrella lookup for load; `owner_of`, `entry`, `resolve`; `relinks`; `effective_rows` | 3, 5, 6, 7 |
| `src/autoresearch/config.py` | call the merge in `load`; new fields `sources`, `umbrella_root` | 3 |
| `src/autoresearch/lanes.py`, `tamper.py` | path rebasing | 2 |
| `src/autoresearch/cli.py` | `domains show/relink`, close, entry show/new, `--all-domains`, harness export, init | 4–11 |
| `src/autoresearch/tree.py`, `skills.py`, `links.py`, `validate.py` | cross-member ids | 6 |
| `src/autoresearch/defects.py`, `driver/loop.py` | shared defect track | 8 |
| `src/autoresearch/scaffold.py` | `init_member` | 11 |
| `docs/ARCHITECTURE.md`, `docs/MIGRATION.md`, `CHANGELOG.md` | docs | 12 |
| `tests/conftest.py` | shared member fixtures | 1 |
| `tests/test_members_{lanes,inherit,ids,all,defects,init}.py` (new) | one file per issue | 2–11 |

Order: Task 1 → #64 (2–4) → #65 (5–7) → #67 (8) → #66 (9–10, since `reap ID` uses #65's resolver) → #68 (11–12).

---

### Task 1: Shared member fixtures

**Files:**
- Modify: `tests/conftest.py`, `tests/test_members.py:19-45`

**Interfaces:**
- Produces, in `conftest.py`:
  - `member_domain(dest, prefixes=("Q","H"), name=None, extra="", drop_tables=(), drop_tracks=()) -> Path`. This is `_domain` from `test_members.py`, moved. `drop_tables` removes whole top-level tables (`[policy]` together with its `[[policy.*]]` arrays, `[hardware.*]`, `[[remote]]`) from the copied TOML. `drop_tracks` removes `[[tracks]]` blocks by prefix *after* renaming.
  - `MEMBERS = '[domains]\nmembers = ["domains/*"]\n'`
  - fixture `umbrella(tmp_path)`: the same shape as today (umb Q/H, `domains/a` A/B, `domains/b` C/D).
  - fixture `git_umbrella(umbrella)`: runs `git init`, `git add -A` and `git commit` with a fixed author, and returns the root.

- [ ] **Step 1:** Move `_domain`, `MEMBERS` and `umbrella` into `conftest.py` under the names above. Point `test_members.py` at them.
- [ ] **Step 2:** Add `drop_tables`/`drop_tracks`. Implement them as a line-based TOML section stripper: a section runs from its header to the next header, and only the comment block directly above a dropped header goes with it. Add `test_member_domain_drops_tables`: after dropping `budgets` and `policy`, the domain still loads, `config.budgets == {}` and `config.policy.human_only == []`.
- [ ] **Step 3:** `uv run --extra dev pytest -n auto tests/test_members.py tests/conftest.py`. Expected: all pass.
- [ ] **Step 4:** Commit: `Members: test fixtures for umbrella workspaces move to conftest (#64)`

---

### Task 2: Lanes see domain-relative paths (#64, lanes half)

**Files:**
- Modify: `src/autoresearch/lanes.py`, `src/autoresearch/tamper.py:74-178`
- Test: `tests/test_members_lanes.py`

**Interfaces:**
- Produces: `lanes.rebase(repo_path: str, prefix: str) -> str | None`. Here `prefix` is `git rev-parse --show-prefix` of the domain root (`"domains/a/"`, or `""` at the repo top). The function returns the path relative to the domain, or None when the path lies outside the domain.
- Produces: `tamper.scan(config, diff, prefix: str = "")`. A `Hunk.path` is domain-relative for paths inside the domain, and `posixpath.relpath`-style (`../../tools/x.py`) for paths outside it.

- [ ] **Step 1: Write the failing tests** (in `git_umbrella`, member `domains/a`):
  - `test_a_tracked_member_findings_file_is_findings`: commit `domains/a/state/entries/A9.yaml`, then append a line `t = time.perf_counter()`. `tamper.scan_tree(DomainConfig.load(root/"domains/a"), root/"domains/a")` returns no hunk for that file. Today it returns a `timing` hunk, because the path arrives as `domains/a/state/entries/A9.yaml` and classifies as scaffolding.
  - `test_an_untracked_member_findings_file_is_findings`: the same file left untracked. No hunk. This passes today by accident and must keep passing.
  - `test_a_member_scaffolding_file_is_reported_member_relative`: modify a tracked `domains/a/bin/x.py` with the same line. A `timing` hunk is reported with `path == "bin/x.py"`.
  - `test_a_path_outside_the_member_is_scaffolding`: modify a tracked `tools/y.py` at the repo top. A hunk is reported with `path == "../../tools/y.py"`, even though the member's lanes would match `tools/`. To prove that, add `|tools` to that member's `findings` regex in the test.
  - `test_the_evaluator_matches_a_member_relative_path`: a tracked change to the member's `bin/measure` is reported with kind `evaluator`.
- [ ] **Step 2:** Run `uv run --extra dev pytest tests/test_members_lanes.py -v`. Expected: the tracked, scaffolding, outside and evaluator tests FAIL. The untracked test passes.
- [ ] **Step 3: Implement.**
  - `diff_of` runs every git call with `cwd` set to the repo toplevel (`rev-parse --show-toplevel` of `tree`), so tracked and untracked paths are both repo-relative.
  - `scan_tree` reads `prefix` from `_git(config.paths.root, "rev-parse", "--show-prefix")`. It reads it from the domain root, not from `tree`: a slot is a worktree of the same repo, so the domain sits at the same prefix inside it.
  - `scan` rebases each path. If `rebase` returns None, the lane is `SCAFFOLDING` and `classify` is never called. Evaluator membership is checked on the rebased path.
- [ ] **Step 4:** Run the test file, then the full suite. Expected: PASS, with no regressions in `test_validate_git.py`, `test_external*` or `test_loop.py`.
- [ ] **Step 5:** Commit: `Members: tamper classifies a member's git paths against the member's lanes (#64)`

---

### Task 3: Members inherit the umbrella's shared tables (#64)

**Files:**
- Create: `src/autoresearch/inherit.py`
- Modify: `src/autoresearch/config.py` (`load`, two new fields), `src/autoresearch/members.py`
- Test: `tests/test_members_inherit.py`

**Interfaces:**
- Produces: `inherit.INHERITED: tuple[str, ...]`, holding the nine table names in the Global Constraints.
- Produces: `inherit.merge(umbrella: dict, member: dict) -> tuple[dict, dict[str, str]]`. It returns the member's raw TOML with inherited tables merged in, and `sources` keyed `"<table>.<key>"` with the value `"umbrella"` or `"member"`. Accumulated rules are keyed `"policy.human_only[<pattern>]"` and `"policy.never_push_remotes[<pattern>]"`. The function is pure, with no I/O and no `ConfigError` except for an unparseable `min_core`.
- Produces: `members.umbrella_raw(root: Path) -> tuple[Path, dict] | None`. It walks up from `root` the same way `Workspace.for_domain` does (`_declared_globs` plus `_expand(strict=False)`) and returns the umbrella's raw TOML. It never loads a `DomainConfig`, so there is no recursion.
- Produces, on `DomainConfig`: `sources: dict[str, str]` (empty outside a workspace) and `umbrella_root: pathlib.Path | None`.

Merge rules (`inherit.merge`):
- A table only the umbrella has is copied whole. A table only the member has is the member's.
- A table both have merges key by key, and the member's key wins. `hardware`, `brain` and `coordinator` merge by key too, so `hardware.<class>` is one key.
- `remote` is an array of tables: the member's list replaces the umbrella's whole list when the member sets it.
- `policy.human_only` and `policy.never_push_remotes`: the umbrella's rules followed by the member's, dropping any member rule whose `pattern` the umbrella already has.
- `policy.spend_ceiling`: `min()` of the values that are set.
- `upstream.min_core`: the higher of the two by `upstream.version_tuple`. If either side doesn't parse, raise `ConfigError` naming both values.

- [ ] **Step 1: Write the failing tests** (members built with `drop_tables` on the shared fixture):
  - `test_a_member_that_sets_one_budget_keeps_the_rest`: the member's `[budgets]` is only `iteration_fanout = 9`. The loaded `budgets` equals the umbrella's budgets with `iteration_fanout` set to 9.0. `sources["budgets.iteration_fanout"] == "member"` and `sources["budgets.domain_max_runs"] == "umbrella"`.
  - `test_a_member_cannot_drop_an_umbrella_human_only_rule`: the member's `[policy]` has its own human_only rule `"member publish"`. `[r.pattern for r in policy.human_only] == ["toy publish", "member publish"]`. Running `ar --domain <member> policy` exits 0 and its output contains `toy publish`.
  - `test_never_push_remotes_accumulate`: the umbrella has `origin` and the member has `upstream`. Both are present.
  - `test_spend_ceiling_takes_the_smaller`: the umbrella has 5.0. A member with 2.0 gets 2.0, and a member with 9.0 gets 5.0.
  - `test_min_core_takes_the_higher_floor`: the umbrella has `0.9.0` and the member `0.8.0`, so the effective floor is `0.9.0`. Swapped, it is also `0.9.0`.
  - `test_identity_tables_are_never_inherited`: a member with no `[commands]` fails to load with its own error, not with the umbrella's commands.
  - `test_inherited_skills_dir_resolves_in_the_member` (Review Focus 1): the member drops `[skills]` and gets `skills.dir == "docs/skills"`. `ar domains show` (Task 4) prints it resolved under the member root.
  - `test_a_plain_domain_has_no_sources`: the sandbox's `config.sources == {}` and `umbrella_root is None`.
  - `test_a_malformed_umbrella_does_not_break_a_member_load`: when the umbrella's TOML doesn't parse, `umbrella_raw` returns None (the loading is not inherited). The member's `validate` still reports the umbrella problem through #63's `config.check`.
- [ ] **Step 2:** Run `uv run --extra dev pytest tests/test_members_inherit.py -v`. Expected: FAIL (`inherit` does not exist).
- [ ] **Step 3: Implement** `inherit.merge`. In `DomainConfig.load`, after the unknown-key check, call `found = members.umbrella_raw(root)` (lazy import). When it returns something, do `data, sources = inherit.merge(found[1], data)`. Every parse below then sees the effective dict. Set `sources` and `umbrella_root` on the instance.
- [ ] **Step 4:** Run the test file and the full suite. Expected: PASS. `test_members.py` is unaffected, because its members declare every table themselves.
- [ ] **Step 5:** Commit: `Members: a member inherits the umbrella's shared tables, safety rules accumulate (#64)`

---

### Task 4: `ar domains show <member>` (#64)

**Files:**
- Modify: `src/autoresearch/members.py`, `src/autoresearch/cli.py:195-213` and the `domains` parser
- Test: `tests/test_members_inherit.py`

**Interfaces:**
- Consumes: `DomainConfig.sources`, `inherit.INHERITED`.
- Produces: `members.effective_rows(config) -> list[dict]` with the keys `table, key, value, source`, where `source` is one of `umbrella | member | default`. Rows cover every key of every inherited table in the effective config, plus these typed defaults the loader supplies when neither file sets them, reported `default` with the loaded value: `coordinator.{risk,tree_max_depth,tree_max_children,branch_stagnation,confirm_runs,distil_every,dispatch}`, `upstream.{url,ref,min_core}`, `session.{parent,settle_hours}`, `skills.{dir,max_lines}`, `policy.spend_ceiling`. `skills.dir` is shown resolved against the domain root.
- CLI: `ar domains` with no subcommand still lists the domains. `ar domains show [PATH] [--json]` defaults to the current domain, and `PATH` is a workspace path such as `domains/t1` or `.`. The prose has one line per row: `[budgets] iteration_fanout = 9.0    member`. JSON: `{"path": ..., "rows": [...]}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_domains_show_reports_a_source_for_every_key`: in the JSON for `domains/a` (which drops `[budgets]`), every row has a source in that set of three. `domain_max_runs` is `umbrella`, and `coordinator.branch_stagnation` (set by neither file) is `default`.
  - `test_domains_show_unknown_path_is_refused`: `ar domains show domains/zz` exits 2 and names the known paths.
  - `test_bare_domains_still_lists`: the output of `ar domains` is unchanged (reuse an assertion from `test_members.py`).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement.** Convert `domains` to subparsers, with `set_defaults(func=cmd_domains)` on the parent so a bare `ar domains` still lists.
- [ ] **Step 4:** Run the test file and the full suite. Expected: PASS.
- [ ] **Step 5: Verify against starks** (manual, recorded in the commit body). Run `git fetch` first in `~/auto-research/starks-research`, because local checkouts can be stale. Copy it to the scratchpad. Save `ar --domain domains/t1 domains show --json` to `before.json`. Delete the inherited tables from the copy's `domains/t1/domain.toml`, add `[domains] members = ["domains/*"]` to the copy's umbrella, and save `after.json`. Diff the `(table, key, value)` triples. The expected differences are only the deliberate overrides that t1 keeps. List them in the commit body.
- [ ] **Step 6:** Commit: `Members: \`ar domains show\` prints the effective config and where each key came from (#64)` with `Fixes #64`.

---

### Task 5: Resolve an id across members (#65)

**Files:**
- Modify: `src/autoresearch/members.py`
- Test: `tests/test_members_ids.py`

**Interfaces:**
- Produces: `Workspace.owner_of(entry_id: str) -> tuple[str, DomainConfig] | None`. This is a prefix match through `track.is_id` over `domains()`. It does no I/O.
- Produces: `Workspace.entry(entry_id: str) -> tuple[DomainConfig, Entry] | None`. It returns None when no domain owns the id or the owner has no such file.
- Produces: `members.resolve(config, entry_id) -> tuple[DomainConfig, Entry]`. It raises `ConfigError("no domain in this workspace owns entry id 'X9'; prefixes are [...]")` when there is no owner, and the owning store's `SchemaError` when the file is missing.
- Produces: `members.workspace_or_none(config) -> Workspace | None`. This is `Workspace.for_domain(config)` with `ConfigError` swallowed (Review Focus 4). Every caller in Tasks 6–10 uses it.

- [ ] **Step 1: Write the failing tests:**
  - `test_resolve_finds_an_entry_in_a_sibling` (an entry C1 in `domains/b`, resolved from `domains/a`)
  - `test_resolve_from_the_umbrella`
  - `test_resolve_unowned_id_is_an_error` (X1)
  - `test_owner_of_does_not_read_stores` (a member whose entries dir is missing still answers)
  - `test_workspace_or_none_on_a_broken_umbrella`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `Members: an entry id resolves to its owning domain across the workspace (#65)`

---

### Task 6: Use the resolver in close, entry show, tree, skills, links and validate (#65)

**Files:**
- Modify: `src/autoresearch/cli.py` (`cmd_close` at 1250-1265 and 1373-1381; `cmd_entry_show` at 1063), `tree.py`, `skills.py:236-250,361-450`, `links.py:37-69`, `validate.py`, `ui/graph.py:25,104`
- Test: `tests/test_members_ids.py`

**Interfaces:**
- Consumes: Task 5.
- `tree.check_derived(store, derived_from, parent, track_id, config=None)` and `tree.check_branch(...)`: when the target is not in the local store and `workspace_or_none(config).owner_of(target)` names another domain, raise
  `"cannot file a branch of C1: it belongs to member domains/b; lineage (--parent, --derived-from) stays inside one domain -- cite it with --source C1 instead"`.
  Callers: `cli.py:743,745,823` and `driver/loop.py:784,785` pass `config`.
- `links.links(entries, foreign: Collection[str] = ())`: a `sources` item in `foreign` yields `Link(item, e.id, "cites")`. `ui/graph.py` passes the ids among the sources that `owner_of` assigns to another domain, so `source_paths` stops listing them as paths.
- `skills._cite_re(config)` builds its regex from every workspace prefix, so inline `[C1]` is recognised. `skills.check` falls back to `Workspace.entry(cid)` on a miss, and runs `_terminal` and the staleness check against the owning config.
- `validate.cross_member_problems(config, entries) -> list[str]`: for each `sources` and `supersedes` item that `owner_of` assigns to *another* domain where `Workspace.entry` is None, report `"Q58 sources C9: member domains/b has no such entry"`. `cmd_validate` adds these to `problems`.
- `cmd_close`: when the local `store.load(superseded_by)` fails and the workspace resolves the id elsewhere, the replacement is saved to the owner's store and the owner's views are re-rendered (`_render_views(owner_config)`). The confirmation line becomes `— superseded by C1 (domains/b)`. With no workspace members, the refusal text is byte-identical to today's.
- `cmd_entry_show`: on a local miss, it resolves across the workspace. stdout is the same JSON, and stderr carries `ar: C1 is in member domains/b (b)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_close_superseded_by_a_member_entry`: from the umbrella, `close Q1 superseded --superseded-by C1` exits 0. C1 on disk has `supersedes == ["Q1"]`. `ar validate` is clean in the umbrella and in `domains/b`.
  - `test_close_superseded_by_unowned_id_is_refused_outside_a_workspace_unchanged`: in the sandbox, the stderr matches today's text exactly. Take the string from `test_cli_close.py`.
  - `test_cross_member_parent_is_refused`: `entry new --parent C1` from the umbrella exits 2 with the message above. Run the same check with `--derived-from`.
  - `test_skill_citing_umbrella_entry_terminal_passes` / `..._moved_after_distillation_is_flagged`: a skill in `domains/a` cites Q1. It is clean when Q1 is terminal, and flagged with the existing "has moved since" text after Q1 is updated past `distilled.at`.
  - `test_skill_inline_cross_member_cite_is_recognised`: a body with `[Q1]` and `cites: [Q1]` produces no "never cites it in its body" warning.
  - `test_links_emit_cross_member_cites`
  - `test_validate_flags_a_dangling_cross_member_source` / `..._clean_when_it_resolves`
  - `test_entry_show_from_another_member`: the stdout JSON id is C1, and stderr names `domains/b`.
  - `test_resolver_survives_a_broken_sibling` (Review Focus 4): corrupt the TOML of `domains/b`. `skill check` in `domains/a` reports the workspace problem and does not raise a traceback.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement in the order tree → close → entry show → skills → links/graph → validate, running the test file after each one.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `Members: close, entry show, skills and validate resolve ids across members; lineage stays local (#65)`

---

### Task 7: `ar domains relink` rewrites superseded-by URLs into ids (#65 migration)

**Files:**
- Modify: `src/autoresearch/members.py`, `src/autoresearch/cli.py` (the `domains` subparser)
- Test: `tests/test_members_ids.py`

**Interfaces:**
- Produces: `members.relinks(workspace) -> list[Relink]`, where `Relink` is a dataclass with `old: DomainConfig, old_id: str, url: str, new: DomainConfig, new_id: str`. A candidate is an entry closed `superseded` whose latest `superseded` history event reads `retired as superseded by external record <url>` (the text at `cli.py:1372`), where `<url>`'s path ends with `<domain path>/<that domain's entries dir relative to its root>/<ID>.yaml` for some workspace domain that has the entry `<ID>`.
- CLI: `ar domains relink` prints one line per candidate and changes nothing. `--write` appends `Event("superseded", session, "retired as superseded by C1 (relinked from <url>)")` to the old entry, adds the old id to the new entry's `supersedes` with its own event, saves both, and re-renders both. History is append-only, so the URL event stays.

- [ ] **Step 1: Write the failing tests:**
  - `test_relink_finds_a_github_url_to_a_member_entry`: close Q1 with `--superseded-by https://github.com/o/r/blob/main/domains/b/state/entries/C1.yaml`. The dry run lists it and changes nothing on disk.
  - `test_relink_write`: afterwards C1 has `supersedes == ["Q1"]`, validate is clean in both domains, and a second run finds nothing.
  - `test_relink_ignores_urls_to_missing_entries`
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), then run the full suite.
- [ ] **Step 5:** Commit: `Members: \`ar domains relink\` turns superseded-by URLs into ids (#65)` with `Fixes #65`.

---

### Task 8: Members share the umbrella's defect track (#67)

**Files:**
- Modify: `src/autoresearch/defects.py:46-130`, `src/autoresearch/cli.py` (`cmd_entry_new` at 719-782, `cmd_harness_export` at 954), `src/autoresearch/driver/loop.py:765-812,1663-1680`
- Test: `tests/test_members_defects.py`

**Interfaces:**
- Produces: `defects.DefectHome`, a dataclass with `track: Track, config: DomainConfig, store: Store, shared: bool`. `shared` is True when the home belongs to the umbrella and not to this domain.
- Produces: `defects.defect_home(config) -> DefectHome | None`. It returns the local defect track first. Otherwise, when `workspace_or_none(config)` puts `config` in a workspace as a member, it returns the umbrella's defect track. A member that declares its own track keeps it. This replaces `defect_track`, whose only callers are in `defects.py`.
- `defects.export_bundle(config, entries=None, all_status=False)`: `entries` defaults to `home.store.all()`, and `project.name` is `home.config.name`, so the bundle is the same from any member. `cmd_harness_export` stops passing the local entries.
- `cmd_entry_new`: when `--track` is not declared locally, or `--id`'s prefix has no local owner, and `defect_home(config)` is shared and matches (by `track.id` or by `track.is_id(args.id)`), the entry is filed into `home.store` under that store's filing lock and with the same evidence check. The command prints the id, then `filed in umbrella <path> (shared defect track 'harness')`, and re-renders the umbrella's views. The existing refusal text stays the same whenever there is no shared home.
- Loop: `_file(self, proposal, home: DefectHome | None = None)` files into `home.store`, with tree checks against `home.config`, when it is given. The QC phase replaces its `t.id == "harness"` lookup with `defect_home(self.config)` and passes the result through. The `core` stamp keys off `track.requires_defect_evidence` as it does now, so it applies.

- [ ] **Step 1: Write the failing tests.** Use a fixture whose members are built with `drop_tracks=("B",)`, so they have no defect track.
  - `test_entry_new_harness_from_a_member_files_in_the_umbrella`: `entry new --track harness --core 0.1 --repro r --observed o ...` from `domains/a` creates `state/entries/H1.yaml` under the umbrella root and nothing under the member. Validate is clean in both.
  - `test_entry_new_by_id_prefix_from_a_member` (`--id H7`)
  - `test_entry_new_shared_defect_still_requires_evidence`: with no `--repro`, it exits 2 with the existing `evidence_problems` text.
  - `test_harness_export_same_from_member_and_umbrella`: the bundles are equal after popping `exported_at`.
  - `test_a_member_with_its_own_defect_track_keeps_it` (the plain `umbrella` fixture, B track)
  - `test_loop_qc_harness_debt_from_a_member_lands_in_the_umbrella`: drive `_qc` with the toy brain from `tests/toy_brain.py`, following the pattern of the existing QC harness-debt test in `test_loop.py`.
  - `test_outside_a_workspace_undeclared_track_message_unchanged`
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), then run the full suite.
- [ ] **Step 5:** Commit: `Members: a member files and exports harness defects on the umbrella's track (#67)` with `Fixes #67`.

---

### Task 9: `--all-domains` runner, and board/rank/budget/validate on it (#66)

**Files:**
- Modify: `src/autoresearch/cli.py` (`cmd_board` 454, `cmd_rank` 1602, `cmd_budget` 1657, `cmd_validate` 1923, and their parsers), `src/autoresearch/render.py`
- Test: `tests/test_members_all.py`

**Interfaces:**
- Produces: `cli.Report`, a dataclass with `status: int, text: str, data: object | None`. `data` is what `--json` prints for one domain today.
- Produces: `cli._board_report(config, args) -> Report`, and the same shape for `_rank_report`, `_budget_report` and `_validate_report`. Each one is the existing body of `cmd_<verb>` with `print` swapped for building the text. Lines that go to stderr today still go to stderr.
- Produces: `cli._run_report(args, fn: Callable[[DomainConfig, Namespace], Report]) -> int`. Without the flag it prints `data` as JSON or `text` and returns `status`, the same bytes as today. With `--all-domains`:
  - when there are no members, it writes `ar: --all-domains: <root> has no member domains; showing this domain only` to stderr once, then runs the single-domain path;
  - otherwise it walks `Workspace.for_domain(config).domains()`. In prose, each section is `== <path> (<name>) ==\n<render.goal_line(cfg.goal)>\n<text>`, and sections are separated by a blank line. In JSON it prints `{"domains": {path: data}}`;
  - a domain whose `fn` raises `AutoresearchError` gets `ar: <error>` as its section body (and `{"error": str}` in JSON) with status 2 (Review Focus 5);
  - the exit code is `max(status)`. For validate, when anything failed, it appends `failing: <path>, <path>` at the end.
- Produces: `render.goal_line(goal) -> str`, factored out of `board_document`'s goal header so the board and the section headers print the same line.
- Each of the four parsers gets `--all-domains` (`dest="all_domains"`).

- [ ] **Step 1: Write the failing tests** (two-member fixture):
  - `test_<verb>_all_domains_contains_each_single_domain_output` for board, rank, budget and validate. For each `(path, cfg)`, the single-domain stdout of `ar --domain <cfg root> <verb>` appears verbatim in the `--all-domains` stdout, umbrella first.
  - `test_<verb>_all_domains_json_has_one_key_per_domain`, giving keys `[".", "domains/a", "domains/b"]` for board, rank, budget and validate.
  - `test_validate_all_domains_fails_when_one_member_fails`: corrupt one entry in `domains/b`. The exit is 1 and the last line is `failing: domains/b`. With every domain clean, the exit is 0.
  - `test_all_domains_outside_a_workspace_is_the_single_output`: the stdout equals the single run, and the stderr note appears once.
  - `test_rank_all_still_means_full`: `rank --all` output is unchanged.
  - `test_a_failing_member_section_does_not_hide_the_others` (Review Focus 5): break `domains/a`'s `probe_target` and run `budget --all-domains`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `Report` and `_run_report`, then convert the verbs one at a time. After each conversion, run that verb's existing tests (`test_cli_rank.py`, `test_cli_budget.py`, `test_board_snapshot.py`, the validate tests) to confirm the single-domain bytes are unchanged.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `Members: --all-domains on board, rank, budget and validate (#66)`

---

### Task 10: `reap --all-domains`, reap by id across members, starks timing (#66)

**Files:**
- Modify: `src/autoresearch/cli.py:1170-1188` and the reap parser
- Test: `tests/test_members_all.py`

**Interfaces:**
- Consumes: `Report`, `_run_report` (Task 9) and `Workspace.owner_of` (Task 5).
- `_reap_report(config, args) -> Report` covers list mode only and has `data=None`. `reap` has no `--json`, so none is added.
- `reap ID`: when the id has no local owner and `owner_of` names another domain, run `Claims(Store(owner.paths.entries), owner, args.session).reap(...)` and re-render that domain's views. The output line gets ` (domains/b)` appended. `reap ID --all-domains` is refused: `--all-domains lists reapable claims; reap one id without it`.

- [ ] **Step 1: Write the failing tests:** `test_reap_all_domains_lists_each_domain`, `test_reap_id_in_another_member`, and `test_reap_id_with_all_domains_is_refused`.
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), then run the full suite.
- [ ] **Step 5: Measure on starks.** In a scratchpad copy of the fetched starks checkout, with the copy's umbrella given `[domains] members = ["domains/*"]`, run `time bin/ar board --all-domains > /dev/null` and `time bin/ar rank --all-domains > /dev/null`. Record the wall times beside the sum of the five single-domain runs. They go in the commit body. If `--all-domains` is more than about 1.2× that sum, profile before committing.
- [ ] **Step 6:** Commit: `Members: reap lists across members and frees an id in any member (#66)` with `Fixes #66` and the timings.

---

### Task 11: `ar init --member` (#68)

**Files:**
- Modify: `src/autoresearch/scaffold.py`, `src/autoresearch/cli.py` (`cmd_init` 117 and the `init` parser at 2338)
- Test: `tests/test_members_init.py`

**Interfaces:**
- Consumes: `Workspace`, `members._expand`.
- Produces: `scaffold.init_member(umbrella: DomainConfig, rel: str, prefix: str, name: str | None = None) -> list[Path]`. It writes these files under `umbrella.paths.root / rel`:
  - `domain.toml` with `[domain]` (name defaults to the directory name), `[state]` with default paths, one `[[tracks]]` (`id = "research"`, `prefix`, `title = "Hypothesis Queue"`), `[lanes] findings` copied from the umbrella because load requires it and lanes are not inherited, and `[commands] measure = "bin/measure"`;
  - a `goal.yaml` stub and `bin/measure`, from the same templates `init` uses;
  - `AGENTS.md`, which says "Shared rules, tools and the defect track live in the umbrella: read `<relpath>/AGENTS.md` first.";
  - `state/entries/`.
- If no existing glob covers `rel`, it appends `rel` to `[domains].members` in the umbrella's `domain.toml`. When a `members = [...]` line exists, it edits that line in place and keeps comments. Otherwise it appends a `[domains]` table.
- It refuses: a prefix that does not match `^[A-Z]+$`; a prefix any workspace domain already declares (naming the owner); a `rel` that is absolute or leaves the umbrella; and an existing `domain.toml` at the target.
- CLI: `ar init --member domains/<name> --prefix C [--name N]`. `path` becomes `nargs="?"`. Exactly one of `path` and `--member` is required, and `--prefix` is required with `--member`. After writing, it runs `cmd_validate` on the member and prints the next steps.

- [ ] **Step 1: Write the failing tests:**
  - `test_init_member_passes_validate_and_validate_all_domains`: run from the umbrella fixture. `ar validate` in the new member exits 0, and `ar validate --all-domains` from the umbrella exits 0.
  - `test_init_member_appends_to_members_unless_covered`: with `members = ["domains/*"]`, `domains/c` leaves the file byte-identical, while `labs/x` appends `"labs/x"`.
  - `test_init_member_refuses_a_used_prefix` (`--prefix A`, naming `domains/a`)
  - `test_init_member_inherits`: the new member's `ar domains show` reports budgets from the umbrella.
- [ ] **Step 2–4:** Run (FAIL), implement, run (PASS), then run the full suite.
- [ ] **Step 5:** Commit: `Members: \`ar init --member\` scaffolds a member under an umbrella (#68)`

---

### Task 12: Docs and the starks recipe (#68)

**Files:**
- Modify: `docs/ARCHITECTURE.md`, `docs/MIGRATION.md`, `CHANGELOG.md`

- [ ] **Step 1:** ARCHITECTURE.md gets a "Member domains" section covering:
  - when to split a goal into a member (when its scores don't compare with the umbrella's);
  - the inherited and identity table lists, and the four merge rules, including the `never_push_remotes` decision;
  - that inherited relative paths resolve per member;
  - ids across members, and lineage staying local;
  - `--all-domains`, and why it is not `--all`;
  - the shared defect track.
- [ ] **Step 2:** MIGRATION.md gets the starks recipe:
  1. add `[domains] members = ["domains/*"]`;
  2. for each member, delete the inherited tables and confirm with `ar domains show domains/<x>` against a saved copy of the earlier output (the Task 4 procedure);
  3. run `ar domains relink`, check Q58→C1 and Q93→C2 in the dry run, then run it again with `--write`;
  4. delete the "harness defects go on the PRIMARY domain" comments;
  5. run `ar validate --all-domains`.
- [ ] **Step 3:** CHANGELOG entry for #64–#68.
- [ ] **Step 4:** Commit: `Members: architecture and migration docs for member domains (#68)` with `Fixes #68`.
- [ ] **Step 5 (in starks-research, not this repo, after the user installs the release there):** apply the recipe, then confirm these acceptance checks:
  - the member `domain.toml` files hold identity tables only;
  - `ar validate --all-domains` is clean;
  - `bin/ar close` cites C1 by id;
  - `bin/ar --domain domains/bounds harness export` exports H1–H12.

  Commit only the migration's own files there. Leave #69 for the user to close.
