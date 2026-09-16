"""Every guard on a skill ships a negative test proving it refuses something.

Invariant 3: a guard whose enforcement can be deleted without a test failing is
a comment. A skill is prose handed to every role as knowledge, so each way it
can be wrong has to be a failing check here.
"""
import pytest

from autoresearch import skills as skills_mod
from autoresearch.errors import SchemaError
from conftest import close, make_entry, make_skill


def _entries(sandbox, store, *ids, **over):
    out = []
    for entry_id in ids:
        entry = make_entry(store, entry_id)
        out.append(close(sandbox, store, entry, **over))
    return out


def _check(sandbox, store):
    found, problems = skills_mod.read_all(sandbox)
    return problems + skills_mod.check(sandbox, found, store.all())


# -- the happy path -------------------------------------------------------

def test_a_cited_skill_over_terminal_entries_is_clean(sandbox, store):
    _entries(sandbox, store, "Q1", "Q2")
    make_skill(sandbox, "knob-interaction", cites=["Q1", "Q2"])
    assert _check(sandbox, store) == []


def test_read_all_reports_zero_rather_than_failing_when_nothing_is_distilled(sandbox):
    """A domain that has not distilled anything is not misconfigured, and a
    reader that cannot say 'zero' is indistinguishable from one that read
    nothing."""
    found, problems = skills_mod.read_all(sandbox)
    assert (found, problems) == ([], [])


def test_unreadable_skill_does_not_hide_valid_sibling(sandbox):
    broken = sandbox.paths.root / sandbox.skills.dir / "broken" / "SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"\x80\x81\x82")
    make_skill(sandbox, "valid-sibling", cites=["Q1"])
    found, problems = skills_mod.read_all(sandbox)
    assert [skill.name for skill in found] == ["valid-sibling"]
    assert len(problems) == 1 and str(broken) in problems[0]


def test_render_round_trips(sandbox, store):
    _entries(sandbox, store, "Q1")
    path = make_skill(sandbox, "round-trip", cites=["Q1"])
    skill = skills_mod.parse(path.read_text(), path)
    assert skill.name == "round-trip" and skill.cites == ["Q1"]
    assert skills_mod.render(skill) == path.read_text()


# -- citation integrity ---------------------------------------------------

def test_a_skill_citing_nothing_is_refused(sandbox, store):
    make_skill(sandbox, "opinion", cites=[], body="# A claim\n\nWith nothing behind it.\n")
    assert any("cites nothing" in p for p in _check(sandbox, store))


def test_a_skill_citing_an_entry_that_does_not_exist_is_refused(sandbox, store):
    make_skill(sandbox, "ghost", cites=["Q99"])
    assert any("not in the store" in p for p in _check(sandbox, store))


def test_a_skill_citing_an_open_entry_is_refused(sandbox, store):
    """Invariant 6: unsettled work is not evidence."""
    make_entry(store, "Q1")                       # queued, never closed
    make_skill(sandbox, "premature", cites=["Q1"])
    assert any("not terminal" in p for p in _check(sandbox, store))


def test_an_inline_citation_missing_from_cites_is_refused(sandbox, store):
    _entries(sandbox, store, "Q1", "Q2")
    make_skill(sandbox, "half-cited", cites=["Q1"],
               body="# A claim\n\nBacked by [Q1] and also by [Q2].\n")
    assert any("in its body but not in" in p for p in _check(sandbox, store))


def test_a_cite_never_used_in_the_body_is_refused(sandbox, store):
    _entries(sandbox, store, "Q1", "Q2")
    make_skill(sandbox, "over-cited", cites=["Q1", "Q2"],
               body="# A claim\n\nBacked by [Q1].\n")
    assert any("never cites it in the body" in p for p in _check(sandbox, store))


def test_a_duplicate_cite_is_refused(sandbox, store):
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "twice", cites=["Q1", "Q1"],
               body="# A claim\n\nBacked by [Q1].\n")
    assert any("twice" in p for p in _check(sandbox, store))


# -- staleness: the load-bearing one --------------------------------------

