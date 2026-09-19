"""A public challenge's shared intel: the leaderboard, the published
measurement policy, the research graph other solvers leave behind.

Auto-research domains are usually one entry in a public challenge with a
leaderboard and a bulletin. That shared state is *domain input*, and the
harness already has a seam for domain input -- but nothing else in core reads
it, so without this module every domain re-invents the fetch, the parser, the
cache and the staleness question, differently.

The design splits on the harness's two enforcement classes:

* **Enforced.** The published record *is* the moving target. `ar challenge
  target` prints the number the domain's `bin/probe-target` wraps; nothing
  else in the loop fetches (see the `refresh_seconds` note in
  `config.Challenge` -- `Target.resolve` already TTLs its probe, and a second
  refresh path would disagree with the first about what "fresh" means).
* **Advisory.** The board, the saturation and the published attempts ride
  into the generator/judge/scout briefs as the `challenge` block. They are
  knowledge, never entries: folding external rows into the record would
  pollute `mechanism_coverage`, the yield floor and the meters with verdicts
  this campaign never paid for.

What this module deliberately does not do:

* **No submission.** Submitting to a challenge is irreversible and public;
  the domain declares it `[[policy.human_only]]` and a person runs it.
* **No unsourced numbers.** The source's own truth label (provably.fast
  distinguishes research-graph rows from evaluator-promoted ones) rides with
  the snapshot; a rate without its hardware context is not a measurement,
  which `check()` makes loud rather than assuming away.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
import urllib.request
from dataclasses import asdict, dataclass, field, fields

from .errors import AutoresearchError, ChallengeError

SCHEMA_VERSION = 1
USER_AGENT = "autoresearch-challenge/1.0"

#: adapter name -> default page. A `[challenge] url` overrides; the adapter
#: name is the contract, the URL is deployment detail.
DEFAULT_URLS = {
    "provablyfast": "https://provably.fast/data/index.json",
    "yukon": "https://www.yukon.org/qsb",
}
ADAPTERS = tuple(sorted(DEFAULT_URLS))

#: who publishes the data. A snapshot quotes other people's measurements;
#: the source's own renderings must carry the credit beside them -- a rate
#: whose provenance is unnamed is the exact defect the fingerprint doctrine
#: exists to stop.
SOURCE_CREDIT = {
    "provablyfast": "data by provably.fast (https://provably.fast)",
    "yukon": "data by Yukon / Eigen Labs with StarkWare (https://www.yukon.org/qsb)",
}


# -- fetching ----------------------------------------------------------------

def _fetch(url: str, timeout: float) -> str:
    """One GET, with the timeout a network call owes a session: offline fails
    loudly in a minute rather than hanging an agent's probe (the same contract
    `upstream.TIMEOUT_SECONDS` states)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (OSError, ValueError) as exc:
        raise ChallengeError(f"challenge fetch of {url} failed: {exc}") from exc


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _age(fetched_at: str) -> float | None:
    """Seconds since an ISO timestamp, or None when unparseable -- which reads
    as infinitely stale, so a corrupt timestamp means refetch, never reuse."""
    try:
        then = dt.datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - then).total_seconds()


# -- the snapshot ------------------------------------------------------------

@dataclass
class Record:
    """The standing number to beat, with the context that makes it a
    measurement: who set it, when, and in what unit."""
    value: float
    unit: str = ""
    by: str = ""
    at: str = ""


