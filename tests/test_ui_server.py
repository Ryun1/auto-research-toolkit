"""`ar ui`'s server (#50): every endpoint answers, nothing writes, and a
change to the record reaches the page as an event."""
import hashlib
import http.client
import json
import os
import pathlib
import threading
import time

import pytest

from autoresearch import cli
from autoresearch.ui import make_server
from autoresearch.ui.server import ARG_ENDPOINTS
from autoresearch.ui.watch import Watcher
from conftest import make_entry


@pytest.fixture
def served(sandbox, store):
    make_entry(store, "Q1", hypothesis="h")
    server = make_server(sandbox, port=0, interval=0.1)
    server.start()
    yield sandbox, server
    server.stop()


def _get(server, path, host=None, method="GET"):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, response.getheader("Content-Type"), body


def _api_paths(server):
    return [f"/api/{name}/Q1" if name in ARG_ENDPOINTS else f"/api/{name}"
            for name in sorted(server.endpoints)]


def _tree(root: pathlib.Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_every_endpoint_and_file_answers(served):
    _, server = served
    for path in ("/", "/app.js", "/style.css", "/graph.js", "/md.js",
                 "/vendor/cytoscape.min.js", "/vendor/dagre.min.js",
                 "/vendor/cytoscape-dagre.js"):
        status, kind, body = _get(server, path)
        assert status == 200 and body, path
    assert "text/html" in _get(server, "/")[1]
    for path in _api_paths(server):
        status, kind, body = _get(server, path)
        assert status == 200 and kind == "application/json", path
        json.loads(body)
    summary = json.loads(_get(server, "/api/summary")[2])
    assert summary["domain"] == "toy" and summary["goal"]["id"]
    [row] = json.loads(_get(server, "/api/entries")[2])
    assert row["id"] == "Q1"


def test_the_board_endpoint_is_ar_boards_own_document(served, capsys):
    sandbox, server = served
    assert cli.main(["--domain", str(sandbox.paths.root), "board", "--json"]) == 0
    assert json.loads(_get(server, "/api/board")[2]) == json.loads(capsys.readouterr().out)


def test_no_endpoint_writes_anything(served, store):
    """With runs, a closed entry's memo and a live claim, so the standing,
    memo and claim paths all run."""
    from autoresearch.claims import Claims
    from autoresearch.runs import RunRecord, append
    from conftest import close
    sandbox, server = served
    make_entry(store, "Q2", sources=["Q1", "bin/probe-target"])
    close(sandbox, store, store.load("Q1"))
    Claims(store, sandbox, "alice").claim("Q2", why="t")
    append(sandbox.paths.runs / "s.jsonl", RunRecord(
        id="r1", metrics={"ops": 5.0, "peak": 1.0}, session="s", entry="Q1",
        provenance={"host": "local"}))
    time.sleep(0.4)                       # let the watcher settle the setup
    before = _tree(sandbox.paths.root)
    for path in ["/", "/app.js", "/style.css", *_api_paths(server), "/api/entry/Q2"]:
        assert _get(server, path)[0] == 200, path
    assert json.loads(_get(server, "/api/entry/Q1")[2])["runs"][0]["standing"] == "ok"
    assert _tree(sandbox.paths.root) == before


def test_writes_traversal_and_foreign_hosts_are_refused(served):
    _, server = served
    assert _get(server, "/api/board", method="POST")[0] == 405
    assert _get(server, "/api/board", method="DELETE")[0] == 405
    assert _get(server, "/../pyproject.toml")[0] == 404
    assert _get(server, "/.hidden")[0] == 404
    assert _get(server, "/api/nope")[0] == 404
    assert _get(server, "/api/entry")[0] == 404
    assert _get(server, "/api/entry/Q404")[0] == 404
    assert _get(server, "/api/board/extra")[0] == 404
    # DNS rebinding: a page on another site resolves its own name to 127.0.0.1
    status, _, body = _get(server, "/api/board", host="evil.example:8484")
    assert status == 403 and b"localhost" in body
    assert _get(server, "/api/board", host="localhost:8484")[0] == 200


def test_a_refused_head_has_no_body_and_a_refused_write_closes(served):
    _, server = served
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("HEAD", "/nope")
    response = conn.getresponse()
    assert response.status == 404 and response.read() == b""
    conn.request("GET", "/api/summary")             # the same connection still works
    assert conn.getresponse().status == 200
    conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/board", body=b"GET /api/board HTTP/1.1\r\n\r\n")
    response = conn.getresponse()
    assert response.status == 405 and response.getheader("Connection") == "close"
    conn.close()


def test_a_build_the_record_moved_under_is_not_cached(sandbox, store):
    """A document built across an invalidation is already stale: caching it
    would serve it until the next change."""
    from autoresearch.ui.api import Api
    make_entry(store, "Q1")
    api = Api(sandbox)
    real = api._entries

    def slow():
        doc = real()
        make_entry(store, "Q2")
        api.invalidate({"entries"})        # the watcher, mid-build
        return doc
    api._entries = slow
    assert [r["id"] for r in api.get("entries")] == ["Q1"]
    api._entries = real
    assert [r["id"] for r in api.get("entries")] == ["Q1", "Q2"]


def test_the_clock_ticks_so_age_based_facts_refresh(sandbox, monkeypatch):
    from autoresearch.ui import watch
    monkeypatch.setattr(watch, "CLOCK", 0.05)
    watcher = Watcher(sandbox, interval=0.05)
    watcher.start()
    try:
        gen, names = watcher.wait(0, timeout=3)
    finally:
        watcher.stop()
    assert gen >= 1 and "clock" in names


def test_the_server_binds_to_localhost_by_default(sandbox):
    server = make_server(sandbox, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
    args = cli.build_parser().parse_args(["ui"])
    assert args.host == "127.0.0.1" and args.port == 8484


def test_an_entry_edit_reaches_the_event_stream(served, store):
    sandbox, server = served
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events")
    stream = conn.getresponse()
    assert stream.getheader("Content-Type") == "text/event-stream"
    assert stream.readline() == b"event: hello\n"
    stream.readline(), stream.readline()
    events = []

    def read():
        while True:
            line = stream.readline()
            if not line:
                return
            if line.startswith(b"data: "):
                events.append((time.monotonic(), json.loads(line[6:])))
                return

    # Cache the document first, so the assertion below shows it was dropped.
    assert [r["id"] for r in json.loads(_get(server, "/api/entries")[2])] == ["Q1"]
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    time.sleep(0.3)
    edited = time.monotonic()
    make_entry(store, "Q2")
    reader.join(timeout=5)
    conn.close()
    assert events, "no change event"
    at, names = events[0]
    assert "entries" in names and at - edited < 2.0
    # the cached document was dropped before the event went out
    assert {r["id"] for r in json.loads(_get(server, "/api/entries")[2])} == {"Q1", "Q2"}


def test_a_file_is_named_by_its_most_specific_resource(sandbox):
    watcher = Watcher(sandbox, interval=0.01)
    entry = sandbox.paths.entries / "Q9.yaml"
    entry.write_text("id: Q9\n")
    assert watcher.poll() == {"entries"}
    other = sandbox.paths.root / "state" / "usage.jsonl"
    other.write_text("{}\n")
    assert watcher.poll() == {"state"}
    os.utime(entry, ns=(1, 1))
    assert watcher.poll() == {"entries"}
    assert watcher.poll() == set()
