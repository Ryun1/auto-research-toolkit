"""`ar rank` — the human's view of the ranking, including the explore reserve."""
from autoresearch import cli
from conftest import make_entry


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


def test_a_bad_explore_flag_is_refused_rather_than_traced(sandbox, store, capsys):
    """H135's shape at the CLI: `ar` catches AutoresearchError and prints a
    refusal; anything else reaches the user as a traceback."""
    _queue(store)
    assert cli.main(["--domain", str(sandbox.paths.root), "rank",
                     "--explore", "1.5"]) == 2   # `ar`'s refusal code
    assert "explore_fraction" in capsys.readouterr().err


def test_the_score_table_marks_the_reserve_too(sandbox, store, capsys):
    """The table is the ranking a human corrects. An explore pick sits low in it
    by design, and one shown unmarked reads as the formula having gone wrong."""
    _queue(store)
    cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"])
    table = capsys.readouterr().out.split("shortlist (top 3):")[0]
    marked = [ln for ln in table.splitlines() if "[explore]" in ln]
    assert len(marked) == 1 and "Q90" in marked[0], table


def test_rank_reconciles_retained_and_unrecorded_campaign_runs(sandbox, store, capsys):
    import json

    from autoresearch.runs import RunRecord, append

    config = sandbox.paths.root / "domain.toml"
    config.write_text(config.read_text().replace(
        "domain_max_runs        = 400", "domain_max_runs        = 5"))
    make_entry(store, "Q99", cost=4, impact=1)
    append(sandbox.paths.runs / "seed.jsonl", RunRecord(
        id="retained", session="worker", metrics={}, status="invalid"))
    sandbox.paths.iterations.mkdir(parents=True, exist_ok=True)
    history = sandbox.paths.iterations / "0001.json"
    history.write_text(json.dumps({"runs": 3}))
    assert cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "1"]) == 0
    assert "Q99" not in capsys.readouterr().out.split("shortlist (top 1):")[1]
    history.write_text(json.dumps({"runs": 1, "run_ids": ["retained"]}))
    assert cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "1"]) == 0
    assert "Q99" in capsys.readouterr().out.split("shortlist (top 1):")[1]