def test_reopening_a_cited_entry_makes_the_skill_stale(sandbox, store):
    """The whole point. A skill outliving its evidence is confidently wrong,
    which is worse than absent -- so it must fail validation, not degrade."""
    entry, = _entries(sandbox, store, "Q1")
    make_skill(sandbox, "was-true", cites=["Q1"])
    assert _check(sandbox, store) == []

    machine = sandbox.track_for("Q1").machine
    entry.apply(machine, machine.initial, "test", why="new evidence arrived")
    store.save(entry)

    problems = _check(sandbox, store)
    assert any("result-archived" in p for p in problems), problems


def test_relabelling_a_closure_makes_the_skill_stale(sandbox, store):
    """A `mechanism` demoted to `cell` no longer supports 'holds everywhere',
    and the skill quoting it has to be re-read before it is trusted."""
    entry, = _entries(sandbox, store, "Q1", verdict="refuted",
                      closure_kind="mechanism")
    make_skill(sandbox, "holds-everywhere", cites=["Q1"])
    assert _check(sandbox, store) == []

    entry.relabel("cell", "test", why="it was only ever measured at one width")
    entry.result.reopen_condition = "another width"
    store.save(entry)

    assert any("relabel" in p for p in _check(sandbox, store))


def test_stale_report_names_the_skill_and_the_reason(sandbox, store):
    entry, = _entries(sandbox, store, "Q1")
    make_skill(sandbox, "was-true", cites=["Q1"])
    machine = sandbox.track_for("Q1").machine
    entry.apply(machine, machine.initial, "test", why="reopened")
    store.save(entry)

    found, _ = skills_mod.read_all(sandbox)
    report = skills_mod.stale_report(sandbox, found, store.all())
    assert "was-true" in report and "Q1" in report["was-true"][0]


# -- format ---------------------------------------------------------------

def test_a_description_that_does_not_state_when_to_read_it_is_refused(sandbox, store):
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "summarised", cites=["Q1"],
               description="Explains the width gate and how to measure it.")
    assert any("must start 'use when'" in p.lower() for p in _check(sandbox, store))


def test_an_empty_description_is_refused(sandbox, store):
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "nameless", cites=["Q1"], description=" ")
    assert any("description is required" in p for p in _check(sandbox, store))


def test_an_over_long_description_is_refused(sandbox, store):
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "verbose", cites=["Q1"],
               description="Use when " + "x" * 1100)
    assert any("over the 1024 ceiling" in p for p in _check(sandbox, store))


def test_a_body_over_max_lines_is_refused(sandbox, store):
    _entries(sandbox, store, "Q1")
    sandbox.skills.max_lines = 5
    make_skill(sandbox, "sprawling", cites=["Q1"],
               body="# A claim [Q1]\n" + "\nfiller\n" * 20)
    assert any("over skills.max_lines" in p for p in _check(sandbox, store))


def test_a_directory_that_disagrees_with_the_name_is_refused(sandbox, store):
    """The directory is the address. Two answers to 'what is this called' is the
    two-encodings-of-one-fact shape (H01)."""
    _entries(sandbox, store, "Q1")
    path = make_skill(sandbox, "correct-name", cites=["Q1"])
    path.write_text(path.read_text().replace("name: correct-name", "name: other-name"))
    assert any("the directory is the address" in p for p in _check(sandbox, store))


def test_a_directory_with_no_skill_file_is_reported(sandbox, store):
    (sandbox.paths.root / sandbox.skills.dir / "empty").mkdir(parents=True)
    assert any("has no SKILL.md" in p for p in _check(sandbox, store))


def test_an_unparseable_skill_is_reported_not_silently_dropped(sandbox, store):
    path = sandbox.paths.root / sandbox.skills.dir / "broken" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# no frontmatter at all\n")
    assert any("frontmatter" in p for p in _check(sandbox, store))


def test_unknown_frontmatter_keys_are_refused(sandbox):
    with pytest.raises(SchemaError, match="unknown key"):
        skills_mod.parse("---\nname: x\ndescription: Use when\nauthor: me\n---\n\nbody\n")


# -- the derived-constant lint -------------------------------------------

