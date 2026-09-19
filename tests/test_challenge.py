"""The challenge intel surface.

The adapters are parsers of other people's output, so the tests pin the
contract both ways: a published snapshot parses to the normalized shape, and
the cached round-trip loses nothing the target probe or the briefs need. The
network is injectable everywhere -- a test that needed the internet would be
a test of the internet -- and every refusal the verbs make gets a test that
sees it refuse.
"""
import json

import pytest

from autoresearch import challenge as challenge_mod
from autoresearch.errors import ChallengeError, ConfigError

PF_SNAPSHOT = {
    "schema_version": 3,
    "truth_label": "RESEARCH_ONLY_NOT_PROMOTED",
    "generated_at": "1970-01-01T00:00:00Z",
    "attempts": [],
    "performance_summary": {"best_measured": None},
    "profile": None,
    "campaign": {
        "campaign_id": "stwo-simd-kernel-linux-x86-v1",
        "digest": "00f5",
        "title": "Stwo SIMD FFT/IFFT Linux x86 discovery pilot",
        "challenge": {
            "allowed_paths": ["crates/stwo/src/prover/backend/simd/fft/ifft.rs"],
            "forbidden_paths": ["**/Cargo.toml", "scripts/**"],
            "selection_basis": "SIMD IFFT kernel with an evaluator-owned oracle bench",
            "summary": "Reduce SIMD circle FFT/IFFT kernel time without changing outputs",
        },
        "measurement_policy": {
            "minimum_speedup": 1.03,
            "statistic": "median",
            "pairs": 3,
            "warmup_runs": 1,
            "timeout_seconds": 1200,
            "max_relative_mad": 0.08,
            "environment": {"LOG_N_INSTANCES": "12", "RAYON_NUM_THREADS": "8"},
            "host_policy": {"cpu_model": "AMD EPYC-Milan Processor",
                            "logical_cpu_count": 16,
                            "memory_bytes": 65834340352},
        },
        "upstream": {"commit": "dd1787bc", "repository": "https://example/proving.git"},
    },
    "research_graph": {"nodes": [], "edges": [], "schema_version": 1,
                       "evidence_class": "NO_RESEARCH_GRAPH",
                       "truth_label": "RESEARCH_ONLY_NOT_PROMOTED"},
}

YUKON_PAGE = """
## Leaderboard
current record 741,852,708 verified candidates/s &middot; Pinning workload
Pinning record: 741.85M146.09M<b>741,852,708</b> candidates/s on the benchmark RTX 4090.
<div><a href="/solver/ercumentyildirim?challenge=qsb&amp;benchmark=pinning">ercumentyildirim</a>
Opus 5 741,852,708 verified candidates/s+52,006 candidates/s (+0.04%) Sep 19, 2026 at 4:08 PM UTC</div>
<div><a href="/solver/johnbpetersen?challenge=qsb&amp;benchmark=pinning">johnbpetersen</a>
GPT-5.6 Sol 741,800,702 verified candidates/s+747,396 candidates/s (+0.51%) Sep 19, 2026 at 7:33 AM UTC</div>
<div><a href="/solver/owizdom?challenge=qsb&amp;benchmark=pinning">owizdom</a>
Opus 5 740,390,516 verified candidates/s+1,210,292 candidates/s (+0.83%) Sep 19, 2026 at 5:58 AM UTC</div>
"""


# -- adapters ----------------------------------------------------------------

def test_provablyfast_parses_the_published_snapshot():
    snap = challenge_mod._from_provablyfast(
        "https://provably.fast/data/index.json", json.dumps(PF_SNAPSHOT))
    assert snap.source == "provablyfast"
    assert snap.challenge == "stwo-simd-kernel-linux-x86-v1"
    assert snap.truth_label == "RESEARCH_ONLY_NOT_PROMOTED"
    assert snap.bar["minimum_speedup"] == 1.03
    assert snap.bar["statistic"] == "median"
    # no qualifying result yet -> the published bar is the target
    assert snap.target_value() == 1.03
    assert "**/Cargo.toml" in snap.policy["forbidden_paths"]
    assert snap.attempts_count == 0
    assert snap.graph["evidence_class"] == "NO_RESEARCH_GRAPH"


def test_provablyfast_record_displaces_the_bar():
    data = json.loads(json.dumps(PF_SNAPSHOT))
    data["performance_summary"]["best_measured"] = 1.05
    snap = challenge_mod._from_provablyfast("u", json.dumps(data))
    # a standing record beats: the bar is the entry bar, not the record
    assert snap.target_value() == 1.05
    assert snap.record.unit == "speedup"


