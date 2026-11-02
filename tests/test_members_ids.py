"""An entry id resolves to its owning domain across the workspace (#65)."""
import shutil

import pytest

from autoresearch import members
from autoresearch.config import DomainConfig
from autoresearch.entries import Store
from autoresearch.errors import ConfigError, SchemaError
from conftest import make_entry


def _load(root):
    return DomainConfig.load(root)


def test_resolve_finds_an_entry_in_a_sibling(umbrella):
    sibling = _load(umbrella / "domains" / "b")
    make_entry(Store(sibling.paths.entries), "C1", track="research")
    owner, entry = members.resolve(_load(umbrella / "domains" / "a"), "C1")
    assert owner.paths.root == sibling.paths.root
    assert entry.id == "C1"


def test_resolve_from_the_umbrella(umbrella):
    member = _load(umbrella / "domains" / "a")
    make_entry(Store(member.paths.entries), "A1", track="research")
    owner, entry = members.resolve(_load(umbrella), "A1")
    assert owner.paths.root == member.paths.root and entry.id == "A1"


def test_resolve_unowned_id_is_an_error(umbrella):
    with pytest.raises(ConfigError, match=r"no domain in this workspace owns "
                                          r"entry id 'X1'; prefixes are \["):
        members.resolve(_load(umbrella), "X1")


def test_resolve_missing_file_is_the_owners_schema_error(umbrella):
    with pytest.raises(SchemaError, match="no entry 'C9'"):
        members.resolve(_load(umbrella / "domains" / "a"), "C9")


def test_owner_of_does_not_read_stores(umbrella):
    shutil.rmtree(umbrella / "domains" / "b" / "state" / "entries")
    workspace = members.Workspace.load(umbrella)
    path, owner = workspace.owner_of("C1")
    assert path == "domains/b" and owner.name == "b"
    assert workspace.entry("C1") is None
    assert workspace.owner_of("X1") is None


def test_workspace_or_none_on_a_broken_umbrella(umbrella):
    member = _load(umbrella / "domains" / "a")
    (umbrella / "domains" / "b" / "domain.toml").write_text("[domain]\n")
    assert members.workspace_or_none(member) is None


def test_workspace_or_none_for_a_member(umbrella):
    workspace = members.workspace_or_none(_load(umbrella / "domains" / "a"))
    assert [p for p, _ in workspace.domains()] == [".", "domains/a", "domains/b"]


# -- the resolver at the call sites (#65) --------------------------------------

def _ar(root, *args):
    from autoresearch import cli
    return cli.main(["--domain", str(root), "--session", "s", *args])


def _seed_c1(umbrella):
    sibling = _load(umbrella / "domains" / "b")
    return make_entry(Store(sibling.paths.entries), "C1", track="research")


def test_close_superseded_by_a_member_entry(umbrella, capsys):
    from conftest import memo
    umb = _load(umbrella)
    make_entry(Store(umb.paths.entries), "Q1")
    _seed_c1(umbrella)
    assert _ar(umbrella, "claim", "Q1", "--why", "retiring it") == 0
    assert _ar(umbrella, "close", "Q1", "superseded", "--superseded-by", "C1",
               "--memo", memo(umb, "inbox/Q1.md"),
               "--summary", "domains/b absorbed it") == 0
    out = capsys.readouterr().out
    assert "— superseded by C1 (member domains/b)" in out
    sibling = _load(umbrella / "domains" / "b")
    assert Store(sibling.paths.entries).load("C1").supersedes == ["Q1"]
    assert not Store(umb.paths.entries).path("C1").exists()
    for root in (umbrella, umbrella / "domains" / "b"):
        capsys.readouterr()
        assert _ar(root, "validate") == 0, capsys.readouterr().out


def test_close_superseded_by_unowned_id_is_refused_outside_a_workspace_unchanged(
        sandbox, store, capsys):
    from conftest import memo
    make_entry(store, "Q2")
    assert _ar(sandbox.paths.root, "claim", "Q2", "--why", "retiring it") == 0
    capsys.readouterr()
    assert _ar(sandbox.paths.root, "close", "Q2", "superseded",
               "--superseded-by", "C1",
               "--memo", memo(sandbox, "inbox/Q2.md")) == 2
    assert capsys.readouterr().err == (
        "ar: cannot retire Q2 as superseded: no entry 'C1' (an outside record "
        "is named with a scheme, e.g. https://... or hosted:<id>)\n")


