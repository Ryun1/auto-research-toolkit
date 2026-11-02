"""`--all-domains` on board, rank, budget and validate (#66).

One section per domain, umbrella first, and each section's body is exactly
what the verb prints for that domain alone: a curator pass reads one screen
instead of five, and nothing in it differs from the per-domain reads it
replaces."""
import json

import pytest

from autoresearch import cli
from autoresearch.entries import Store
from conftest import make_entry, member_domain

VERBS = ["board", "rank", "budget", "validate"]
PATHS = [".", "domains/a", "domains/b"]


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _roots(umbrella):
    return [umbrella / p if p != "." else umbrella for p in PATHS]


def _corrupt(root, entry_id):
    store = Store(root / "state" / "entries")
    make_entry(store, entry_id, track="research")
    path = store.path(entry_id)
    path.write_text(path.read_text() + "<<<<<<< ours\nx: 1\n=======\nx: 2\n>>>>>>> theirs\n")


@pytest.mark.parametrize("verb", VERBS)
def test_verb_all_domains_contains_each_single_domain_output(umbrella, capsys, verb):
    singles = []
    for root in _roots(umbrella):
        code, out, _ = _run(capsys, "--domain", str(root), verb)
        assert code == 0, out
        singles.append(out)
    code, out, _ = _run(capsys, "--domain", str(umbrella), verb, "--all-domains")
    assert code == 0, out
    at = 0
    for path, name, single in zip(PATHS, ["umb", "a", "b"], singles, strict=True):
        header = f"== {path} ({name}) ==\n## Goal: beat-toy-target\n"
        found = out.index(header + single, at)
        at = found + len(header + single)
    # Sections are separated by exactly one blank line, and nothing trails.
    assert "\n\n\n" not in out
    assert out.endswith(singles[-1])


@pytest.mark.parametrize("verb", VERBS)
def test_verb_all_domains_from_a_member_is_the_whole_workspace(umbrella, capsys, verb):
    code, out, _ = _run(capsys, "--domain", str(umbrella / "domains" / "b"),
                        verb, "--all-domains")
    assert code == 0
    assert [ln for ln in out.splitlines() if ln.startswith("== ")] == [
        "== . (umb) ==", "== domains/a (a) ==", "== domains/b (b) =="]


@pytest.mark.parametrize("verb", VERBS)
def test_verb_all_domains_json_has_one_key_per_domain(umbrella, capsys, verb):
    code, out, _ = _run(capsys, "--domain", str(umbrella), verb,
                        "--all-domains", "--json")
    assert code == 0
    doc = json.loads(out)
    assert list(doc) == ["domains"]
    assert list(doc["domains"]) == PATHS
    _, single, _ = _run(capsys, "--domain", str(umbrella / "domains" / "a"),
                        verb, "--json")
    assert doc["domains"]["domains/a"] == json.loads(single)


def test_validate_all_domains_fails_when_one_member_fails(umbrella, capsys):
    code, out, _ = _run(capsys, "--domain", str(umbrella), "validate", "--all-domains")
    assert code == 0
    assert "failing:" not in out
    _corrupt(umbrella / "domains" / "b", "C1")
    code, out, _ = _run(capsys, "--domain", str(umbrella), "validate", "--all-domains")
    assert code == 1
    assert out.splitlines()[-1] == "failing: domains/b"
    # The failing member's own report is still there in full.
    assert "C1.yaml" in out


def test_validate_all_domains_names_every_failure_in_workspace_order(umbrella, capsys):
    _corrupt(umbrella / "domains" / "b", "C1")
    _corrupt(umbrella / "domains" / "a", "A1")
    code, out, _ = _run(capsys, "--domain", str(umbrella), "validate", "--all-domains")
    assert code == 1
    assert out.splitlines()[-1] == "failing: domains/a, domains/b"
    code, out, _ = _run(capsys, "--domain", str(umbrella), "validate",
                        "--all-domains", "--json")
    assert code == 1
    doc = json.loads(out)
    assert [p for p, d in doc["domains"].items() if not d["ok"]] == [
        "domains/a", "domains/b"]


@pytest.mark.parametrize("verb", VERBS)
def test_all_domains_outside_a_workspace_is_the_single_output(sandbox, capsys, verb):
    root = str(sandbox.paths.root)
    code, single, single_err = _run(capsys, "--domain", root, verb)
    all_code, out, err = _run(capsys, "--domain", root, verb, "--all-domains")
    assert (all_code, out) == (code, single)
    note = (f"ar: --all-domains: {sandbox.paths.root} has no member domains; "
            "showing this domain only\n")
    assert err.count(note) == 1
    assert err.replace(note, "") == single_err


def test_rank_all_still_means_full(umbrella, capsys):
    """`rank --all` keeps its meaning (the full ranking of this domain); the
    workspace walk is `--all-domains` and only that."""
    root = umbrella / "domains" / "a"
    make_entry(Store(root / "state" / "entries"), "A1", track="research")
    code, full, err = _run(capsys, "--domain", str(root), "rank", "--all")
    assert code == 0 and err == ""
    assert "== " not in full and "A1" in full
    code, walk, _ = _run(capsys, "--domain", str(root), "rank", "--all",
                         "--all-domains")
    assert code == 0
    assert "== domains/a (a) ==\n## Goal: beat-toy-target\n" + full in walk