@dataclass
class Snapshot:
    """One challenge source, normalized. `board[0]` is rank 1; `value` is the
    ranked metric where higher is better (both sources rank that way). A rate
    without its `truth_label` is research-only, not promoted -- the label
    must survive every rendering or the snapshot lies about its own evidence.
    """
    source: str
    challenge: str
    url: str
    fetched_at: str
    benchmark: str = ""
    truth_label: str = ""
    generated_at: str = ""
    record: Record | None = None
    #: rows of {rank, solver, model, value, delta, delta_pct, at}. Best
    #: effort -- a board missing its tail row is reported as parsed, never
    #: padded; the record above is authoritative.
    board: list[dict] = field(default_factory=list)
    #: the published bar, when the source declares one (provably.fast's
    #: `minimum_speedup`, its statistic, its pairing).
    bar: dict | None = None
    #: the published contract: allowed/forbidden paths, measurement policy,
    #: host policy. Absent means `check()` has nothing to compare against.
    policy: dict | None = None
    upstream: dict | None = None
    #: counts only. The row shapes behind these counts are schema_versioned
    #: upstream and unverified against a populated campaign; counting is the
    #: honest ceiling until one exists (the live campaign's graph is empty).
    graph: dict | None = None
    attempts_count: int = 0

    def target_value(self) -> float | None:
        """The number the goal chases: the standing record, or -- when no
        qualifying result exists yet -- the published bar. None when the
        source declares neither, which `ar challenge target` refuses rather
        than printing a guess."""
        if self.record is not None:
            return self.record.value
        if self.bar is not None and self.bar.get("minimum_speedup") is not None:
            return float(self.bar["minimum_speedup"])
        return None

    def saturation(self) -> dict | None:
        """How much room is actually left at the top: the rank-1-to-5 spread
        and the distinct-solver count. A frontier whose spread is 0.36% (the
        live qsb board, 2026-09-19) makes a +0.1% proposal a marginal move,
        and the generator prices differently when it knows that."""
        values = [r["value"] for r in self.board[:5] if r.get("value")]
        if len(values) < 2 or values[0] <= 0:
            return None
        return {
            "top5_spread_pct": round((values[0] - values[-1]) / values[-1] * 100, 3),
            "n_solvers": len({r.get("solver") for r in self.board if r.get("solver")}),
        }

    def to_dict(self) -> dict:
        out = asdict(self)
        out["schema_version"] = SCHEMA_VERSION
        return out

    @classmethod
    def from_dict(cls, data: dict) -> Snapshot:
        if not isinstance(data, dict):
            raise ChallengeError("challenge snapshot is not an object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ChallengeError(
                f"challenge snapshot schema_version {data.get('schema_version')!r} "
                f"is not {SCHEMA_VERSION}; delete state/challenge/ and re-pull")
        record = data.get("record")
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known - {"schema_version"}
        if unknown:
            raise ChallengeError(
                f"challenge snapshot has unknown field(s) {sorted(unknown)}")
        try:
            parsed_record = None if record is None else Record(**record)
        except TypeError as exc:
            raise ChallengeError(
                f"challenge snapshot's record field is malformed ({exc}); "
                "delete state/challenge/ and re-pull") from exc
        return cls(
            **{k: v for k, v in data.items() if k in known and k != "record"},
            record=parsed_record)


# -- adapters ----------------------------------------------------------------