def test_yukon_parses_record_and_board():
    snap = challenge_mod._from_yukon("https://www.yukon.org/qsb", YUKON_PAGE)
    assert snap.source == "yukon"
    assert snap.challenge == "qsb"
    assert snap.record.value == 741_852_708
    assert snap.record.unit == "candidates/s"
    # rank 1 is the record holder: the header omits what the board carries
    assert snap.record.by == "ercumentyildirim"
    assert snap.record.at == "Sep 19, 2026 at 4:08 PM UTC"
    # the labelled "Pinning record:" form, not the header's "current record"
    assert snap.benchmark == "pinning"
    assert snap.target_value() == 741_852_708
    assert [r["solver"] for r in snap.board] == [
        "ercumentyildirim", "johnbpetersen", "owizdom"]
    assert snap.board[0]["rank"] == 1
    assert snap.board[0]["value"] == 741_852_708
    assert snap.board[1]["delta_pct"] == 0.51
    sat = snap.saturation()
    assert sat["n_solvers"] == 3
    expected = (741_852_708 - 740_390_516) / 740_390_516 * 100
    assert sat["top5_spread_pct"] == pytest.approx(expected, abs=1e-3)


def test_a_snapshot_with_neither_record_nor_bar_has_no_target():
    snap = challenge_mod._from_yukon("https://www.yukon.org/qsb", "<html></html>")
    assert snap.record is None
    assert snap.board == []
    assert snap.target_value() is None


# -- the cache ---------------------------------------------------------------

def test_snapshot_round_trip_is_lossless():
    snap = challenge_mod._from_yukon("https://www.yukon.org/qsb", YUKON_PAGE)
    restored = challenge_mod.Snapshot.from_dict(snap.to_dict())
    assert restored.target_value() == snap.target_value()
    assert restored.board == snap.board
    assert restored.saturation() == snap.saturation()


def test_snapshot_from_dict_refuses_unknown_fields():
    data = challenge_mod._from_yukon("u", YUKON_PAGE).to_dict()
    data["mystery"] = 1
    with pytest.raises(ChallengeError, match="unknown field"):
        challenge_mod.Snapshot.from_dict(data)


def test_snapshot_from_dict_refuses_a_foreign_schema():
    data = challenge_mod._from_yukon("u", YUKON_PAGE).to_dict()
    data["schema_version"] = 99
    with pytest.raises(ChallengeError, match="schema_version"):
        challenge_mod.Snapshot.from_dict(data)


# -- pull: fetch-if-stale ------------------------------------------------------

@pytest.fixture
def domain(sandbox):
    return sandbox


def _declare_challenge(sandbox, source="yukon", **over):
    toml = sandbox.paths.root / "domain.toml"
    text = toml.read_text()
    table = "\n[challenge]\n" + "\n".join(
        f'{k} = {json.dumps(v)}' for k, v in
        {"source": source, **over}.items()) + "\n"
    toml.write_text(text + table)
    from autoresearch.config import DomainConfig
    return DomainConfig.load(sandbox.paths.root)


def test_pull_inside_the_window_never_touches_the_network(domain):
    config = _declare_challenge(domain, refresh_seconds=3600)
    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        return YUKON_PAGE

    snap1, action1 = challenge_mod.pull(
        config.challenge.source, config.challenge.url or
        challenge_mod.DEFAULT_URLS["yukon"], "", 3600, 60,
        domain.paths.root, fetcher=fetcher)
    snap2, action2 = challenge_mod.pull(
        config.challenge.source, config.challenge.url or
        challenge_mod.DEFAULT_URLS["yukon"], "", 3600, 60,
        domain.paths.root, fetcher=fetcher)
    assert calls == ["https://www.yukon.org/qsb"], "second pull refetched"
    assert "fetched" in action1
    assert "cached" in action2
    assert snap1.target_value() == snap2.target_value()


def test_pull_refetches_when_stale(domain):
    import datetime as dt
    config = _declare_challenge(domain, refresh_seconds=1)
    challenge_mod.pull(config.challenge.source, "", "pinning", 1, 60,
                       domain.paths.root, fetcher=lambda u, t: YUKON_PAGE)
    cached = challenge_mod.load_snapshot(domain.paths.root)
    stale = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)).isoformat()
    cached.fetched_at = stale
    (domain.paths.root / "state/challenge/snapshot.json").write_text(
        json.dumps(cached.to_dict()))
    _, action = challenge_mod.pull(config.challenge.source, "", "pinning",
                                   1, 60, domain.paths.root,
                                   fetcher=lambda u, t: YUKON_PAGE)
    assert action == "fetched"