def test_close_superseded_by_an_unowned_id_inside_a_workspace(umbrella, capsys):
    from conftest import memo
    umb = _load(umbrella)
    make_entry(Store(umb.paths.entries), "Q1")
    assert _ar(umbrella, "claim", "Q1", "--why", "retiring it") == 0
    capsys.readouterr()
    assert _ar(umbrella, "close", "Q1", "superseded", "--superseded-by", "X1",
               "--memo", memo(umb, "inbox/Q1.md")) == 2
    assert "no domain in this workspace owns entry id 'X1'" in capsys.readouterr().err


def test_close_superseded_by_a_missing_member_entry_names_the_member(
        umbrella, capsys):
    from conftest import memo
    umb = _load(umbrella)
    make_entry(Store(umb.paths.entries), "Q1")
    assert _ar(umbrella, "claim", "Q1", "--why", "retiring it") == 0
    capsys.readouterr()
    assert _ar(umbrella, "close", "Q1", "superseded", "--superseded-by", "C9",
               "--memo", memo(umb, "inbox/Q1.md")) == 2
    assert ("cannot retire Q1 as superseded: member domains/b has no entry "
            "'C9'") in capsys.readouterr().err
    assert Store(umb.paths.entries).load("Q1").status == "in-progress"


@pytest.mark.parametrize("flag", ["--parent", "--derived-from"])
def test_cross_member_parent_is_refused(umbrella, capsys, flag):
    _seed_c1(umbrella)
    assert _ar(umbrella, "entry", "new", "--track", "research", "a branch",
               flag, "C1") == 2
    assert capsys.readouterr().err == (
        "ar: cannot file a branch of C1: it belongs to member domains/b; "
        "lineage (--parent, --derived-from) stays inside one domain -- cite it "
        "with --source C1 instead\n")
    assert Store(_load(umbrella).paths.entries).all() == []


def _skill_over_q1(umbrella, **skill):
    from conftest import close, make_skill
    umb = _load(umbrella)
    umb_store = Store(umb.paths.entries)
    entry = close(umb, umb_store, make_entry(umb_store, "Q1"),
                  verdict="refuted", closure_kind="mechanism")
    member = _load(umbrella / "domains" / "a")
    make_skill(member, "holds", cites=["Q1"], **skill)
    return umb_store, entry, member


def _skill_check(member):
    from autoresearch import skills as skills_mod
    found, problems = skills_mod.read_all(member)
    entries = Store(member.paths.entries).all()
    return problems + skills_mod.check(member, found, entries)


def test_skill_citing_umbrella_entry_terminal_passes(umbrella):
    _, _, member = _skill_over_q1(umbrella)
    assert _skill_check(member) == []


def test_skill_citing_umbrella_entry_moved_after_distillation_is_flagged(umbrella):
    umb_store, entry, member = _skill_over_q1(umbrella)
    entry.relabel("cell", "test", why="measured at one width only",
                  reopen_condition="a second width")
    umb_store.save(entry)
    problems = _skill_check(member)
    assert [p for p in problems if "cites Q1, which has relabel" in p
            and "since this skill was distilled" in p], problems


def test_skill_citing_an_open_umbrella_entry_is_flagged(umbrella):
    from conftest import make_skill
    make_entry(Store(_load(umbrella).paths.entries), "Q1")
    member = _load(umbrella / "domains" / "a")
    make_skill(member, "holds", cites=["Q1"])
    assert [p for p in _skill_check(member)
            if "cites Q1, which is 'queued' and not terminal" in p]


def test_skill_inline_cross_member_cite_is_recognised(umbrella):
    _, _, member = _skill_over_q1(
        umbrella, body="# holds states one claim\n\nIt held [Q1].\n")
    assert not [p for p in _skill_check(member) if "never cites it" in p]


def test_links_emit_cross_member_cites(umbrella):
    from autoresearch import links as links_mod
    from autoresearch.ui import graph
    umb = _load(umbrella)
    _seed_c1(umbrella)
    q1 = make_entry(Store(umb.paths.entries), "Q1",
                    sources=["C1", "docs/notes.md"])
    assert links_mod.links([q1], foreign={"C1"}) == [
        links_mod.Link("C1", "Q1", "cites")]
    assert links_mod.links([q1]) == []
    assert {"src": "C1", "dst": "Q1", "kind": "cites"} in graph.graph(umb)["links"]
    panel = graph.entry(umb, "Q1", {})
    assert [p["path"] for p in panel["paths"]] == ["docs/notes.md"]
    assert [u["id"] for u in panel["upstream"]] == ["C1"]


def test_validate_flags_a_dangling_cross_member_source(umbrella):
    from autoresearch import validate
    umb = _load(umbrella)
    q58 = make_entry(Store(umb.paths.entries), "Q58", sources=["C9"],
                     supersedes=["C8"])
    assert validate.cross_member_problems(umb, [q58]) == [
        "Q58 sources C9: member domains/b has no such entry",
        "Q58 supersedes C8: member domains/b has no such entry"]