def test_a_stale_derived_constant_is_refused(sandbox, store):
    """H36 ported: a number frozen into prose survived four re-rank sections
    after the value under it moved. Core computes derived constants from the
    typed goal, so a literal beside its label is checkable."""
    _entries(sandbox, store, "Q1")
    measurements = {"ops": 1_000_000.0, "peak": 100.0}
    actual = sandbox.goal.namespace(measurements)["break_even"]
    make_skill(sandbox, "quoting", cites=["Q1"],
               body=f"# A claim [Q1]\n\nbreak_even = {actual + 500:.0f} at the operating point.\n")
    found, _ = skills_mod.read_all(sandbox)
    problems = skills_mod.check(sandbox, found, store.all(), measurements)
    assert any("quotes break_even" in p for p in problems), problems


def test_a_current_derived_constant_passes(sandbox, store):
    _entries(sandbox, store, "Q1")
    measurements = {"ops": 1_000_000.0, "peak": 100.0}
    actual = sandbox.goal.namespace(measurements)["break_even"]
    make_skill(sandbox, "quoting", cites=["Q1"],
               body=f"# A claim [Q1]\n\nbreak_even = {actual:.0f} at the operating point.\n")
    found, _ = skills_mod.read_all(sandbox)
    assert skills_mod.check(sandbox, found, store.all(), measurements) == []


def test_the_lint_does_not_run_without_a_measurement(sandbox, store):
    """No scored run means the lint cannot run. That is a gap, and callers say
    so -- it must not read as a pass by producing a false problem either."""
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "quoting", cites=["Q1"],
               body="# A claim [Q1]\n\nbreak_even = 99999 at the operating point.\n")
    found, _ = skills_mod.read_all(sandbox)
    assert skills_mod.check(sandbox, found, store.all(), None) == []


# -- writing --------------------------------------------------------------

def test_write_refuses_a_skill_that_would_not_validate(sandbox, store):
    make_entry(store, "Q1")                        # open
    with pytest.raises(SchemaError, match="not terminal"):
        skills_mod.write(sandbox, "premature", "Use when it applies.", ["Q1"],
                         "# claim [Q1]\n", entries=store.all())
    assert not (sandbox.paths.root / sandbox.skills.dir / "premature").exists(), \
        "a refused skill must not leave a directory behind"


def test_write_refuses_a_name_that_is_a_path_traversal(sandbox, store):
    with pytest.raises(SchemaError, match="path traversal"):
        skills_mod.write(sandbox, "../../etc/evil", "Use when.", ["Q1"], "x",
                         entries=store.all())


def test_write_stamps_provenance_the_role_did_not_supply(sandbox, store):
    """A role does not certify its own output: `distilled` is written here."""
    _entries(sandbox, store, "Q1")
    path = skills_mod.write(sandbox, "stamped", "Use when it applies.", ["Q1"],
                            "# claim [Q1]\n", iteration=7, entries=store.all())
    skill = skills_mod.parse(path.read_text(), path)
    assert skill.distilled["iteration"] == 7 and skill.distilled["at"]


def test_retire_refuses_a_skill_that_is_not_there(sandbox):
    with pytest.raises(SchemaError, match="no skill named"):
        skills_mod.retire(sandbox, "never-existed")


def test_retire_removes_the_skill_and_its_directory(sandbox, store):
    _entries(sandbox, store, "Q1")
    path = make_skill(sandbox, "going", cites=["Q1"])
    skills_mod.retire(sandbox, "going")
    assert not path.exists() and not path.parent.exists()


# -- the index ------------------------------------------------------------

def test_the_index_carries_descriptions_and_not_bodies(sandbox, store):
    """The efficiency claim, as an assertion rather than a hope."""
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "indexed", cites=["Q1"],
               body="# A claim [Q1]\n\n" + "a long body line\n" * 200)
    found, _ = skills_mod.read_all(sandbox)
    index = skills_mod.index(sandbox, found)
    assert index[0]["description"].startswith("Use when")
    assert index[0]["path"] == "docs/skills/indexed/SKILL.md"
    assert "long body line" not in str(index)


def test_undistilled_lists_terminal_entries_no_skill_cites(sandbox, store):
    _entries(sandbox, store, "Q1", "Q2")
    make_entry(store, "Q3")                        # open: not raw material
    make_skill(sandbox, "covers-one", cites=["Q1"])
    found, _ = skills_mod.read_all(sandbox)
    pending = skills_mod.undistilled(sandbox, found, store.all())
    assert [p["id"] for p in pending] == ["Q2"]


