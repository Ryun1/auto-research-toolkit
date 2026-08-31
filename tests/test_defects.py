"""The field-defect boundary: evidence gates, export, ingest.

An entry on a defect track must carry `core`, `repro` and `observed` -- the
evidence that makes it reproducible by someone who has never seen this project.
Every gate here has a negative test naming the missing field, because a gate
that can be deleted without a test failing is a comment (invariant 3), and a
refusal without the field name sends the reporter diff-hunting.
"""
import json
import pathlib

import pytest

from autoresearch import defects as defects_mod
from autoresearch.cli import main as ar
from autoresearch.config import DomainConfig
from autoresearch.driver.brain import Role, ScriptedBrain
from autoresearch.driver.loop import Coordinator
from autoresearch.entries import Entry, Store
from autoresearch.errors import AutoresearchError
from autoresearch.scaffold import init
from autoresearch.skills import core_version
from conftest import make_entry


def file_defect(store, entry_id="H1", **over):
    data = dict(id=entry_id, track="harness",
                title="the judge brief drops the shortlist marks",
                core="0.1.0", repro="ar rank --top 3",
                observed="reserved slots arrive unlabelled",
                expected="the brief names which entries hold reserved slots")
    data.update(over)
    entry = Entry(**data)
    store.save(entry)
    return entry


# -- the evidence gate -------------------------------------------------------

def test_validate_refuses_a_defect_entry_missing_evidence(sandbox, capsys):
    store = Store(sandbox.paths.entries)
    file_defect(store, repro="", observed="")
    ar(["--domain", str(sandbox.paths.root), "render"])
    assert ar(["--domain", str(sandbox.paths.root), "validate"]) == 1
    out = capsys.readouterr().out
    assert "missing observed, repro" in out, out


def test_the_refusal_names_each_missing_field(sandbox):
    store = Store(sandbox.paths.entries)
    file_defect(store, core="", repro="", observed="", expected="")
    track = sandbox.track_for("H1")
    problems = defects_mod.evidence_problems(store.load("H1"), track)
    assert len(problems) == 1
    assert "core, observed, repro" in problems[0]


def test_validate_passes_a_complete_defect_entry(sandbox):
    store = Store(sandbox.paths.entries)
    file_defect(store)
    ar(["--domain", str(sandbox.paths.root), "render"])
    assert ar(["--domain", str(sandbox.paths.root), "validate"]) == 0


def test_an_ordinary_track_is_not_gated(sandbox):
    """The gate is a property of the track, so a research entry with no `core`
    is normal -- only the defect track demands the evidence."""
    store = Store(sandbox.paths.entries)
    make_entry(store, "Q1")
    ar(["--domain", str(sandbox.paths.root), "render"])
    assert ar(["--domain", str(sandbox.paths.root), "validate"]) == 0


# -- export ------------------------------------------------------------------

def test_export_refuses_an_incomplete_defect_and_names_the_field(sandbox):
    store = Store(sandbox.paths.entries)
    file_defect(store, repro="")
    bundle, refused, stats = defects_mod.export_bundle(
        sandbox, store.all())
    assert bundle["defects"] == []
    assert len(refused) == 1
    assert refused[0][0] == "H1" and "repro" in refused[0][1]
    assert stats["read"] == 1


def test_export_refuses_a_domain_with_no_defect_track(sandbox):
    sandbox.tracks["harness"].requires_defect_evidence = False
    with pytest.raises(AutoresearchError, match="no track with"):
        defects_mod.export_bundle(sandbox, [])


def test_export_carries_project_provenance(sandbox):
    store = Store(sandbox.paths.entries)
    file_defect(store)
    bundle, refused, _ = defects_mod.export_bundle(sandbox, store.all())
    assert refused == []
    assert bundle["schema"] == defects_mod.BUNDLE_SCHEMA
    assert bundle["project"]["name"] == sandbox.name
    assert bundle["project"]["core"] == core_version()
    assert bundle["project"]["host"]


def test_closed_defects_are_skipped_until_all_is_asked(sandbox):
    store = Store(sandbox.paths.entries)
    entry = file_defect(store)
    from conftest import close
    close(sandbox, store, entry, verdict="fixed")
    bundle, refused, stats = defects_mod.export_bundle(sandbox, store.all())
    assert refused == [] and bundle["defects"] == []
    assert stats["closed_skipped"] == 1
    bundle, refused, _ = defects_mod.export_bundle(sandbox, store.all(),
                                                   all_status=True)
    assert [d["id"] for d in bundle["defects"]] == ["H1"]


def test_export_cli_exit_codes_and_bundle_file(sandbox, tmp_path, capsys):
    store = Store(sandbox.paths.entries)
    file_defect(store, observed="")
    assert ar(["--domain", str(sandbox.paths.root), "harness", "export",
               "--out", str(tmp_path / "b.json")]) == 1
    assert "refused H1" in capsys.readouterr().out
    assert not (tmp_path / "b.json").exists(), \
        "a bundle of nothing must not be written as if it were one"

    ar(["--domain", str(sandbox.paths.root), "entry", "amend", "H1",
        "--observed", "reserved slots arrive unlabelled"])
    assert ar(["--domain", str(sandbox.paths.root), "harness", "export",
               "--out", str(tmp_path / "b.json")]) == 0
    out = capsys.readouterr().out
    assert "exported 1, refused 0" in out
    bundle = json.loads((tmp_path / "b.json").read_text())
    assert bundle["defects"][0]["observed"] == "reserved slots arrive unlabelled"


