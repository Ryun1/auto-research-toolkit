"""H15: the record surface could not be corrected, filed or retired in place.

Three defects, one entry surface:

* `amend` reached every prose field except the ones that make the record a
  record. `--parent`, `--source` and `--mechanism` had no writer, so a
  mis-parented entry had to be closed and re-filed -- burning an id and
  appending a refutation that was never true, because there was no honest
  terminal state for "this was filed twice".
* `entry new` defaulted `--track` to `research` and read the defect track's
  own `requires_defect_evidence` rule nowhere at filing time. So a harness
  defect filed without `--track` landed on the research track, where an agent
  booting into research reads it as a lead on the score, and a defect filed
  with `--track harness` sat in the queue carrying nothing reproducible.
* An entry retired because a sibling covers it had no terminal state, stayed
  actionable, and was re-ranked every iteration.

The fix is deliberately small: writers where there were none, validation
before the record exists, and one status that means "retired, not refuted".
"""
import pytest

from autoresearch import cli
from conftest import make_entry, memo


def root_of(sandbox):
    return ["--domain", str(sandbox.paths.root), "--session", "s"]


# -- amend reaches the record's own fields ---------------------------------


def test_amend_corrects_a_mis_parented_entry(sandbox, store, capsys):
    """The live shape: Q271 was filed with --parent Q266 when the intended
    parent was Q269, and the only correction available was prose."""
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    make_entry(store, "Q3")
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q2",
                     "--parent", "Q3"]) == 0
    assert "parent" in capsys.readouterr().out
    assert store.load("Q2").parent == "Q3"


def test_amend_refuses_a_parent_that_does_not_exist(sandbox, store, capsys):
    make_entry(store, "Q2")
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q2",
                     "--parent", "Q99"]) == 2
    assert "no such entry" in capsys.readouterr().err
    assert store.load("Q2").parent == "", "a refused re-parent changes nothing"


def test_amend_refuses_a_parent_on_another_track(sandbox, store, capsys):
    make_entry(store, "H1", track="harness", title="a defect")
    make_entry(store, "Q1")
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q1",
                     "--parent", "H1"]) == 2
    assert "stays on its parent's track" in capsys.readouterr().err
    assert store.load("Q1").parent == ""


def test_amend_adds_sources_and_mechanisms_without_dropping_the_ones_there(
        sandbox, store, capsys):
    """A list written blind is how one session's list loses another's: the
    exclusion vocabulary and the citations are typed fields, not prose."""
    make_entry(store, "Q1", sources=["inbox/first.md"], mechanisms=["m1"])
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q1",
                     "--source", "inbox/second.md",
                     "--mechanism", "m2"]) == 0
    saved = store.load("Q1")
    assert saved.sources == ["inbox/first.md", "inbox/second.md"]
    assert saved.mechanisms == ["m1", "m2"]
    event = saved.history[-1]
    assert "sources +=" in event.detail and "mechanisms +=" in event.detail


def test_amend_refuses_a_source_that_is_already_recorded(sandbox, store, capsys):
    """Adding what is there is a no-op the caller should hear about, not a
    silent success that looks like a correction."""
    make_entry(store, "Q1", sources=["inbox/first.md"])
    before = store.load("Q1").history
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q1",
                     "--source", "inbox/first.md"]) == 2
    assert "already recorded" in capsys.readouterr().err
    saved = store.load("Q1")
    assert saved.sources == ["inbox/first.md"] and saved.history == before


def test_amend_drops_a_source_that_is_recorded_but_wrong(sandbox, store, capsys):
    """A list writer that can only add leaves a wrong pointer in the record
    forever, which is the same dead end the missing writer was."""
    make_entry(store, "Q1", sources=["inbox/renamed.md", "inbox/kept.md"])
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q1",
                     "--drop-source", "inbox/renamed.md",
                     "--source", "inbox/current.md"]) == 0
    saved = store.load("Q1")
    assert saved.sources == ["inbox/kept.md", "inbox/current.md"]
    detail = saved.history[-1].detail
    assert "sources -= ['inbox/renamed.md']" in detail
    assert "sources += ['inbox/current.md']" in detail


def test_amend_refuses_to_drop_a_source_that_is_not_there(sandbox, store, capsys):
    make_entry(store, "Q1", sources=["inbox/kept.md"])
    before = store.load("Q1").history
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q1",
                     "--drop-source", "inbox/never.md"]) == 2
    assert "nothing to drop" in capsys.readouterr().err
    saved = store.load("Q1")
    assert saved.sources == ["inbox/kept.md"] and saved.history == before


def test_amend_records_the_value_it_replaced(sandbox, store):
    """An amend is a blind full-value write, so two sessions correcting one
    field inside a minute lose a text silently. The replaced value rides
    along in the event, which is what makes the loss visible without an
    external note to diff against."""
    make_entry(store, "Q1", observed="the first observation")
    assert cli.main([*root_of(sandbox), "entry", "amend", "Q1",
                     "--observed", "the second observation"]) == 0
    detail = store.load("Q1").history[-1].detail
    assert "observed='the second observation'" in detail
    assert "was 'the first observation'" in detail


# -- filing validates before the record exists -----------------------------


def test_entry_new_refuses_without_a_track(sandbox, store):
    """A forgotten --track used to file on the research track, where a defect
    reads to the next agent as a lead on the score."""
    with pytest.raises(SystemExit) as exc:
        cli.main([*root_of(sandbox), "entry", "new", "a defect about the CLI"])
    assert exc.value.code == 2
    assert store.all() == []


