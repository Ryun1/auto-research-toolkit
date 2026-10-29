"""`ar close` and the per-track terminal set.

Which statuses are terminal is per-track machine config — a defect track
closes `fixed`, not `confirmed` — and the first real domain filed a harness
defect with the research-track verb, watched the entry bounce back to the
queue, and only then learned the rule. A close that leaves the entry
actionable must say so at the moment it happens.
"""
from autoresearch import cli
from conftest import make_entry, memo


def _claim_and_close(sandbox, entry_id, status, *extra):
    root = ["--domain", str(sandbox.paths.root), "--session", "s"]
    assert cli.main([*root, "claim", entry_id, "--why", "taking it"]) == 0
    capsys_code = cli.main([*root, "close", entry_id, status, *extra])
    return capsys_code


def test_a_non_terminal_close_says_where_the_entry_goes(sandbox, store, capsys):
    """`blocked` is a legal close target that is not terminal. It does not
    return to the queue -- `ar rank` reads only the initial status -- so the
    warning must say it waits until `--reopen` (#28)."""
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "blocked",
                            "--why", "waiting on a quiet host")
    assert code == 0
    out = capsys.readouterr().out
    assert "'blocked' is not terminal on track 'research'" in out, out
    assert "returns to the queue" not in out
    assert "Q1 stays blocked, off the queue, until `ar close Q1 --reopen`" in out
    assert "terminal here: confirmed, refuted" in out
    assert "evidence:" not in out, "no memo was given, so none is named"


def test_a_blocked_close_keeps_the_evidence_it_was_given(sandbox, store, capsys):
    """#28: --memo/--summary/--reopen-condition/--evidence-class were built
    into a Result and dropped on a non-terminal close, while the command
    printed the memo path as if it had been recorded."""
    memo(sandbox, "inbox/m.md")
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "blocked", "--why", "w",
                            "--memo", "inbox/m.md", "--summary", "real evidence",
                            "--reopen-condition", "a quiet host",
                            "--evidence-class", "ledger")
    assert code == 0
    assert "evidence: inbox/m.md" in capsys.readouterr().out
    entry = store.load("Q1")
    assert entry.status == "blocked" and entry.result is None
    event = next(e for e in entry.history if e.kind == "evidence")
    assert event.snapshot == {"verdict": "blocked", "memo": "inbox/m.md",
                              "summary": "real evidence",
                              "reopen_condition": "a quiet host",
                              "evidence_class": "ledger"}
    assert "real evidence" in event.detail


def test_a_blocked_close_refuses_a_memo_that_does_not_exist(sandbox, store, capsys):
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "blocked", "--why", "w",
                            "--memo", "inbox/missing.md")
    assert code != 0
    assert store.load("Q1").status == "in-progress"


def test_a_blocked_close_refuses_a_closure_kind(sandbox, store, capsys):
    """A closure kind says how far a verdict reaches; a non-terminal close has
    no verdict to label, so the flag is refused rather than dropped."""
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "blocked", "--why", "w",
                            "--closure", "cell")
    assert code != 0
    assert "--closure" in capsys.readouterr().err
    assert store.load("Q1").status == "in-progress"


def test_a_terminal_close_prints_no_warning(sandbox, store, capsys):
    memo(sandbox, "inbox/Q1.md")
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "confirmed", "--memo", "inbox/Q1.md",
                            "--summary", "it held")
    assert code == 0
    assert "not terminal" not in capsys.readouterr().out


def test_a_close_leaves_the_views_valid_so_validate_passes(sandbox, capsys):
    """The queue view carries status and claim state; a close that left it
    stale made every hand-driven mutation eat a failed `ar validate` before
    the next read. Mutating verbs re-render, so validate follows close with
    no manual `ar render` in between."""
    root = ["--domain", str(sandbox.paths.root), "--session", "s"]
    assert cli.main([*root, "entry", "new", "Q1 title",
                     "--track", "research", "--id", "Q1"]) == 0
    assert cli.main([*root, "claim", "Q1", "--why", "taking it"]) == 0
    assert cli.main([*root, "close", "Q1", "confirmed",
                     "--memo", memo(sandbox, "inbox/Q1.md"),
                     "--summary", "it held"]) == 0
    assert cli.main([*root, "validate"]) == 0
    assert "problem" not in capsys.readouterr().out