# -- ingest ------------------------------------------------------------------

def make_bundle(entry_dict, project="widgets"):
    return {"schema": defects_mod.BUNDLE_SCHEMA,
            "exported_at": "2026-09-01T00:00:00+00:00",
            "project": {"name": project, "core": "0.1.0", "host": "somewhere"},
            "defects": [entry_dict]}


def test_a_bundle_roundtrips_into_ordinary_entries(tmp_path, toy):
    source = Store(tmp_path / "field")
    file_defect(source)
    bundle, _, _ = defects_mod.export_bundle(toy, source.all())
    target = Store(tmp_path / "upstream")
    filed, skipped = defects_mod.ingest_bundle(bundle, target, prefix="F")
    assert skipped == [] and len(filed) == 1
    entry = target.load("F1")
    assert entry.title == source.load("H1").title
    assert entry.core == "0.1.0" and entry.repro and entry.observed
    assert entry.tags == ["from:toy/H1"]


def test_reingesting_the_same_bundle_files_nothing_twice(tmp_path):
    target = Store(tmp_path / "upstream")
    bundle = make_bundle({"id": "H1", "title": "the judge brief drops marks",
                          "core": "0.1.0", "repro": "ar rank",
                          "observed": "unlabelled reserve"})
    filed, _ = defects_mod.ingest_bundle(bundle, target)
    assert len(filed) == 1
    filed, skipped = defects_mod.ingest_bundle(bundle, target)
    assert filed == []
    assert skipped == [("H1", "already ingested (from:widgets/H1)")]


def test_ingest_skips_an_incomplete_defect_and_names_the_field(tmp_path):
    target = Store(tmp_path / "upstream")
    bundle = make_bundle({"id": "H2", "title": "no repro attached",
                          "core": "0.1.0", "observed": "it broke"})
    filed, skipped = defects_mod.ingest_bundle(bundle, target)
    assert filed == []
    assert skipped == [("H2", "incomplete: missing repro; "
                              "export upstream should have refused it")]


def test_ingest_refuses_an_unknown_schema(tmp_path):
    with pytest.raises(AutoresearchError, match="refusing to guess"):
        defects_mod.ingest_bundle({"schema": "ar-defect-bundle-9", "defects": []},
                                  Store(tmp_path / "upstream"))


def test_the_ingest_script_runs_end_to_end(tmp_path):
    """The script is how a bundle lands here; it must work unattended, report
    its counts, and refuse an incomplete bundle with a failing exit."""
    import subprocess
    import sys
    target = tmp_path / "entries"
    target.mkdir()
    bundle = make_bundle({"id": "H1", "title": "judge brief drops marks",
                          "core": "0.1.0", "repro": "ar rank --top 3",
                          "observed": "unlabelled reserve"})
    path = tmp_path / "b.json"
    path.write_text(json.dumps(bundle))
    here = pathlib.Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(here / "scripts" / "ingest-defects.py"),
         str(path), "--into", str(target)],
        capture_output=True, text=True, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "filed 1" in proc.stdout
    assert (target / "F1.yaml").exists()

    incomplete = make_bundle({"id": "H2", "title": "no repro",
                              "core": "0.1.0", "observed": "broke"})
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(incomplete))
    proc = subprocess.run(
        [sys.executable, str(here / "scripts" / "ingest-defects.py"),
         str(bad), "--into", str(target)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert "incomplete: missing repro" in proc.stdout


# -- where the evidence comes from ------------------------------------------

def test_qc_files_harness_debt_with_core_stamped(sandbox):
    """The reporter is the one place the running core version is known for
    certain, so the loop stamps it; QC is prompted to carry a repro."""
    store = Store(sandbox.paths.entries)
    make_entry(store, "Q1", impact=1.0)
    brain = ScriptedBrain({
        Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a"},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "verdict": "problems", "harness_debt": [
            {"title": "measure command has no timeout",
             "hypothesis": "a hung run stalls the pool",
             "repro": "ar --domain domains/toy measure  # with a hanging measure",
             "observed": "the iteration never finished"}]}})
    Coordinator(sandbox, brain).run_iteration(1)
    debt = [e for e in store.all() if e.id.startswith("H")]
    assert len(debt) == 1
    assert debt[0].core == core_version()
    assert debt[0].repro and debt[0].observed


# -- what a new project arrives with ----------------------------------------

def test_the_scaffold_declares_the_defect_track(tmp_path):
    init(tmp_path / "d", name="d")
    config = DomainConfig.load(tmp_path / "d")
    assert config.tracks["harness"].requires_defect_evidence is True


def test_the_scaffold_makes_publishing_human_only(tmp_path):
    """The rule ships active, and -- invariant 3 -- it demonstrably refuses the
    thing an agent would run to file upstream itself."""
    init(tmp_path / "d", name="d")
    config = DomainConfig.load(tmp_path / "d")
    rules = [r for r in config.policy.human_only
             if r.pattern == "gh issue create"]
    assert rules, "the publishing gate is missing from the scaffold"
    with pytest.raises(Exception, match="human-only"):
        config.policy.check_command(rules[0].example)