def test_entry_new_refuses_a_defect_with_no_evidence(sandbox, store, capsys):
    """The track declares `requires_defect_evidence`; before H15 only
    `ar harness export` and `ar validate` read it, so an unreproducible
    defect could sit in the queue for a whole session."""
    assert cli.main([*root_of(sandbox), "entry", "new", "--track", "harness",
                     "the CLI refuses nothing"]) == 2
    err = capsys.readouterr().err
    assert "requires_defect_evidence" in err
    for field in ("core", "repro", "observed"):
        assert field in err
    assert store.all() == [], "a refused filing writes no record"


def test_a_refused_defect_costs_no_id(sandbox, store, capsys):
    """H15's other half: the refusal happens before anything is written, so
    the number the filing computed is free for the next one."""
    assert cli.main([*root_of(sandbox), "entry", "new", "--track", "harness",
                     "no evidence here"]) == 2
    capsys.readouterr()
    assert cli.main([*root_of(sandbox), "entry", "new", "--track", "harness",
                     "a defect with a repro", "--core", "0.8.0",
                     "--repro", "ar entry new --kind probe x",
                     "--observed", "it was refused after allocating an id"]) == 0
    assert [entry.id for entry in store.all()] == ["H1"]


# -- retirement is a terminal state ----------------------------------------


def test_superseded_closure_names_its_replacement(sandbox, store, capsys):
    make_entry(store, "Q1")
    make_entry(store, "Q2", hypothesis="the same question, filed twice")
    assert cli.main([*root_of(sandbox), "claim", "Q2", "--why", "retiring it"]) == 0
    assert cli.main([*root_of(sandbox), "close", "Q2", "superseded",
                     "--superseded-by", "Q1",
                     "--memo", memo(sandbox, "inbox/Q2.md")]) == 0
    assert "superseded by Q1" in capsys.readouterr().out
    retired = store.load("Q2")
    assert retired.status == "superseded"
    # A retirement is not an experiment, and the disposition is what ranking
    # and the stagnation counters read.
    assert retired.result.disposition == "superseded"
    assert store.load("Q1").supersedes == ["Q2"]
    assert any(event.kind == "superseded" for event in store.load("Q1").history)


def test_superseded_without_a_replacement_is_refused(sandbox, store, capsys):
    """Retirement that names nothing is how a duplicate outlives the entry it
    duplicated: nobody can tell which entry absorbed it."""
    make_entry(store, "Q2")
    assert cli.main([*root_of(sandbox), "claim", "Q2", "--why", "retiring it"]) == 0
    assert cli.main([*root_of(sandbox), "close", "Q2", "superseded",
                     "--memo", memo(sandbox, "inbox/Q2.md")]) == 2
    assert "--superseded-by" in capsys.readouterr().err
    assert store.load("Q2").status == "in-progress"


def test_superseded_by_a_missing_entry_is_refused(sandbox, store, capsys):
    make_entry(store, "Q2")
    assert cli.main([*root_of(sandbox), "claim", "Q2", "--why", "retiring it"]) == 0
    assert cli.main([*root_of(sandbox), "close", "Q2", "superseded",
                     "--superseded-by", "Q99",
                     "--memo", memo(sandbox, "inbox/Q2.md")]) == 2
    assert "Q99" in capsys.readouterr().err
    assert store.load("Q2").status == "in-progress"


def test_superseded_by_is_refused_on_another_closure(sandbox, store, capsys):
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    assert cli.main([*root_of(sandbox), "claim", "Q2", "--why", "closing it"]) == 0
    assert cli.main([*root_of(sandbox), "close", "Q2", "refuted",
                     "--closure", "cell",
                     "--superseded-by", "Q1",
                     "--memo", memo(sandbox, "inbox/Q2.md")]) == 2
    assert "belongs to" in capsys.readouterr().err


def test_a_retired_entry_reopens(sandbox, store, capsys):
    """Every terminal status keeps its reopen edge; a retirement that could
    not be undone would be terminal by omission (H111)."""
    assert "superseded" in sandbox.track_for("Q1").machine.terminal_names
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    assert cli.main([*root_of(sandbox), "claim", "Q2", "--why", "retiring it"]) == 0
    assert cli.main([*root_of(sandbox), "close", "Q2", "superseded",
                     "--superseded-by", "Q1",
                     "--memo", memo(sandbox, "inbox/Q2.md")]) == 0
    assert cli.main([*root_of(sandbox), "close", "Q2", "--reopen", "--why",
                     "the sibling it deferred to was refuted"]) == 0
    assert store.load("Q2").status == "queued"


def test_a_retired_sibling_is_not_evidence_against_its_parent(sandbox, store):
    """The consumer-visible half: ranking counts refuted experiment siblings
    to mark a parent stagnant. A retired entry was never an experiment, so it
    must not appear in that tally."""
    from autoresearch import rank

    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1", hypothesis="filed twice")
    assert cli.main([*root_of(sandbox), "claim", "Q2", "--why", "retiring it"]) == 0
    assert cli.main([*root_of(sandbox), "close", "Q2", "superseded",
                     "--superseded-by", "Q1",
                     "--memo", memo(sandbox, "inbox/Q2.md")]) == 0
    machine_for = lambda entry_id: sandbox.track_for(entry_id).machine  # noqa: E731
    assert rank.stagnant_parents(store.all(), machine_for, threshold=1) == {}
