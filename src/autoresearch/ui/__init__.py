"""`ar ui`: a local, read-only view of one campaign (#59).

A stdlib HTTP server for the domain it is started in: JSON endpoints, static
files shipped as package data, and one Server-Sent Events stream that names
the resources that changed so the page refetches them. It never writes to
the record -- every action stays in the CLI -- and every number comes from
the core functions the CLI uses (`board.snapshot` is `ar board`'s own
document), so the page cannot disagree with `ar board`.

The toolkit depends only on PyYAML, and so does this: a domain that never
runs `ar ui` pays nothing for it.
"""
from .server import make_server, serve

__all__ = ["make_server", "serve"]
