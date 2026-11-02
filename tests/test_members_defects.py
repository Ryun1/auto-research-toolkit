"""Members share the umbrella's defect track (#67).

A member that declares no defect track files harness debt, from the CLI and
from the loop, on the umbrella's -- and `harness export` reads the same
bundle from anywhere in the workspace. A member that declares its own keeps it.
"""
import json

import pytest

from autoresearch import cli, defects
from autoresearch.config import DomainConfig
from autoresearch.driver.brain import Role, ScriptedBrain
from autoresearch.driver.loop import Coordinator
from autoresearch.entries import Store
from conftest import MEMBERS, make_entry, member_domain


@pytest.fixture
def shared(tmp_path):
    """The umbrella (Q, H) over members with a research track only:
    `domains/a` (A) and `domains/b` (C). Only the umbrella has a defect track."""
    root = member_domain(tmp_path / "umb", name="umb", extra=MEMBERS)
    member_domain(root / "domains" / "a", ("A", "B"), name="a",
                  drop_tracks=("B",))
    member_domain(root / "domains" / "b", ("C", "D"), name="b",
                  drop_tracks=("D",))
    return root


def _ar(root, *args):
    return cli.main(["--domain", str(root), "--session", "s", *args])


EVIDENCE = ("--core", "0.1", "--repro", "ar measure", "--observed", "it hung")


def _ids(root):
    return sorted(p.stem for p in (root / "state" / "entries").glob("*.yaml"))


def test_a_member_with_no_defect_track_finds_the_umbrellas(shared):
    member = DomainConfig.load(shared / "domains" / "a")
    home = defects.defect_home(member)
    assert home.shared and home.track.id == "harness"
    assert home.config.paths.root == shared
    assert home.store.dir == shared / "state" / "entries"


def test_entry_new_harness_from_a_member_files_in_the_umbrella(shared, capsys):
    member = shared / "domains" / "a"
    assert _ar(member, "entry", "new", "--track", "harness", *EVIDENCE,
               "measure has no timeout") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[:2] == [
        str(shared / "state" / "entries" / "H1.yaml"),
        f"filed in umbrella {shared} (shared defect track 'harness')"]
    assert _ids(shared) == ["H1"]
    assert _ids(member) == []
    assert "H1" in (shared / "docs" / "log" / "Harness Debt.md").read_text()
    assert _ar(shared, "validate") == 0, capsys.readouterr().out
    assert _ar(member, "validate") == 0, capsys.readouterr().out


def test_entry_new_by_id_prefix_from_a_member(shared, capsys):
    member = shared / "domains" / "a"
    assert _ar(member, "entry", "new", "--track", "harness", "--id", "H7",
               *EVIDENCE, "measure has no timeout") == 0
    assert capsys.readouterr().out.splitlines()[0].endswith("H7.yaml")
    assert _ids(shared) == ["H7"] and _ids(member) == []


def test_entry_new_shared_defect_still_requires_evidence(shared, capsys):
    member = shared / "domains" / "a"
    assert _ar(member, "entry", "new", "--track", "harness", "--core", "0.1",
               "--observed", "it hung", "measure has no timeout") == 2
    err = capsys.readouterr().err
    assert ("track 'harness' requires_defect_evidence and this entry is "
            "missing repro") in err
    assert _ids(shared) == [] and _ids(member) == []


def test_harness_export_same_from_member_and_umbrella(shared, tmp_path, capsys):
    umb = DomainConfig.load(shared)
    store = Store(umb.paths.entries)
    make_entry(store, "H1", track="harness", core="0.1", repro="r",
               observed="o")
    bundles = []
    for root, name in ((shared, "umb.json"), (shared / "domains" / "a", "a.json")):
        out = tmp_path / name
        assert _ar(root, "harness", "export", "--out", str(out)) == 0
        bundle = json.loads(out.read_text())
        bundle.pop("exported_at")
        bundles.append(bundle)
    assert bundles[0] == bundles[1]
    assert bundles[0]["project"]["name"] == "umb"
    assert [d["id"] for d in bundles[0]["defects"]] == ["H1"]


def test_a_member_with_its_own_defect_track_keeps_it(umbrella, capsys):
    member = umbrella / "domains" / "a"
    own = defects.defect_home(DomainConfig.load(member))
    assert not own.shared and own.track.prefix == "B"
    assert _ar(member, "entry", "new", "--track", "harness", *EVIDENCE,
               "measure has no timeout") == 0
    assert capsys.readouterr().out.splitlines() == [
        str(member / "state" / "entries" / "B1.yaml")]
    assert _ids(member) == ["B1"] and _ids(umbrella) == []


