"""`ar init --member` scaffolds a member under an umbrella (#68)."""
import json

import pytest

from autoresearch import cli


def _ar(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_init_member_passes_validate_and_validate_all_domains(umbrella, capsys):
    code, out, err = _ar(capsys, "--domain", str(umbrella), "init",
                         "--member", "domains/c", "--prefix", "Z")
    assert code == 0, out + err
    member = umbrella / "domains" / "c"
    for rel in ("domain.toml", "goal.yaml", "bin/measure", "AGENTS.md"):
        assert (member / rel).is_file()
    assert (member / "state" / "entries").is_dir()
    assert "Shared rules, tools and the defect track live in the umbrella: " \
           "read `../../AGENTS.md` first." in (member / "AGENTS.md").read_text()
    code, out, err = _ar(capsys, "--domain", str(member), "validate")
    assert code == 0, out + err
    code, out, err = _ar(capsys, "--domain", str(umbrella), "validate",
                         "--all-domains")
    assert code == 0, out + err


def test_init_member_appends_to_members_unless_covered(umbrella, capsys):
    toml = umbrella / "domain.toml"
    before = toml.read_bytes()
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "domains/c", "--prefix", "Z")
    assert code == 0, err
    assert toml.read_bytes() == before
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "labs/x", "--prefix", "Y")
    assert code == 0, err
    after = toml.read_text()
    assert 'members = ["domains/*", "labs/x"]' in after
    assert after.replace(', "labs/x"', "").encode() == before


def test_init_member_creates_a_domains_table_when_absent(umbrella, capsys):
    toml = umbrella / "domain.toml"
    text = toml.read_text().replace('[domains]\nmembers = ["domains/*"]\n', "")
    toml.write_text(text)
    import shutil
    shutil.rmtree(umbrella / "domains")
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "domains/c", "--prefix", "Z")
    assert code == 0, err
    assert toml.read_text() == text + '\n[domains]\nmembers = ["domains/c"]\n'


def test_init_member_keeps_comments_on_the_members_line(umbrella, capsys):
    toml = umbrella / "domain.toml"
    toml.write_text(toml.read_text().replace(
        'members = ["domains/*"]', 'members = ["domains/a"]  # just a'))
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "domains/c", "--prefix", "Z")
    assert code == 0, err
    assert 'members = ["domains/a", "domains/c"]  # just a' in toml.read_text()


def test_init_member_refuses_a_multiline_members_list(umbrella, capsys):
    toml = umbrella / "domain.toml"
    toml.write_text(toml.read_text().replace(
        'members = ["domains/*"]', 'members = [\n  "domains/a",\n]'))
    before = toml.read_bytes()
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "labs/x", "--prefix", "Z")
    assert code == 2
    assert "labs/x" in err and "by hand" in err
    assert toml.read_bytes() == before
    assert not (umbrella / "labs").exists()


def test_init_member_refuses_a_used_prefix(umbrella, capsys):
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "domains/c", "--prefix", "A")
    assert code == 2
    assert "domains/a" in err and "'A'" in err
    assert not (umbrella / "domains" / "c").exists()


@pytest.mark.parametrize("rel", ["/abs/x", "../out", "domains/../../out"])
def test_init_member_refuses_a_path_outside_the_umbrella(umbrella, capsys, rel):
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", rel, "--prefix", "Z")
    assert code == 2
    assert "umbrella" in err


@pytest.mark.parametrize("prefix", ["a", "C1", "", "A-B", "A\n"])
def test_init_member_refuses_a_bad_prefix(umbrella, capsys, prefix):
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "domains/c", "--prefix", prefix)
    assert code == 2
    assert "prefix" in err


def test_init_member_refuses_an_existing_domain(umbrella, capsys):
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "domains/a", "--prefix", "Z")
    assert code == 2
    assert "already exists" in err


def test_init_member_inherits(umbrella, capsys):
    _ar(capsys, "--domain", str(umbrella), "init",
        "--member", "domains/c", "--prefix", "Z")
    capsys.readouterr()
    code, out, _ = _ar(capsys, "--domain", str(umbrella / "domains" / "c"),
                       "domains", "show", "domains/c", "--json")
    assert code == 0
    rows = {(r["table"], r["key"]): r for r in json.loads(out)["rows"]}
    assert rows[("budgets", "domain_max_runs")]["source"] == "umbrella"


def test_init_requires_exactly_one_of_path_and_member(umbrella, tmp_path, capsys):
    for argv in (["init"],
                 ["init", str(tmp_path / "p"), "--member", "domains/c",
                  "--prefix", "Z"],
                 ["init", "--member", "domains/c"],
                 ["init", str(tmp_path / "p"), "--prefix", "Z"]):
        code, _, err = _ar(capsys, "--domain", str(umbrella), *argv)
        assert code == 2, argv


def test_init_without_member_is_unchanged(tmp_path, capsys):
    code, out, err = _ar(capsys, "init", str(tmp_path / "solo"))
    assert code == 0, err
    assert "scaffolded domain 'solo'" in out


@pytest.mark.parametrize("name", ['a"b', "a\\b", "a\nb"])
def test_init_member_refuses_a_name_that_would_break_the_toml(umbrella, capsys, name):
    code, _, err = _ar(capsys, "--domain", str(umbrella), "init",
                       "--member", "domains/c", "--prefix", "Z", "--name", name)
    assert code == 2
    assert "--name" in err
    assert not (umbrella / "domains" / "c").exists()
