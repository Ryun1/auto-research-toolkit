"""The HTTP side of `ar ui`: static files, JSON endpoints and one event stream.

Read-only by construction: the handler answers GET and HEAD and nothing else,
and no endpoint calls a function that writes.

Binding to localhost is not enough on its own. A web page on any site can
point a hostname it controls at 127.0.0.1 (DNS rebinding) and then read
`http://that-name:8484/api/...` as same-origin. So a request whose `Host`
header is not a loopback name or the address the server was asked to bind
is refused, and no response carries a CORS header.
"""
from __future__ import annotations

import json
import mimetypes
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources as pkg_resources

from .api import Api
from .watch import Watcher

#: Seconds between keep-alive comments on the event stream; also how soon a
#: closed tab's handler thread notices and exits.
KEEPALIVE = 15.0
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _static(name: str) -> bytes | None:
    """A file shipped under `static/`, at most one directory deep; never a
    path that climbs out of it."""
    parts = name.split("/")
    if len(parts) > 2 or any(not p or p.startswith(".") for p in parts):
        return None
    node = pkg_resources.files(__package__).joinpath("static", *parts)
    try:
        return node.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ar-ui"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):         # quiet unless asked
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host[1:host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in LOOPBACK or name == self.server.bound_host.lower()

    def _send(self, status, body: bytes, kind: str, head=False):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, status, doc, head=False):
        self._send(status, json.dumps(doc).encode(), "application/json", head)

    def _refuse(self, status, why):
        self._json(status, {"error": why})

    # -- verbs ------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self._host_ok():
            return self._refuse(HTTPStatus.FORBIDDEN,
                                "ar ui answers only to localhost (Host header refused)")
        path = self.path.split("?", 1)[0]
        if path == "/api/events":
            return self._events()
        if path.startswith("/api/"):
            name = path[len("/api/"):]
            if name not in self.server.endpoints:
                return self._refuse(HTTPStatus.NOT_FOUND, f"no endpoint {name!r}")
            try:
                doc = self.server.api.get(name)
            except Exception as exc:    # a record that will not load is the page's to show
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                  {"error": f"{type(exc).__name__}: {exc}"}, head)
            return self._json(HTTPStatus.OK, doc, head)
        name = "index.html" if path == "/" else path.lstrip("/")
        body = _static(name)
        if body is None:
            return self._refuse(HTTPStatus.NOT_FOUND, f"no file {path!r}")
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        return self._send(HTTPStatus.OK, body, kind, head)

    def _refuse_write(self):
        self._refuse(HTTPStatus.METHOD_NOT_ALLOWED, "ar ui is read-only")

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse_write

    def _events(self):
        """Server-Sent Events: one `change` event per settled change, its data
        the JSON list of resource names. The first message is `hello` with
        the current generation, so a reconnecting page refetches everything."""
        watcher = self.server.watcher
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seen = watcher.generation
        try:
            self.wfile.write(f"event: hello\ndata: {seen}\n\n".encode())
            self.wfile.flush()
            while not self.server.stopping.is_set():
                gen, names = watcher.wait(seen, timeout=KEEPALIVE)
                if gen > seen:
                    seen = gen
                    self.wfile.write(
                        f"event: change\ndata: {json.dumps(sorted(names))}\n\n".encode())
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(config, host="127.0.0.1", port=8484, interval=1.0, verbose=False):
    """A configured, not yet running server. `port=0` takes an ephemeral port
    (`server.server_address[1]` names it). The watcher is started by `start`."""
    cls = Server
    if ":" in host:
        cls = type("Server6", (Server,), {"address_family": socket.AF_INET6})
    server = cls((host, port), Handler)
    server.bound_host = host
    server.verbose = verbose
    server.api = Api(config)
    server.endpoints = {"summary", "board", "entries"}
    server.watcher = Watcher(config, interval=interval)
    server.stopping = threading.Event()
    real_publish = server.watcher.publish

    def publish(names):
        server.api.invalidate(names)      # before readers wake and refetch
        real_publish(names)
    server.watcher.publish = publish

    def start():
        server.watcher.start()
        thread = threading.Thread(target=server.serve_forever, name="ar-ui",
                                  daemon=True)
        thread.start()
        return thread

    def stop():
        server.stopping.set()
        server.watcher.stop()
        server.shutdown()
        server.server_close()
    server.start, server.stop = start, stop
    return server


def serve(config, host="127.0.0.1", port=8484, open_browser=False) -> int:
    server = make_server(config, host, port)
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{server.server_address[1]}/"
    print(f"ar ui: serving {config.name} on {url}  (read-only; Ctrl-C stops)", flush=True)
    if host not in LOOPBACK:
        print(f"ar ui: warning: bound to {host}, not localhost; anyone who can "
              f"reach it can read this campaign's record", file=sys.stderr)
    server.watcher.start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.watcher.stop()
        server.server_close()
    return 0