def test_a_skill_is_addressed_by_the_track_prefixes_the_domain_declares(sandbox, store):
    """Inline citations are built from the declared prefixes, not guessed. The
    toy domain declares H as well as Q."""
    entry = make_entry(store, "H1", track="harness")
    close(sandbox, store, entry, verdict="fixed")
    make_skill(sandbox, "harness-lesson", cites=["H1"],
               body="# A claim\n\nThe scaffolding did this [H1].\n")
    assert _check(sandbox, store) == []


# -- gaps found in review -------------------------------------------------

def test_a_skill_with_no_distilled_timestamp_is_refused(sandbox, store):
    """A hand-written skill is supported; one nothing can check is not. With no
    `at`, every staleness check below returns clean by never running."""
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "handwritten", cites=["Q1"], distilled={})
    assert any("no `distilled.at`" in p for p in _check(sandbox, store))


def test_a_null_distilled_timestamp_is_refused(sandbox, store):
    """`distilled: {at: }` parses to None, and 'None' sorts above every ISO
    timestamp -- so the comparison would be permanently false."""
    _entries(sandbox, store, "Q1")
    make_skill(sandbox, "nulled", cites=["Q1"], distilled={"at": None})
    assert any("no `distilled.at`" in p for p in _check(sandbox, store))


def test_relabelling_is_caught_on_a_skill_whose_entry_stays_terminal(sandbox, store):
    """The terminality check does not cover this: a `mechanism` demoted to
    `cell` leaves the entry closed, so only the timestamp comparison sees it."""
    entry, = _entries(sandbox, store, "Q1", verdict="refuted",
                      closure_kind="mechanism")
    make_skill(sandbox, "holds", cites=["Q1"])
    entry.relabel("cell", "test", why="measured at one width only")
    entry.result.reopen_condition = "another width"
    store.save(entry)

    problems = _check(sandbox, store)
    assert any("relabel" in p for p in problems), problems
    assert sandbox.track_for("Q1").machine.status(entry.status).terminal, \
        "the entry is still closed; only the label moved"


def test_the_constant_lint_does_not_fire_on_ordinary_prose(sandbox, store):
    """`of` and `at` assert nothing. A lint that fails the build on 'a cost of
    3 runs' is the one everybody learns to skip."""
    _entries(sandbox, store, "Q1")
    measurements = {"ops": 1_000_000.0, "peak": 100.0}
    make_skill(sandbox, "prose", cites=["Q1"],
               body=("# A claim [Q1]\n\nA break_even of 99999 runs was never "
                     "reached, measured break_even at 12345 threads.\n"))
    found, _ = skills_mod.read_all(sandbox)
    assert skills_mod.check(sandbox, found, store.all(), measurements) == []


def test_undistilled_is_newest_first_by_number_not_by_string(sandbox, store):
    """Q10 sorts before Q2 as a string. Every consumer truncates this list, so
    the wrong order means the oldest work is distilled forever."""
    _entries(sandbox, store, "Q1", "Q2", "Q10")
    found, _ = skills_mod.read_all(sandbox)
    assert [p["id"] for p in skills_mod.undistilled(sandbox, found, store.all())] \
        == ["Q10", "Q2", "Q1"]


def test_write_stamps_the_core_version(sandbox, store):
    """A skill diagnosed a year later has to say what wrote it; closure
    semantics and these very check rules live in core."""
    _entries(sandbox, store, "Q1")
    path = skills_mod.write(sandbox, "versioned", "Use when it applies.", ["Q1"],
                            "# claim [Q1]\n", iteration=1, entries=store.all())
    skill = skills_mod.parse(path.read_text(), path)
    assert skill.distilled["core"], "core version must not be blank"


def test_an_event_in_the_same_second_as_the_distillation_still_counts(sandbox, store):
    """Timestamps are ISO to the second, so `>` misses a relabel that lands in
    the same second a skill was written -- which is what a human correcting a
    label right after a distil pass actually does."""
    entry, = _entries(sandbox, store, "Q1", verdict="refuted",
                      closure_kind="mechanism")
    entry.relabel("cell", "test", why="one width only")
    entry.result.reopen_condition = "another width"
    store.save(entry)
    same_second = entry.history[-1].at

    make_skill(sandbox, "tied", cites=["Q1"], distilled={
        "at": same_second, "iteration": 1, "core": "test"})
    assert any("relabel" in p for p in _check(sandbox, store))
