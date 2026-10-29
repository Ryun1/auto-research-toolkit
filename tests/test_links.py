"""`links.links`: how entries connect, from stored fields only (#51)."""
from autoresearch.links import Link, links, source_paths
from conftest import make_entry


def test_every_stored_field_is_a_link(store):
    make_entry(store, "Q1")
    make_entry(store, "Q2", parent="Q1")
    make_entry(store, "Q3", derived_from="Q1")
    make_entry(store, "Q4", supersedes=["Q3"])
    make_entry(store, "Q5", related=["Q4"])
    make_entry(store, "Q6", sources=["Q2", "inbox/m.md", "guides/g.md"])
    assert links(store.all()) == sorted([
        Link("Q1", "Q2", "parent"), Link("Q1", "Q3", "derived"),
        Link("Q3", "Q4", "supersedes"), Link("Q4", "Q5", "related"),
        Link("Q2", "Q6", "cites")])


def test_a_cite_is_an_exact_id_in_the_store_and_never_prose(store):
    make_entry(store, "Q1", hypothesis="builds on Q2 and H7")
    make_entry(store, "Q2", sources=["Q1", "Q99", "q1", "see Q1", "inbox/Q1.md"])
    assert links(store.all()) == [Link("Q1", "Q2", "cites")]
    assert source_paths(store.load("Q2"), {"Q1", "Q2"}) == [
        "Q99", "q1", "see Q1", "inbox/Q1.md"]


def test_a_cite_crosses_tracks(store):
    make_entry(store, "H1", track="harness")
    make_entry(store, "Q1", sources=["H1"])
    assert links(store.all()) == [Link("H1", "Q1", "cites")]


def test_each_pair_is_joined_once(store):
    """A child citing its parent adds nothing; related is undirected."""
    make_entry(store, "Q1", related=["Q2"])
    make_entry(store, "Q2", parent="Q1", sources=["Q1", "Q1"], related=["Q1"])
    make_entry(store, "Q3", sources=["Q3"])
    assert links(store.all()) == [Link("Q1", "Q2", "parent"),
                                  Link("Q1", "Q2", "related")]


def test_a_link_to_an_entry_not_in_the_store_is_dropped(store):
    make_entry(store, "Q2", parent="Q1", supersedes=["Q9"], related=["H4"])
    assert links(store.all()) == []
