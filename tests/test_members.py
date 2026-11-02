"""Member domains (#63): an umbrella lists its members, and loading refuses
everything that would make an id or a path ambiguous.

starks-research ran an umbrella and four members from one checkout by hand;
these fixtures are that shape in miniature: the toy domain as the umbrella
(prefixes Q, H) and two copies of it under `domains/` with their own prefixes.
"""
import json
import shutil

import pytest

from autoresearch import cli
from autoresearch.config import DomainConfig
from autoresearch.errors import ConfigError
from autoresearch.members import Workspace
from conftest import TOY, close, make_entry


def _domain(dest, prefixes=("Q", "H"), name=None, extra=""):
    """A copy of the toy domain at `dest`, its two tracks renamed to
    `prefixes`, with `extra` appended to its domain.toml."""
    shutil.copytree(TOY, dest, ignore=shutil.ignore_patterns("__pycache__"))
    toml = dest / "domain.toml"
    text = toml.read_text()
    text = text.replace('prefix = "Q"', f'prefix = "{prefixes[0]}"')
    text = text.replace('prefix = "H"', f'prefix = "{prefixes[1]}"')
    if name:
        text = text.replace('name = "toy"', f'name = "{name}"')
    toml.write_text(text + "\n" + extra)
    (dest / "state" / "entries").mkdir(parents=True, exist_ok=True)
    return dest


MEMBERS = '[domains]\nmembers = ["domains/*"]\n'


@pytest.fixture
def umbrella(tmp_path):
    root = _domain(tmp_path / "umb", name="umb", extra=MEMBERS)
    _domain(root / "domains" / "a", ("A", "B"), name="a")
    _domain(root / "domains" / "b", ("C", "D"), name="b")
    return root


def test_an_umbrella_loads_its_members(umbrella):
    ws = Workspace.load(umbrella)
    assert ws.umbrella.name == "umb"
    assert list(ws.members) == ["domains/a", "domains/b"]
    assert [p for p, _ in ws.domains()] == [".", "domains/a", "domains/b"]


def test_for_domain_finds_the_umbrella_from_either_member(umbrella):
    for name in ("a", "b"):
        member = DomainConfig.load(umbrella / "domains" / name)
        ws = Workspace.for_domain(member)
        assert ws.root == umbrella
        assert ws.members[f"domains/{name}"] is member   # the caller's object
        assert ws.path_of(member) == f"domains/{name}"


def test_for_domain_from_the_umbrella_is_the_same_workspace(umbrella):
    config = DomainConfig.load(umbrella)
    ws = Workspace.for_domain(config)
    assert ws.umbrella is config
    assert list(ws.members) == ["domains/a", "domains/b"]


def test_a_plain_domain_is_a_workspace_of_one(sandbox):
    ws = Workspace.for_domain(sandbox)
    assert ws.umbrella is sandbox and ws.members == {}
    assert ws.domains() == [(".", sandbox)]


def test_a_domain_below_a_non_umbrella_is_alone(tmp_path):
    """A domain.toml further up that lists no members is not an umbrella."""
    outer = _domain(tmp_path / "outer")
    inner = _domain(outer / "domains" / "x", ("A", "B"))
    ws = Workspace.for_domain(DomainConfig.load(inner))
    assert ws.root == inner and ws.members == {}


def test_an_unlisted_directory_is_not_a_member(tmp_path):
    root = _domain(tmp_path / "umb",
                   extra='[domains]\nmembers = ["domains/a"]\n')
    _domain(root / "domains" / "a", ("A", "B"))
    stray = _domain(root / "domains" / "z", ("Y", "Z"))
    ws = Workspace.for_domain(DomainConfig.load(stray))
    assert ws.root == stray and ws.members == {}


# -- refusals, each naming the culprit ---------------------------------------

def test_a_shared_prefix_is_refused_naming_both(umbrella):
    toml = umbrella / "domains" / "b" / "domain.toml"
    toml.write_text(toml.read_text().replace('prefix = "C"', 'prefix = "A"'))
    with pytest.raises(ConfigError) as exc:
        Workspace.load(umbrella)
    msg = str(exc.value)
    assert "'A'" in msg and "domains/a" in msg and "domains/b" in msg


def test_a_member_sharing_the_umbrellas_prefix_is_refused(umbrella):
    toml = umbrella / "domains" / "a" / "domain.toml"
    toml.write_text(toml.read_text().replace('prefix = "B"', 'prefix = "H"'))
    with pytest.raises(ConfigError, match=r"'H'.*umbrella 'umb' \(\.\).*domains/a"):
        Workspace.load(umbrella)