def test_a_failing_member_section_does_not_hide_the_others(umbrella, capsys):
    """Review Focus 5: a member whose probe fails still gets its section, and
    the sections after it still print."""
    probe = umbrella / "domains" / "a" / "bin" / "probe-target"
    probe.write_text("#!/bin/sh\necho 'probe broke' >&2\nexit 3\n")
    code, out, err = _run(capsys, "--domain", str(umbrella), "budget", "--all-domains")
    assert code == 0
    assert [ln for ln in out.splitlines() if ln.startswith("== ")] == [
        "== . (umb) ==", "== domains/a (a) ==", "== domains/b (b) =="]
    code, out, err = _run(capsys, "--domain", str(umbrella), "board", "--all-domains")
    assert code == 0
    # The board's stderr note goes to stderr, once, for the member it names.
    assert err.count("(target unavailable: ") == 1 and "probe broke" in err
    assert "== domains/b (b) ==" in out


def test_a_member_that_refuses_is_an_ar_line_in_its_section(umbrella, capsys):
    """A member whose verb raises gets `ar: <error>` as its body and exit 2;
    the other members still print, and JSON carries the error under its key."""
    _corrupt(umbrella / "domains" / "a", "A1")
    code, out, _ = _run(capsys, "--domain", str(umbrella), "budget", "--all-domains")
    assert code == 2
    section = out.split("== domains/a (a) ==\n", 1)[1].split("\n== ", 1)[0]
    lines = section.splitlines()
    assert lines[0] == "## Goal: beat-toy-target"
    assert lines[1].startswith("ar: ") and "A1.yaml" in lines[1]
    assert "== domains/b (b) ==" in out and "stop decision" in out.split("(b) ==")[1]
    code, out, _ = _run(capsys, "--domain", str(umbrella), "budget",
                        "--all-domains", "--json")
    assert code == 2
    doc = json.loads(out)["domains"]
    assert list(doc["domains/a"]) == ["error"] and "A1.yaml" in doc["domains/a"]["error"]
    assert "meters" in doc["domains/b"]


def test_a_broken_workspace_refuses_rather_than_showing_one_domain(umbrella, capsys):
    """With --all-domains a broken umbrella is the answer, not a quiet fall
    back to this domain alone."""
    member_domain(umbrella / "domains" / "c", ("A", "E"), name="c")
    code, out, err = _run(capsys, "--domain", str(umbrella / "domains" / "b"),
                          "board", "--all-domains")
    assert code == 2 and out == ""
    assert err.startswith("ar: ") and "prefix 'A'" in err


# -- reap (#66) ----------------------------------------------------------------

def _stale(root, entry_id, session="gone", hours=10):
    """A claim on `entry_id` in the domain at `root`, silent for `hours`."""
    import datetime as dt

    from autoresearch.claims import Claims
    from autoresearch.config import DomainConfig
    config = DomainConfig.load(root)
    store = Store(config.paths.entries)
    make_entry(store, entry_id, track="research")
    entry = Claims(store, config, session).claim(entry_id, why="t")
    then = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).isoformat()
    entry.claim.at = then
    for event in entry.history:
        event.at = then
    store.save(entry)
    return store


def test_reap_all_domains_lists_each_domain(umbrella, capsys):
    _stale(umbrella / "domains" / "a", "A1")
    _stale(umbrella / "domains" / "b", "C1", session="other")
    code, out, _ = _run(capsys, "--domain", str(umbrella), "reap", "--all-domains")
    assert code == 0, out
    assert [ln for ln in out.splitlines() if ln.startswith("== ")] == [
        "== . (umb) ==", "== domains/a (a) ==", "== domains/b (b) =="]
    assert "0 reapable record(s)" in out.split("== domains/a")[0]
    section_a = out.split("== domains/a (a) ==")[1].split("== domains/b")[0]
    assert "1 reapable record(s)" in section_a and "A1" in section_a
    assert "C1" in out.split("== domains/b (b) ==")[1]


def test_reap_id_in_another_member(umbrella, capsys):
    store = _stale(umbrella / "domains" / "b", "C1")
    from autoresearch.config import DomainConfig
    cli._render_views(DomainConfig.load(umbrella / "domains" / "b"))
    stale_views = lambda: [f for f in (umbrella / "domains" / "b").rglob("*.md")
                           if "gone" in f.read_text()]
    assert stale_views(), "fixture: the claim should show in a rendered view"
    code, out, _ = _run(capsys, "--domain", str(umbrella / "domains" / "a"),
                        "reap", "C1")
    assert code == 0, out
    assert out.startswith("C1 reaped from gone (silent ")
    assert out.rstrip().endswith("h) (domains/b)")
    assert store.load("C1").claim is None
    # The owner's views were re-rendered, not the caller's.
    assert stale_views() == []


def test_reap_id_in_the_local_domain_is_unchanged(umbrella, capsys):
    _stale(umbrella / "domains" / "a", "A1")
    code, out, _ = _run(capsys, "--domain", str(umbrella / "domains" / "a"),
                        "reap", "A1")
    assert code == 0 and "(domains/" not in out
    assert out.startswith("A1 reaped from gone (silent ")


def test_reap_id_with_all_domains_is_refused(umbrella, capsys):
    _stale(umbrella / "domains" / "b", "C1")
    code, out, err = _run(capsys, "--domain", str(umbrella), "reap", "C1",
                          "--all-domains")
    assert code != 0
    assert ("--all-domains lists reapable claims; reap one id without it"
            in err + out)
    assert Store(umbrella / "domains" / "b" / "state" / "entries").load("C1").claim


def test_reap_outside_a_workspace_is_unchanged(sandbox, capsys):
    code, out, _ = _run(capsys, "--domain", str(sandbox.paths.root), "reap")
    assert code == 0 and out == "0 reapable record(s)\n"