def _qc_brain():
    """A loop iteration whose QC reports one item of harness debt."""
    return ScriptedBrain({
        Role.GENERATOR: lambda b: [],
        Role.JUDGE: lambda b: [],
        Role.WORKER: lambda b: {"verdict": "inconclusive", "summary": "n/a",
                                "verification": {"reread": True,
                                                 "claims_checked": ["summary"]}},
        Role.CURATOR: lambda b: {"reprice": [], "notes": []},
        Role.QC: lambda b: {"problems": [], "verdict": "problems", "harness_debt": [
            {"title": "measure command has no timeout",
             "repro": "ar measure  # with a hanging measure",
             "observed": "the iteration never finished"}]}})


def test_loop_qc_harness_debt_from_a_member_lands_in_the_umbrella(shared):
    member = DomainConfig.load(shared / "domains" / "a")
    make_entry(Store(member.paths.entries), "A1", impact=1.0)
    it = Coordinator(member, _qc_brain()).run_iteration(1)
    qc = next(p for p in it.phases if p.name == "qc")
    assert "filed H1 (harness debt)" in qc.detail
    assert _ids(shared) == ["H1"]
    assert _ids(shared / "domains" / "a") == ["A1"]
    from autoresearch.skills import core_version
    assert Store(shared / "state" / "entries").load("H1").core == core_version()


def test_loop_qc_filing_on_the_umbrella_re_renders_its_views(shared, capsys):
    """Filing H1 in the umbrella's store without re-rendering its views left
    the umbrella's `validate` red: the view differed from the records."""
    member = DomainConfig.load(shared / "domains" / "a")
    make_entry(Store(member.paths.entries), "A1", impact=1.0)
    Coordinator(member, _qc_brain()).run_iteration(1)
    assert _ids(shared) == ["H1"]
    capsys.readouterr()
    assert cli.main(["--domain", str(shared), "validate"]) == 0, \
        capsys.readouterr()
    assert "H1" in (shared / "docs" / "log" / "Harness Debt.md").read_text()


def test_lone_domain_qc_debt_without_a_harness_track_is_dropped_as_before(
        tmp_path):
    """No umbrella: exactly as before defect homes existed. A defect track
    not named `harness` does not take QC debt; it is dropped with the old
    note (#67 final review)."""
    lone = member_domain(tmp_path / "lone", name="lone")
    toml = lone / "domain.toml"
    toml.write_text(toml.read_text().replace('id     = "harness"',
                                             'id     = "defects"'))
    config = DomainConfig.load(lone)
    make_entry(Store(config.paths.entries), "Q1", impact=1.0)
    it = Coordinator(config, _qc_brain()).run_iteration(1)
    qc = next(p for p in it.phases if p.name == "qc")
    assert ("harness debt not filed: no 'harness' track (1 item(s) dropped)"
            in qc.detail)
    assert _ids(lone) == ["Q1"]


def test_outside_a_workspace_undeclared_track_message_unchanged(tmp_path, capsys):
    lone = member_domain(tmp_path / "lone", ("A", "B"), name="lone",
                         drop_tracks=("B",))
    assert _ar(lone, "entry", "new", "--track", "harness", *EVIDENCE,
               "measure has no timeout") == 2
    assert ("track 'harness' is not declared by this domain; declared: "
            "['research']") in capsys.readouterr().err
    assert _ar(lone, "entry", "new", "--track", "research", "--id", "H7",
               "a lead") == 2
    assert "no track owns entry id 'H7'; prefixes are ['A']" in \
        capsys.readouterr().err


def test_a_shared_defect_id_on_a_local_track_is_refused(shared, capsys):
    """`--id H7` on the member's research track is the wrong-track filing a
    single domain refuses; reaching the umbrella must not turn it into a
    harness defect the caller never named (H15)."""
    member = shared / "domains" / "a"
    assert _ar(member, "entry", "new", "--track", "research", "--id", "H7",
               "a lead") == 2
    assert ("id 'H7' belongs to track 'harness' (prefix 'H'), not 'research' "
            "(prefix 'A')") in capsys.readouterr().err
    assert _ids(shared) == [] and _ids(member) == []