def test_validate_cross_member_source_clean_when_it_resolves(umbrella, capsys):
    from autoresearch import validate
    umb = _load(umbrella)
    _seed_c1(umbrella)
    q58 = make_entry(Store(umb.paths.entries), "Q58", sources=["C1"])
    assert validate.cross_member_problems(umb, [q58]) == []


def test_validate_reports_cross_member_problems(umbrella, capsys):
    umb = _load(umbrella)
    make_entry(Store(umb.paths.entries), "Q58", sources=["C9"])
    assert _ar(umbrella, "render") == 0
    capsys.readouterr()
    assert _ar(umbrella, "validate") == 1
    assert "Q58 sources C9: member domains/b has no such entry" in \
        capsys.readouterr().out


def test_cross_member_problems_outside_a_workspace_are_none(sandbox, store):
    from autoresearch import validate
    q1 = make_entry(store, "Q1", sources=["C9"])
    assert validate.cross_member_problems(sandbox, [q1]) == []


def test_entry_show_from_another_member(umbrella, capsys):
    import json
    _seed_c1(umbrella)
    assert _ar(umbrella / "domains" / "a", "entry", "show", "C1") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["id"] == "C1"
    assert captured.err == "ar: C1 is in member domains/b (b)\n"


def test_entry_show_missing_member_entry_is_still_refused(umbrella, capsys):
    assert _ar(umbrella / "domains" / "a", "entry", "show", "C9") == 2


def test_resolver_survives_a_broken_sibling(umbrella, capsys):
    from conftest import make_skill
    member = _load(umbrella / "domains" / "a")
    make_skill(member, "holds", cites=["Q1"])
    (umbrella / "domains" / "b" / "domain.toml").write_text("x = [\n")
    assert _ar(umbrella / "domains" / "a", "skill", "check") == 1
    out = capsys.readouterr().out
    assert f"domains: umbrella at {umbrella}: member domains/b:" in out
    assert "not valid TOML" in out


# -- `ar domains relink` (#65 migration) ---------------------------------------

URL = "https://github.com/o/r/blob/main/domains/b/state/entries/C1.yaml"


def _retire_q1_by(umbrella, replacement):
    from conftest import memo
    umb = _load(umbrella)
    make_entry(Store(umb.paths.entries), "Q1")
    assert _ar(umbrella, "claim", "Q1", "--why", "retiring it") == 0
    assert _ar(umbrella, "close", "Q1", "superseded", "--superseded-by",
               replacement, "--memo", memo(umb, "inbox/Q1.md"),
               "--summary", "absorbed elsewhere") == 0


def _snapshot(umbrella):
    return {p: p.read_bytes() for p in umbrella.rglob("*.yaml")}


def test_relink_finds_a_github_url_to_a_member_entry(umbrella, capsys):
    _seed_c1(umbrella)
    _retire_q1_by(umbrella, URL)
    before = _snapshot(umbrella)
    capsys.readouterr()
    assert _ar(umbrella, "domains", "relink") == 0
    out = capsys.readouterr().out
    assert "Q1" in out and "C1" in out and URL in out
    assert _snapshot(umbrella) == before


def test_relink_write(umbrella, capsys):
    _seed_c1(umbrella)
    _retire_q1_by(umbrella, URL)
    capsys.readouterr()
    assert _ar(umbrella, "domains", "relink", "--write") == 0
    c1 = Store(_load(umbrella / "domains" / "b").paths.entries).load("C1")
    assert c1.supersedes == ["Q1"]
    q1 = Store(_load(umbrella).paths.entries).load("Q1")
    assert q1.history[-1].kind == "superseded"
    assert q1.history[-1].detail == f"retired as superseded by C1 (relinked from {URL})"
    assert c1.history[-1].kind == "superseded"
    for root in (umbrella, umbrella / "domains" / "b"):
        capsys.readouterr()
        assert _ar(root, "validate") == 0, capsys.readouterr().out
    capsys.readouterr()
    assert _ar(umbrella, "domains", "relink") == 0
    assert "Q1" not in capsys.readouterr().out


def test_relink_ignores_urls_to_missing_entries(umbrella, capsys):
    _retire_q1_by(umbrella, URL)  # C1 was never filed
    capsys.readouterr()
    assert _ar(umbrella, "domains", "relink", "--write") == 0
    assert "Q1" not in capsys.readouterr().out
    assert _ar(umbrella, "domains", "relink") == 0