def test_a_glob_matching_nothing_is_refused_naming_it(umbrella):
    toml = umbrella / "domain.toml"
    toml.write_text(toml.read_text().replace(
        '"domains/*"', '"domains/*", "goals/*"'))
    with pytest.raises(ConfigError, match="'goals/\\*' matches no directory"):
        Workspace.load(umbrella)


def test_a_match_without_a_domain_toml_is_refused_naming_it(umbrella):
    (umbrella / "domains" / "notes").mkdir()
    with pytest.raises(ConfigError, match="domains/notes, which has no domain.toml"):
        Workspace.load(umbrella)


def test_a_file_the_glob_matches_is_not_a_member(umbrella):
    (umbrella / "domains" / "README.md").write_text("members live here\n")
    assert list(Workspace.load(umbrella).members) == ["domains/a", "domains/b"]


def test_two_level_nesting_is_refused(umbrella):
    toml = umbrella / "domains" / "a" / "domain.toml"
    toml.write_text(toml.read_text() + '\n[domains]\nmembers = ["sub/*"]\n')
    _domain(umbrella / "domains" / "a" / "sub" / "x", ("X", "Y"))
    with pytest.raises(ConfigError, match="member domains/a declares members"):
        Workspace.load(umbrella)
    # and from the inner umbrella, walking up finds the outer one and refuses
    with pytest.raises(ConfigError, match="nest one level only"):
        Workspace.for_domain(DomainConfig.load(umbrella / "domains" / "a"))


def test_a_member_inside_another_member_is_refused(umbrella):
    toml = umbrella / "domain.toml"
    toml.write_text(toml.read_text().replace(
        '"domains/*"', '"domains/*", "domains/a/inner"'))
    _domain(umbrella / "domains" / "a" / "inner", ("X", "Y"))
    with pytest.raises(ConfigError, match="domains/a/inner is inside member domains/a"):
        Workspace.load(umbrella)


def test_a_member_outside_the_umbrella_is_refused(tmp_path):
    root = _domain(tmp_path / "umb", extra='[domains]\nmembers = ["../elsewhere"]\n')
    _domain(tmp_path / "elsewhere", ("A", "B"))
    with pytest.raises(ConfigError, match="not inside the umbrella"):
        Workspace.load(root)


def test_an_absolute_glob_is_refused(tmp_path):
    root = _domain(tmp_path / "umb", extra=f'[domains]\nmembers = ["{tmp_path}/x"]\n')
    with pytest.raises(ConfigError, match="is absolute"):
        Workspace.load(root)


@pytest.mark.parametrize("table, match", [
    ('[domains]\nmember = ["domains/*"]\n', r"unknown key\(s\) \['member'\]"),
    ('[domains]\nmembers = "domains/*"\n', "non-empty list"),
    ('[domains]\nmembers = []\n', "non-empty list"),
])
def test_a_malformed_domains_table_is_refused_at_load(tmp_path, table, match):
    root = _domain(tmp_path / "umb", extra=table)
    with pytest.raises(ConfigError, match=match):
        DomainConfig.load(root)


def test_validate_reports_a_broken_member_glob(umbrella):
    (umbrella / "domains" / "notes").mkdir()
    problems = DomainConfig.load(umbrella).check()
    assert [p for p in problems if p.startswith("domains:") and "domains/notes" in p]


def test_no_domains_table_changes_nothing(toy):
    assert toy.members == ()
    assert not [p for p in toy.check() if p.startswith("domains:")]


# -- `ar domains` -------------------------------------------------------------

def _seed(umbrella):
    member = DomainConfig.load(umbrella / "domains" / "a")
    from autoresearch.entries import Store
    store = Store(member.paths.entries)
    make_entry(store, "A1", track="research")
    done = make_entry(store, "A2", track="research")
    (member.paths.memos).mkdir(parents=True, exist_ok=True)
    close(member, store, done)


