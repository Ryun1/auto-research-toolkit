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


def test_no_endpoint_writes_anything(served):
    sandbox, server = served
    before = _tree(sandbox.paths.root)
    for path in ["/", "/app.js", "/style.css", *_api_paths(server)]:
        _get(server, path)
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
