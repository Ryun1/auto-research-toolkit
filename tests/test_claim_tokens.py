"""#33: a claim is bound to a token, not to a session name anyone can type.

`require_owner` compared session names, so any session passing the holder's
name owned the claim, and a session that claimed under one name could not
act under another -- qsb had six entries frozen until their TTL ran out, all
held by `cli`. A claim now carries the hash of a token `ar claim` hands out;
presenting the token proves ownership under any name, and the refusal says
which of three things is wrong.
"""
import datetime as dt

import pytest

from autoresearch import cli
from autoresearch.claims import Claims
from autoresearch.errors import ClaimError
from conftest import make_entry


def _ar(sandbox, *args):
    return cli.main(["--domain", str(sandbox.paths.root), *args])


def _token(capsys):
    out = capsys.readouterr().out
    return next(line.split()[1] for line in out.splitlines()
                if line.startswith("token "))


def test_ar_claim_hands_out_a_token_and_stores_only_its_hash(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    token = _token(capsys)
    claim = store.load("Q1").claim
    assert len(token) >= 32 and claim.token_sha
    assert token not in store.path("Q1").read_text()


def test_the_name_alone_no_longer_owns_a_tokened_claim(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    capsys.readouterr()
    assert _ar(sandbox, "--session", "a", "release", "Q1", "--why", "x") == 2
    err = capsys.readouterr().err
    assert "bound to a token" in err and "--claim-token" in err


def test_the_token_owns_the_claim_under_any_name(sandbox, store, capsys):
    """qsb's six frozen entries: claimed as `cli`, acted on as someone else."""
    make_entry(store, "Q1")
    assert _ar(sandbox, "claim", "Q1", "--why", "t") == 0          # as `cli`
    token = _token(capsys)
    assert _ar(sandbox, "--session", "scout-3", "--claim-token", token,
               "release", "Q1", "--why", "done") == 0
    assert store.load("Q1").claim is None


def test_the_token_may_come_from_the_environment(sandbox, store, capsys, monkeypatch):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    monkeypatch.setenv("AR_CLAIM_TOKEN", _token(capsys))
    assert _ar(sandbox, "--session", "a", "release", "Q1", "--why", "x") == 0


def test_a_wrong_token_is_refused(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    capsys.readouterr()
    assert _ar(sandbox, "--session", "a", "--claim-token", "0" * 32,
               "release", "Q1", "--why", "x") == 2
    assert "does not match" in capsys.readouterr().err


def test_another_live_session_is_named_with_its_remedy(sandbox, store):
    make_entry(store, "Q1")
    Claims(store, sandbox, "a").claim("Q1", why="t")
    with pytest.raises(ClaimError) as exc:
        Claims(store, sandbox, "b").release("Q1", why="x")
    message = str(exc.value)
    assert "held by 'a'" in message and "inside" in message
    assert "If you are 'a' under another name" in message


def test_a_gone_holder_is_named_with_reap(sandbox, store):
    make_entry(store, "Q1")
    entry = Claims(store, sandbox, "a").claim("Q1", why="t")
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=100)
    entry.claim.at = old.isoformat()
    store.save(entry)
    with pytest.raises(ClaimError, match="ar reap Q1"):
        Claims(store, sandbox, "b").release("Q1", why="x")


def test_the_issuing_process_keeps_its_own_token(sandbox, store):
    """The coordinator claims and releases in one process; it never has to
    be handed the token it was given."""
    make_entry(store, "Q1")
    claims = Claims(store, sandbox, "coordinator")
    claims.claim("Q1", why="t")
    claims.release("Q1", why="done")
    assert store.load("Q1").claim is None


def test_a_pre_token_claim_keeps_name_ownership(sandbox, store):
    from autoresearch.entries import Claim
    make_entry(store, "Q1", status="in-progress",
               claim=Claim(session="a", at=dt.datetime.now(dt.UTC).isoformat()))
    Claims(store, sandbox, "a").release("Q1", why="legacy")
    assert store.load("Q1").claim is None


def test_a_domain_may_keep_name_ownership(sandbox, store, capsys):
    toml = sandbox.paths.root / "domain.toml"
    toml.write_text(toml.read_text().replace(
        "[coordinator]\n", "[coordinator]\nclaim_tokens = false\n"))
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    assert _ar(sandbox, "--session", "a", "release", "Q1", "--why", "x") == 0


def test_gates_updates_honour_the_token(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    token = _token(capsys)
    assert _ar(sandbox, "--session", "a", "gates", "configure", "Q1",
               "--why", "x") == 2
    assert _ar(sandbox, "--session", "a", "--claim-token", token, "gates",
               "configure", "Q1", "--why", "x") == 0


# -- several claims, several tokens (#44) ------------------------------------

def test_one_session_holds_two_claims_with_both_tokens_exported(
        sandbox, store, capsys, monkeypatch):
    make_entry(store, "Q1")
    make_entry(store, "Q2")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    first = _token(capsys)
    assert _ar(sandbox, "--session", "a", "claim", "Q2", "--why", "t") == 0
    second = _token(capsys)
    monkeypatch.setenv("AR_CLAIM_TOKEN", f"{first},{second}")
    assert _ar(sandbox, "--session", "a", "release", "Q2", "--why", "x") == 0
    assert _ar(sandbox, "--session", "a", "release", "Q1", "--why", "x") == 0


def test_the_claim_output_shows_how_to_add_the_token(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    out = capsys.readouterr().out
    assert 'AR_CLAIM_TOKEN="${AR_CLAIM_TOKEN:+$AR_CLAIM_TOKEN,}' in out


def test_no_matching_token_names_the_entry_and_the_count(sandbox, store, capsys):
    make_entry(store, "Q1")
    assert _ar(sandbox, "--session", "a", "claim", "Q1", "--why", "t") == 0
    capsys.readouterr()
    assert _ar(sandbox, "--session", "a", "--claim-token", "0" * 32 + "," + "1" * 32,
               "release", "Q1", "--why", "x") == 2
    err = capsys.readouterr().err
    assert "none of the 2 claim tokens presented" in err and "Q1" in err
