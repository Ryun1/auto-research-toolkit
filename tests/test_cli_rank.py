"""`ar rank` — the human's view of the ranking, including the explore reserve."""
from conftest import make_entry

from autoresearch import cli


def _queue(store):
    for i in range(4):                   # cheap, likely, small
        make_entry(store, f"Q1{i}", confidence=0.85, impact=0.02, cost=1.0)
    make_entry(store, "Q90", confidence=0.10, impact=0.40, cost=8.0)


def test_rank_reserves_shortlist_slots_for_amplitude(sandbox, store, capsys):
    _queue(store)
    assert cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"]) == 0
    out = capsys.readouterr().out
    shortlist = out.split("shortlist (top 3):")[1]
    assert "Q90" in shortlist
    assert "explore reserve: 20%" in out


def test_rank_explore_zero_shows_the_score_alone(sandbox, store, capsys):
    _queue(store)
    assert cli.main(["--domain", str(sandbox.paths.root), "rank",
                     "--top", "3", "--explore", "0"]) == 0
    out = capsys.readouterr().out
    assert "Q90" not in out.split("shortlist (top 3):")[1]
    assert "explore reserve" not in out


def test_rank_marks_which_shortlist_entries_the_reserve_took(sandbox, store, capsys):
    _queue(store)
    cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"])
    lines = capsys.readouterr().out.split("shortlist (top 3):")[1].strip().splitlines()
    marked = [ln for ln in lines if "[explore]" in ln]
    assert len(marked) == 1 and "Q90" in marked[0], lines


def test_rank_reads_the_reserve_from_the_domain(sandbox, store, capsys):
    _queue(store)
    (sandbox.paths.root / "domain.toml").write_text(
        (sandbox.paths.root / "domain.toml").read_text().replace(
            "explore_fraction = 0.2", "explore_fraction = 0.5"))
    assert cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"]) == 0
    out = capsys.readouterr().out
    assert "explore reserve: 50%" in out


def test_the_explore_flag_overrides_the_domain(sandbox, store, capsys):
    _queue(store)
    cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3",
              "--explore", "0.5"])
    assert "explore reserve: 50%" in capsys.readouterr().out