def test_pull_refetches_when_the_source_changed(domain):
    _declare_challenge(domain, refresh_seconds=3600)
    challenge_mod.pull("yukon", "", "", 3600, 60, domain.paths.root,
                       fetcher=lambda u, t: YUKON_PAGE)
    _, action = challenge_mod.pull("provablyfast", "u", "", 3600, 60,
                                   domain.paths.root,
                                   fetcher=lambda u, t: json.dumps(PF_SNAPSHOT))
    assert action == "fetched"


def test_pull_refuses_an_unknown_source(domain):
    with pytest.raises(ChallengeError, match="not a known adapter"):
        challenge_mod.pull("nosuch", "", "", 1800, 60, domain.paths.root)


def test_pull_refuses_a_benchmark_the_page_does_not_serve(domain):
    """A declared request the page does not answer is refused, not silently
    answered with the wrong frontier's numbers."""
    with pytest.raises(ChallengeError, match="pinning.*subset|subset.*pinning"):
        challenge_mod.pull("yukon", "", "subset", 3600, 60, domain.paths.root,
                           fetcher=lambda u, t: YUKON_PAGE)


def test_snapshot_record_drift_is_a_challenge_error():
    snap = challenge_mod._from_yukon("u", YUKON_PAGE).to_dict()
    snap["record"]["mystery"] = 1
    with pytest.raises(ChallengeError, match="malformed"):
        challenge_mod.Snapshot.from_dict(snap)


# -- target: the probe contract -------------------------------------------------

def test_target_prints_the_record_and_survives_a_dead_site(domain):
    config = _declare_challenge(domain, refresh_seconds=3600)
    challenge_mod.pull(config.challenge.source, "", "", 3600, 60,
                       domain.paths.root, fetcher=lambda u, t: YUKON_PAGE)
    def dead_fetcher(url, timeout):
        raise ChallengeError("connection refused")
    # fresh cache -> no fetch attempted at all; and a dead site with a warm
    # cache still answers, because a leaderboard having a bad day must not
    # stop a loop that has a sane target
    value = challenge_mod.target_value(
        config.challenge.source, "", "", 3600, 60, domain.paths.root,
        fetcher=dead_fetcher)
    assert value == 741_852_708


def test_target_raises_without_a_cache(domain):
    _declare_challenge(domain, refresh_seconds=3600)
    with pytest.raises(ChallengeError):
        challenge_mod.target_value("yukon", "", "", 3600, 60,
                                   domain.paths.root,
                                   fetcher=lambda u, t: (_ for _ in ()).throw(
                                       ChallengeError("down")))


def test_target_refuses_a_snapshot_with_no_number(domain):
    config = _declare_challenge(domain, refresh_seconds=3600)
    challenge_mod.pull(config.challenge.source, "", "", 3600, 60,
                       domain.paths.root,
                       fetcher=lambda u, t: "<html></html>")
    with pytest.raises(ChallengeError, match="names no record"):
        challenge_mod.target_value(config.challenge.source, "", "", 3600, 60,
                                   domain.paths.root,
                                   fetcher=lambda u, t: "<html></html>")


# -- the briefs -----------------------------------------------------------------

def test_the_coordinator_gates_the_challenge_block_by_role(domain):
    """The block rides to the roles that route on it and to no others -- the
    same role-scoping the board fields obey, so a worker's payload does not
    grow with a challenge it cannot act on."""
    from autoresearch.driver.brain import Role, ScriptedBrain
    from autoresearch.driver.loop import Coordinator, Iteration
    _declare_challenge(domain)
    challenge_mod.pull("yukon", "", "", 3600, 60, domain.paths.root,
                       fetcher=lambda u, t: YUKON_PAGE)
    from autoresearch.config import DomainConfig
    config = DomainConfig.load(domain.paths.root)
    coordinator = Coordinator(config, ScriptedBrain({}))
    it = Iteration(n=coordinator._last_recorded_n() + 1)
    coordinator.orient(it)
    routed = {role: "challenge" in json.loads(coordinator._brief(role, it))
              for role in (Role.GENERATOR, Role.SCOUT, Role.JUDGE,
                           Role.WORKER, Role.CURATOR, Role.QC)}
    assert routed == {Role.GENERATOR: True, Role.SCOUT: True, Role.JUDGE: True,
                      Role.WORKER: False, Role.CURATOR: False, Role.QC: False}, routed



