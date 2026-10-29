"""How entries connect, from the fields the record stores and nothing else.

Five kinds of link, each a field an entry carries:

* `parent` -- a branch of the tree (tree.py);
* `derived` -- `derived_from`, lineage the tree's caps kept off it (#23);
* `supersedes` -- the old entry and the one that replaced it;
* `related` -- undirected, drawn once per pair;
* `cites` -- a `sources` item that is the id of an entry in the store.

`sources` matters most on a large record: ECDSA Fail sets no `parent`,
`supersedes` or `related` at all, but 406 of its 470 entries carry
`sources`, so without it the record is 470 unconnected entries. An item is
an entry id only when it matches one exactly; anything else is a path. Ids
are never parsed out of prose -- 440 ECDSA entries mention other ids in
their text, and drawing those mentions would be a regex reaching a decision.

Every link points from earlier knowledge to what builds on it: parent to
child, the cited entry to the one citing it, the superseded entry to its
replacement. So "downstream of X" is reachability from X, which is what a
view's focus mode and taint propagation (#54) both need.
"""
from __future__ import annotations

from dataclasses import dataclass

KINDS = ("parent", "derived", "supersedes", "related", "cites")


@dataclass(frozen=True, order=True)
class Link:
    src: str      # the earlier knowledge
    dst: str      # what builds on it
    kind: str


def links(entries) -> list[Link]:
    """Every link between entries in `entries`, sorted, each pair joined at
    most once per kind. A `cites` link is dropped where a lineage link
    already joins the pair: a child citing its parent says nothing more."""
    ids = {e.id for e in entries}
    out: set[Link] = set()
    joined: set[frozenset] = set()
    for e in entries:
        if e.parent in ids and e.parent != e.id:
            out.add(Link(e.parent, e.id, "parent"))
            joined.add(frozenset((e.parent, e.id)))
        if e.derived_from in ids and e.derived_from != e.id:
            out.add(Link(e.derived_from, e.id, "derived"))
            joined.add(frozenset((e.derived_from, e.id)))
        for old in e.supersedes:
            if old in ids and old != e.id:
                out.add(Link(old, e.id, "supersedes"))
                joined.add(frozenset((old, e.id)))
        for other in e.related:
            if other in ids and other != e.id:
                a, b = sorted((e.id, other))
                out.add(Link(a, b, "related"))
    for e in entries:
        for item in e.sources:
            if item in ids and item != e.id and frozenset((item, e.id)) not in joined:
                out.add(Link(item, e.id, "cites"))
    return sorted(out)


def source_paths(entry, ids) -> list[str]:
    """The entry's `sources` items that are not entry ids: memo, guide and
    code paths, in the order the entry lists them."""
    return [item for item in entry.sources if item not in ids]