def _from_provablyfast(url: str, body: str, benchmark: str = "") -> Snapshot:
    """`data/index.json` is a published snapshot: the bar, the measurement
    policy, the pinned upstream, the research graph and every attempt. The
    leaderboard table itself is computed client-side from `attempts`, whose
    row schema is unverified (the live campaign's array is empty), so the
    board starts empty and `attempts_count` reports what there is to count."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ChallengeError(f"provablyfast: response is not JSON: {exc}") from exc
    campaign = data.get("campaign") or {}
    measurement = campaign.get("measurement_policy") or {}
    declared = campaign.get("challenge") or {}
    performance = data.get("performance_summary") or {}
    graph = data.get("research_graph") or {}

    best = performance.get("best_measured")
    record = None
    if best is not None:
        record = Record(value=float(best), unit="speedup",
                        by=str(performance.get("best_by") or ""),
                        at=str(performance.get("best_at") or ""))

    bar = None
    if measurement.get("minimum_speedup") is not None:
        bar = {"minimum_speedup": float(measurement["minimum_speedup"]),
               "statistic": measurement.get("statistic"),
               "pairs": measurement.get("pairs"),
               "warmup_runs": measurement.get("warmup_runs"),
               "timeout_seconds": measurement.get("timeout_seconds")}

    policy = None
    if measurement or declared:
        policy = {
            "allowed_paths": list(declared.get("allowed_paths") or []),
            "forbidden_paths": list(declared.get("forbidden_paths") or []),
            "selection_basis": declared.get("selection_basis") or "",
            "measurement_policy": {k: v for k, v in measurement.items()
                                   if k != "minimum_speedup"},
        }

    return Snapshot(
        source="provablyfast",
        challenge=str(campaign.get("campaign_id") or ""),
        url=url,
        fetched_at=_now(),
        truth_label=str(data.get("truth_label") or ""),
        generated_at=str(data.get("generated_at") or ""),
        record=record,
        bar=bar,
        policy=policy,
        upstream=campaign.get("upstream"),
        graph={
            "nodes": len(graph.get("nodes") or []),
            "edges": len(graph.get("edges") or []),
            "evidence_class": str(graph.get("evidence_class") or ""),
        } if graph else None,
        attempts_count=len(data.get("attempts") or []))


#: yukon's board is server-rendered HTML. These patterns are the proven ones
#: (24/24 live rows, 2026-09-19); the page is best-effort by design -- a row
#: the regex misses is one fewer board row, never a wrong number, because the
#: rank comes from enumeration order and the record line is parsed
#: separately.
_YUKON_ROW = re.compile(
    r'href="(/solver/[^"]+)"[^>]*>([^<]+)</a>.*?'
    r'([\d,]+)\s*verified candidates/s.*?'
    r'\+([\d,]+) candidates/s \(\+([\d.]+)%\).*?'
    r'([A-Z][a-z]{2} \d{1,2}, \d{4} at [\d:]+ [AP]M UTC)', re.S)
#: the "current record" header precedes the board rows in page order, so the
#: first precise "verified candidates/s" figure IS the standing record. The
#: labelled "Pinning record:" line renders the same number again without
#: "verified", but label-anchored parsing loses to the page's own markup
#: volume (inline SVG payloads put the digits arbitrarily far from the
#: label); the header sits directly beside its figure.
_YUKON_RECORD = re.compile(r'([\d,]{7,})\s*verified candidates/s')
#: the workload label rides beside "record" -- but the live page splits the
#: pair with React comment nodes ("Pinning<!-- --> record") and the
#: leaderboard header says "current record", so the match tolerates inline
#: markup and the code skips the header's label.
_YUKON_WORKLOAD = re.compile(
    r'([A-Za-z][\w/]{1,30})(?:<[^>]+>|\s)+record', re.I)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _from_yukon(url: str, body: str, benchmark: str = "") -> Snapshot:
    page = body
    board = []
    for rank, m in enumerate(_YUKON_ROW.finditer(page), 1):
        path, solver, value, delta, pct, at = m.groups()
        board.append({
            "rank": rank,
            "solver": solver,
            "model": "",       # the page renders models as SVG logos; not parsed
            "value": _int(value),
            "delta": _int(delta),
            "delta_pct": float(pct),
            "at": at,
            "path": path,
        })
    record = None
    record_match = _YUKON_RECORD.search(page)
    if record_match is not None:
        record = Record(value=_int(record_match.group(1)),
                        unit="candidates/s", by="", at="")
        if board and board[0]["value"] == record.value:
            # rank 1 is the record holder by definition; the header omits
            # the attribution the board row carries
            record.by, record.at = board[0]["solver"], board[0]["at"]
    elif board:
        # no header parsed: rank 1 is the record holder by definition
        record = Record(value=board[0]["value"], unit="candidates/s",
                        by=board[0]["solver"], at=board[0]["at"])
    if record is not None:
        # always derive the label from the page, never from the request: the
        # mismatch check in pull needs what the page actually serves
        for m in _YUKON_WORKLOAD.finditer(page):
            label = m.group(1)
            if label.lower() == "current":
                continue      # the leaderboard header, not a workload label
            benchmark = label.lower()
            break
    return Snapshot(
        source="yukon",
        challenge=url.rstrip("/").rsplit("/", 1)[-1],
        url=url,
        fetched_at=_now(),
        benchmark=benchmark,
        record=record,
        board=board)


_PARSE = {"provablyfast": _from_provablyfast, "yukon": _from_yukon}


# -- the cache ---------------------------------------------------------------

def snapshot_path(root) -> pathlib.Path:
    """`state/challenge/snapshot.json`. `state/`, not `data/`: the snapshot is
    machine-owned input with no run row behind it, and `data/` is for
    run-backed findings -- the lane boundary (H51) is exactly about a file
    whose provenance does not match its directory."""
    return pathlib.Path(root) / "state" / "challenge" / "snapshot.json"


def load_snapshot(root) -> Snapshot | None:
    path = snapshot_path(root)
    if not path.exists():
        return None
    try:
        return Snapshot.from_dict(json.loads(path.read_text()))
    except (OSError, ChallengeError) as exc:
        raise ChallengeError(
            f"challenge snapshot at {path} is unreadable: {exc}") from exc


def pull(source: str, url: str, benchmark: str, refresh_seconds: float,
         timeout_seconds: float, root, *, force: bool = False,
         fetcher=None) -> tuple[Snapshot, str]:
    """Fetch-if-stale, cache, return `(snapshot, what_happened)`.

    Fetch failure with a warm cache is the caller's decision: `pull` raises,
    and `target()` catches -- a human running `pull` wants the error, a probe
    wants yesterday's record over a dead morning.
    """
    if source not in _PARSE:
        raise ChallengeError(
            f"challenge source {source!r} is not a known adapter; "
            f"known: {', '.join(ADAPTERS)}")
    fetcher = fetcher or _fetch
    cached = load_snapshot(root)
    if cached is not None and not force:
        fresh = (cached.source == source
                 and cached.url == url
                 and (age := _age(cached.fetched_at)) is not None
                 and age < refresh_seconds)
        if fresh:
            return cached, (f"cached ({_human_age(age)} old; refreshes after "
                            f"{refresh_seconds:g}s)")
    parsed = _PARSE[source](url, fetcher(url, timeout_seconds), benchmark)
    if benchmark and parsed.benchmark and parsed.benchmark != benchmark:
        # A declared request the page does not answer is a mismatch, not a
        # preference: the page this URL serves is the pinning board, and a
        # domain asking for subset must be told that instead of silently
        # ranking against the wrong frontier.
        raise ChallengeError(
            f"challenge source {source!r} serves the {parsed.benchmark!r} "
            f"board, but [challenge] declares benchmark {benchmark!r}; "
            "point url at the board you mean")
    parsed.benchmark = parsed.benchmark or benchmark
    path = snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(parsed.to_dict(), indent=2, sort_keys=True) + "\n")
    return parsed, "fetched"


def target_value(source: str, url: str, benchmark: str, refresh_seconds: float,
                 timeout_seconds: float, root, *, fetcher=None) -> float:
    """The number the goal chases. The probe contract is one float on stdout;
    notes go to stderr, which the probe keeps for failures. Site down plus a
    warm cache returns the cached record -- a leaderboard having a bad day
    must not stop a loop that has a sane target -- while no cache at all
    raises into the probe-failure path the coordinator already records."""
    try:
        snapshot, action = pull(source, url, benchmark, refresh_seconds,
                                timeout_seconds, root, fetcher=fetcher)
    except ChallengeError as exc:
        cached = load_snapshot(root)
        if cached is None or cached.target_value() is None:
            raise
        print(f"challenge: fetch failed ({exc}); using cached snapshot",
              file=sys.stderr)
        return cached.target_value()
    value = snapshot.target_value()
    if value is None:
        raise ChallengeError(
            "the snapshot names no record and no bar; a target cannot be "
            "read from it")
    print(f"challenge target: {action}", file=sys.stderr)
    return value


# -- the briefs --------------------------------------------------------------

def brief_payload(root, source: str) -> dict | None:
    """The `challenge` block for generator/judge/scout briefs. Cache read
    only -- orient does no network IO; the probe is the one refresher. None
    when the table is empty or nothing is cached, so a role-gated brief
    simply omits the key rather than carrying an empty promise."""
    if not source:
        return None
    snapshot = load_snapshot(root)
    if snapshot is None:
        return None
    payload = {
        "source": snapshot.source,
        "challenge": snapshot.challenge,
        "benchmark": snapshot.benchmark,
        "truth_label": snapshot.truth_label,
        "fetched_at": snapshot.fetched_at,
        "record": asdict(snapshot.record) if snapshot.record else None,
        "bar": snapshot.bar,
        "saturation": snapshot.saturation(),
        "board_top": snapshot.board[:5],
        "attempts_count": snapshot.attempts_count,
        "graph": snapshot.graph,
        "note": "shared state from outside this board: do not propose what "
                "it shows is already settled, and price marginal ideas "
                "against its saturation",
    }
    return payload


# -- the comparability check ---------------------------------------------------

def _sample_path(glob: str) -> str:
    """A concrete path a published forbidden glob would match, so the domain's
    own policy can be tested against it (the same probes-over-the-pattern
    idea `Rule.probes()` states: a check that tests the pattern string tests
    nothing)."""
    sample = glob.replace("**/", "x/") if "**/" in glob else glob
    if sample.endswith("/**"):
        sample = sample[:-3] + "x"
    return sample.replace("*", "x")


def check(config, host=None) -> list[str]:
    """Comparability findings -- loud notes, not refusals. Two rows with
    different fingerprints are not comparable without saying so
    (`hardware.Host.fingerprint`'s own contract), and a domain whose
    `[policy]` does not cover the challenge's forbidden paths is one rejected
    submission away from learning it the expensive way."""
    if not config.challenge.source:
        raise ChallengeError("no [challenge] source declared; nothing to check")
    from . import hardware as hw
    findings: list[str] = []
    snapshot = load_snapshot(config.paths.root)
    if snapshot is None:
        return ["no snapshot cached; run `ar challenge pull` first"]
    age = _age(snapshot.fetched_at)
    if age is not None and age > config.challenge.refresh_seconds * 4:
        findings.append(
            f"snapshot is {_human_age(age)} old -- refresh_seconds is "
            f"{config.challenge.refresh_seconds:g}s; the record may have moved")
    if snapshot.source != config.challenge.source:
        findings.append(
            f"snapshot came from {snapshot.source!r} but [challenge] declares "
            f"{config.challenge.source!r}; re-pull before trusting either")

    published = snapshot.policy or {}
    if not published:
        findings.append(
            f"{snapshot.source} publishes no machine-readable policy; "
            "comparability is uncheckable -- name the hardware and "
            "concurrency beside every rate you quote")
        return findings

    if host is None:
        host = hw.detect()
    host_policy = (published.get("measurement_policy") or {}).get("host_policy") or {}
    if host_policy:
        published_cpu = host_policy.get("cpu_model")
        if published_cpu and host.chip and _norm(published_cpu) != _norm(host.chip):
            findings.append(
                f"challenge measured on {published_cpu!r}; this host is "
                f"{host.chip!r} -- rates are not comparable without saying so")
        published_threads = host_policy.get("logical_cpu_count")
        if published_threads and host.cpu_threads and published_threads != host.cpu_threads:
            findings.append(
                f"challenge ran with {published_threads} logical CPUs; this "
                f"host reports {host.cpu_threads} threads")
        published_bytes = host_policy.get("memory_bytes")
        if published_bytes and host.memory_gb:
            published_gb = published_bytes / 2 ** 30
            if abs(host.memory_gb - published_gb) / published_gb > 0.05:
                findings.append(
                    f"challenge ran with {published_gb:.0f}GiB; this host "
                    f"reports {host.memory_gb:.0f}GB")

    domain_globs = [r.pattern for r in config.policy.forbidden_paths]
    for forbidden in published.get("forbidden_paths") or []:
        if forbidden in domain_globs:
            continue
        try:
            config.policy.check_paths([_sample_path(forbidden)])
        except AutoresearchError:
            continue    # the domain's policy refuses it: covered
        findings.append(
            f"challenge forbids {forbidden!r}; your [policy] forbidden_paths "
            "does not cover it -- editing that file would be a rejected "
            "submission")
    return findings


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