def test_brief_payload_is_none_without_a_snapshot(domain):
    _declare_challenge(domain)
    assert challenge_mod.brief_payload(domain.paths.root, "yukon") is None


def test_brief_payload_carries_the_record_and_saturation(domain):
    _declare_challenge(domain)
    challenge_mod.pull("yukon", "", "", 3600, 60, domain.paths.root,
                       fetcher=lambda u, t: YUKON_PAGE)
    payload = challenge_mod.brief_payload(domain.paths.root, "yukon")
    assert payload["source"] == "yukon"
    assert payload["record"]["value"] == 741_852_708
    assert payload["saturation"]["n_solvers"] == 3
    assert payload["board_top"][0]["solver"] == "ercumentyildirim"
    assert "saturation" in payload["note"].lower() or "marginal" in payload["note"]


def test_brief_payload_is_none_when_no_challenge_is_declared(domain):
    assert challenge_mod.brief_payload(domain.paths.root, "") is None


# -- the comparability check ------------------------------------------------------

class _FakeHost:
    chip = "Apple M5"
    cpu_threads = 14
    memory_gb = 61.3


def test_check_refuses_without_a_challenge(domain):
    with pytest.raises(ChallengeError, match="no \\[challenge\\]"):
        challenge_mod.check(domain)


def test_check_names_a_missing_snapshot(domain):
    config = _declare_challenge(domain)
    findings = challenge_mod.check(config, host=_FakeHost())
    assert findings == ["no snapshot cached; run `ar challenge pull` first"]


def test_check_flags_a_host_that_cannot_be_compared(domain):
    config = _declare_challenge(domain, source="provablyfast")
    challenge_mod.pull("provablyfast", "u", "", 3600, 60, domain.paths.root,
                       fetcher=lambda u, t: json.dumps(PF_SNAPSHOT))
    findings = challenge_mod.check(config, host=_FakeHost())
    assert any("AMD EPYC-Milan" in f and "Apple M5" in f for f in findings), findings
    assert any("logical CPUs" in f for f in findings), findings


def test_check_flags_forbidden_paths_the_domain_does_not_cover(domain):
    config = _declare_challenge(domain, source="provablyfast")
    challenge_mod.pull("provablyfast", "u", "", 3600, 60, domain.paths.root,
                       fetcher=lambda u, t: json.dumps(PF_SNAPSHOT))
    findings = challenge_mod.check(config, host=_FakeHost())
    assert any("Cargo.toml" in f for f in findings), findings
    assert any("scripts/" in f for f in findings), findings


def test_check_is_quiet_when_the_host_and_policy_match(domain, monkeypatch):
    """A host matching the published one and a [policy] covering the
    published globs produce no findings -- the quiet case is a real answer,
    not a check that always warns."""
    config = _declare_challenge(domain, source="provablyfast")
    data = json.loads(json.dumps(PF_SNAPSHOT))
    host_policy = data["campaign"]["measurement_policy"]["host_policy"]
    host_policy["cpu_model"] = "Apple M5"
    host_policy["logical_cpu_count"] = 14
    challenge_mod.pull("provablyfast", "u", "", 3600, 60, domain.paths.root,
                       fetcher=lambda u, t: json.dumps(data))
    # cover the published globs in [policy]
    toml = domain.paths.root / "domain.toml"
    toml.write_text(toml.read_text() +
                    '\n[[policy.forbidden_paths]]\npattern = "**/Cargo.toml"\n'
                    'reason = "challenge forbids it"\n'
                    'example = "x/Cargo.toml"\n'
                    '\n[[policy.forbidden_paths]]\npattern = "scripts/**"\n'
                    'reason = "challenge forbids it"\n'
                    'example = "scripts/x"\n')
    from autoresearch.config import DomainConfig
    config = DomainConfig.load(domain.paths.root)
    findings = challenge_mod.check(config, host=_FakeHost())
    assert findings == [], findings


# -- config wiring -----------------------------------------------------------------

def test_config_refuses_unknown_challenge_keys(domain):
    with pytest.raises(ConfigError, match="unknown key"):
        _declare_challenge(domain, sourc="yukon")


def test_config_flags_an_unknown_source(domain):
    config = _declare_challenge(domain, source="nosuch")
    problems = config.check()
    assert any("nosuch" in p and "adapter" in p for p in problems), problems
