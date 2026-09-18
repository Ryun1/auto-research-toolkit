"""`ar rank` — the human's view of the ranking, including the risk dial."""
from autoresearch import cli
from conftest import make_entry


def _queue(store):
    for i in range(4):                   # cheap, likely, small roots
        make_entry(store, f"Q1{i}", confidence=0.85, impact=0.02, cost=1.0)
    make_entry(store, "Q90", confidence=0.10, impact=0.40, cost=8.0)


def _with_branch(store):
    """A branch off Q90: the incumbent partition, priced worse than the roots."""
    _queue(store)
    make_entry(store, "Q95", parent="Q90", confidence=0.10, impact=0.20, cost=8.0)


def _no_seam(sandbox):
    """Strip the toy domain's score seam so the tests below exercise the
    formula path; the seam itself has its own tests."""
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text("".join(
        ln for ln in toml.read_text().splitlines(keepends=True)
        if '"bin/score"' not in ln))


def test_rank_splits_the_shortlist_by_the_dial(sandbox, store, capsys):
    _no_seam(sandbox)
    _with_branch(store)
    assert cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"]) == 0
    out = capsys.readouterr().out
    shortlist = out.split("shortlist (top 3):")[1]
    # 50/50 of 3 rounds to 2 novel slots (the best roots) + 1 incumbent slot.
    assert "Q10" in shortlist and "Q11" in shortlist and "Q95" in shortlist
    assert "Q90" not in shortlist
    assert "risk 50%" in out


def test_rank_risk_zero_shows_the_incumbent_first(sandbox, store, capsys):
    _no_seam(sandbox)
    _with_branch(store)
    assert cli.main(["--domain", str(sandbox.paths.root), "rank",
                     "--top", "3", "--risk", "0"]) == 0
    out = capsys.readouterr().out
    shortlist = out.split("shortlist (top 3):")[1]
    assert "Q95" in shortlist
    assert "Q90" not in shortlist       # a root, backfilled only if slots remain
    assert "risk" not in out


def test_rank_marks_which_shortlist_entries_are_novel(sandbox, store, capsys):
    _no_seam(sandbox)
    _with_branch(store)
    cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"])
    lines = capsys.readouterr().out.split("shortlist (top 3):")[1].strip().splitlines()
    marked = [ln for ln in lines if "[novel]" in ln]
    assert len(marked) == 2, lines


def test_rank_reads_the_dial_from_the_domain(sandbox, store, capsys):
    _no_seam(sandbox)
    _queue(store)
    (sandbox.paths.root / "domain.toml").write_text(
        (sandbox.paths.root / "domain.toml").read_text().replace(
            "risk = 0.5", "risk = 0.9"))
    assert cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"]) == 0
    out = capsys.readouterr().out
    assert "risk 90%" in out


def test_the_risk_flag_overrides_the_domain(sandbox, store, capsys):
    _no_seam(sandbox)
    _queue(store)
    cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3",
              "--risk", "0.9"])
    assert "risk 90%" in capsys.readouterr().out


def test_a_bad_risk_flag_is_refused_rather_than_traced(sandbox, store, capsys):
    """H135's shape at the CLI: `ar` catches AutoresearchError and prints a
    refusal; anything else reaches the user as a traceback."""
    _no_seam(sandbox)
    _queue(store)
    assert cli.main(["--domain", str(sandbox.paths.root), "rank",
                     "--risk", "1.5"]) == 2   # `ar`'s refusal code
    assert "risk" in capsys.readouterr().err


def test_the_score_table_marks_novel_branches_too(sandbox, store, capsys):
    """The table is the ranking a human corrects. A novel pick may sit low in
    it by design, and one shown unmarked reads as the formula having gone
    wrong."""
    _no_seam(sandbox)
    _with_branch(store)
    cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "3"])
    out = capsys.readouterr().out
    table, shortlist = out.split("shortlist (top 3):")
    # The table partitions every scored entry; the shortlist shows how the
    # dial spent its slots.
    assert len([ln for ln in table.splitlines() if "[novel]" in ln]) == 5
    assert "[branch]" in table
    assert len([ln for ln in shortlist.splitlines() if "[novel]" in ln]) == 2
    assert "[branch]" in shortlist


def test_rank_prices_through_the_domain_seam(sandbox, store, capsys):
    """The toy domain ships `score = "bin/score"`; the human-correction
    surface shows what the loop would dispatch, which is the domain's prices,
    with the scorer's reasons legible in the table."""
    _queue(store)
    assert cli.main(["--domain", str(sandbox.paths.root), "rank", "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "priced by domain seam: bin/score" in out
    assert "domain=novel root" in out


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