def test_a_census_close_bypasses_the_replication_gate(sandbox, store, capsys):
    """The replication gate demands ledger rows of a `confirmed` close; a
    census or official closure is deliberately measurement-free or carries
    evaluator-owned numbers, so the same close with a declared class goes
    through where the ledger default is refused (pvfast-stwo-simd H10)."""
    # cmd_close loads its own config from disk: the gate must be a real
    # config value, not a test-only attribute mutation.
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        "[coordinator]", "[coordinator]\nconfirm_runs = 2"))
    memo(sandbox, "inbox/Q1.md")
    make_entry(store, "Q1")
    code = _claim_and_close(sandbox, "Q1", "confirmed", "--memo", "inbox/Q1.md",
                            "--summary", "it held")
    assert code == 2
    assert "valid run row" in capsys.readouterr().err

    make_entry(store, "Q2")
    code = _claim_and_close(sandbox, "Q2", "confirmed", "--memo", "inbox/Q1.md",
                            "--summary", "a census of the corpus",
                            "--evidence-class", "census")
    assert code == 0
    assert store.load("Q2").result.evidence_class == "census"


def test_a_terminal_close_without_a_summary_is_refused(sandbox, store, capsys):
    """sig.golf H18: the summary is the record's own typed sentence about the
    verdict -- the closed row renders it, every brief carries it, and the
    fabrication net reads its numbers. Without one, a closure that states
    nothing passes every check there is."""
    memo(sandbox, "inbox/Q1.md")
    make_entry(store, "Q1")
    root = ["--domain", str(sandbox.paths.root), "--session", "s"]
    assert cli.main([*root, "claim", "Q1", "--why", "taking it"]) == 0
    assert cli.main([*root, "close", "Q1", "confirmed",
                     "--memo", "inbox/Q1.md"]) == 2
    err = capsys.readouterr().err
    assert "--summary" in err and "states what it decided" in err
    assert "ar entry amend" in err          # the remedy for one that exists
    assert store.load("Q1").result is None  # nothing was written
    assert cli.main([*root, "close", "Q1", "confirmed", "--memo", "inbox/Q1.md",
                     "--summary", "measured 3.1% below the record"]) == 0
    assert store.load("Q1").result.summary == "measured 3.1% below the record"


def test_a_mechanism_closure_names_the_entries_it_hides_from_rank(
        sandbox, store, capsys):
    """#13: a mechanism refutation silently removed sibling entries from
    `ar rank`; curators found it by hand and relabelled to `cell`."""
    make_entry(store, "Q1", mechanisms=["port"])
    make_entry(store, "Q2", mechanisms=["port"])
    make_entry(store, "Q3", mechanisms=["other"])
    code = _claim_and_close(sandbox, "Q1", "refuted",
                            "--closure", "mechanism", "--summary", "no gain",
                            "--memo", memo(sandbox))
    assert code == 0
    out = capsys.readouterr().out
    assert "now excluded from `ar rank` by this mechanism closure: Q2 " in out, out
    assert "Q3" not in out


def _token(capsys):
    out = capsys.readouterr().out
    return next(line.split()[1] for line in out.splitlines()
                if line.startswith("token "))


def _close_as(sandbox, session, entry_id, *extra):
    memo(sandbox, f"inbox/{entry_id}.md")
    return cli.main(["--domain", str(sandbox.paths.root), "--session", session,
                     "close", entry_id, "refuted", "--memo",
                     f"inbox/{entry_id}.md", "--closure", "cell",
                     "--reopen-condition", "r", "--summary", "s", *extra])


def test_another_sessions_live_claim_refuses_a_tokenless_close(
        sandbox, store, capsys):
    """starks H4: Q49 was closed refuted by a curator while another session
    held a live claim on it -- the race the claim lock exists to prevent."""
    make_entry(store, "Q1")
    root = ["--domain", str(sandbox.paths.root)]
    assert cli.main([*root, "--session", "holder", "claim", "Q1",
                     "--why", "mine"]) == 0
    capsys.readouterr()
    assert _close_as(sandbox, "curator", "Q1") == 2
    err = capsys.readouterr().err
    assert "held by 'holder'" in err and "--claim-token" in err
    assert store.load("Q1").status == "in-progress"


def test_the_holders_token_lets_another_session_close(sandbox, store, capsys):
    """The orchestrator closes what its subagent claimed, given the token."""
    make_entry(store, "Q1")
    root = ["--domain", str(sandbox.paths.root)]
    assert cli.main([*root, "--session", "holder", "claim", "Q1",
                     "--why", "mine"]) == 0
    token = _token(capsys)
    assert _close_as(sandbox, "curator", "Q1", "--claim-token", token) == 0
    assert store.load("Q1").status == "refuted"


def test_a_wrong_token_refuses_even_under_the_holders_name(
        sandbox, store, capsys):
    make_entry(store, "Q1")
    root = ["--domain", str(sandbox.paths.root)]
    assert cli.main([*root, "--session", "holder", "claim", "Q1",
                     "--why", "mine"]) == 0
    capsys.readouterr()
    assert _close_as(sandbox, "holder", "Q1", "--claim-token", "0" * 32) == 2
    assert store.load("Q1").status == "in-progress"