def test_ar_domains_lists_every_domain_from_a_member(umbrella, capsys):
    _seed(umbrella)
    assert cli.main(["--domain", str(umbrella / "domains" / "a"),
                     "domains", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["umbrella"] == str(umbrella)
    rows = doc["domains"]
    assert list(rows) == [".", "domains/a", "domains/b"]
    assert rows["."]["prefixes"] == ["Q", "H"] and rows["."]["role"] == "umbrella"
    assert (rows["domains/a"]["open"], rows["domains/a"]["closed"]) == (1, 1)
    assert rows["domains/a"]["current"] and not rows["."]["current"]


def test_ar_domains_prose(umbrella, capsys):
    assert cli.main(["--domain", str(umbrella), "domains"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"umbrella: {umbrella}"
    assert "* . (umbrella)" in out
    assert "domains/b" in out and "C D" in out


def test_ar_domains_on_a_plain_domain(sandbox, capsys):
    assert cli.main(["--domain", str(sandbox.paths.root), "domains"]) == 0
    out = capsys.readouterr().out
    assert "no [domains] members" in out and "Q H" in out


def test_a_member_validate_reports_a_prefix_clash_with_a_sibling(umbrella):
    """The clash makes the member's own ids ambiguous, so validating the
    member alone must say so, not only validating the umbrella."""
    toml = umbrella / "domains" / "b" / "domain.toml"
    toml.write_text(toml.read_text().replace('prefix = "C"', 'prefix = "A"'))
    problems = DomainConfig.load(umbrella / "domains" / "a").check()
    assert [p for p in problems if p.startswith("domains:") and "'A'" in p]


def test_a_prefix_repeated_inside_one_domain_is_left_to_its_own_check(tmp_path):
    root = _domain(tmp_path / "d", prefixes=("Q", "Q"))
    ws = Workspace.for_domain(DomainConfig.load(root))   # does not raise
    assert ws.members == {}
    assert [p for p in ws.umbrella.check() if "two tracks share" in p]


def test_ar_domains_survives_a_malformed_entry_in_one_member(umbrella, capsys):
    _seed(umbrella)
    (umbrella / "domains" / "b" / "state" / "entries" / "C1.yaml").write_text(
        "id: [not, an, id\n")
    assert cli.main(["--domain", str(umbrella), "domains"]) == 0
    out = capsys.readouterr().out
    assert "! domains/b: 1 entry file(s) do not load" in out
    assert "domains/a" in out


def test_ar_domains_with_no_entries_directory(umbrella, capsys):
    shutil.rmtree(umbrella / "domains" / "a" / "state" / "entries")
    assert cli.main(["--domain", str(umbrella), "domains", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["domains"]
    assert (rows["domains/a"]["open"], rows["domains/a"]["closed"]) == (0, 0)


def test_a_glob_reaching_the_umbrella_skips_it(umbrella):
    """A glob reaching the umbrella's own directory skips it: the umbrella
    is not its own member."""
    toml = umbrella / "domain.toml"
    toml.write_text(toml.read_text().replace('"domains/*"', '"domains/*", "domains/.."'))
    assert list(Workspace.load(umbrella).members) == ["domains/a", "domains/b"]


def test_a_sibling_with_malformed_toml_is_a_named_problem_not_a_crash(umbrella):
    """A member's validate and loop start now read its siblings; a broken
    file there must be a refusal naming it, never a traceback here."""
    (umbrella / "domains" / "b" / "domain.toml").write_text("x = [\n")
    problems = DomainConfig.load(umbrella / "domains" / "a").check()
    assert [p for p in problems if p.startswith(
        f"domains: umbrella at {umbrella}: member domains/b:")
        and "not valid TOML" in p]


def test_a_siblings_own_problem_names_the_sibling(umbrella):
    (umbrella / "domains" / "b" / "goal.yaml").unlink()
    problems = DomainConfig.load(umbrella / "domains" / "a").check()
    assert [p for p in problems if p.startswith(
        f"domains: umbrella at {umbrella}: member domains/b: domain.goal")]


def test_a_broken_umbrella_is_named_from_a_member(umbrella):
    (umbrella / "goal.yaml").unlink()
    problems = DomainConfig.load(umbrella / "domains" / "a").check()
    assert [p for p in problems
            if p.startswith(f"domains: umbrella at {umbrella}: domain.goal")]


def test_ar_domains_json_on_a_plain_domain_has_no_umbrella(sandbox, capsys):
    assert cli.main(["--domain", str(sandbox.paths.root), "domains", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["umbrella"] is None and list(doc["domains"]) == ["."]


def test_a_bad_glob_in_an_umbrella_above_is_named_from_below(umbrella):
    (umbrella / "domains" / "notes").mkdir()
    problems = DomainConfig.load(umbrella / "domains" / "a").check()
    assert [p for p in problems
            if p.startswith(f"domains: umbrella at {umbrella}: [domains] member glob")]
