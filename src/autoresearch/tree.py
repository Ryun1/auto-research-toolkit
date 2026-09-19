"""Filing-time tree validation, shared by the loop and the CLI.

A branch is filed in two ways -- a generator's JSON proposal through the
coordinator, and `ar entry new --parent` from a human or a native-dispatch
agent. Both must clear the same gate with the same refusals, or the two
paths diverge into different trees. The coordinator owns the structure: an
unknown parent, a lineage past `tree_max_depth`, more siblings per parent
than `tree_max_children`, a branch crossing tracks, or a kind on a root is
refused here, with a reason, before anything is written. A parent the record
has marked stagnant (`branch_stagnation`: enough refuted children, none
confirmed) is refused a new child for the same reason."""
from __future__ import annotations

from .entries import KINDS
from .errors import AutoresearchError


def check_branch(config, store, parent: str, kind: str,
                 track_id: str) -> None:
    """Refuse an invalid branch filing, or return None when it may proceed.

    `parent` is the entry id being branched from ("" = a novel root, which
    skips every lineage check). `kind` is the branch intent or "". Raises
    `AutoresearchError` with the refusal reason; the caller records it.
    """
    if kind:
        if kind not in KINDS:
            raise AutoresearchError(
                f"unknown branch kind {kind!r}; one of {KINDS}, or omit it")
        if not parent:
            raise AutoresearchError(
                f"kind {kind!r} is branch intent; a proposal with no "
                "parent is a novel root and carries no kind")
    if not parent:
        return
    try:
        parent_entry = store.load(parent)
    except Exception as exc:
        raise AutoresearchError(
            f"cannot file a branch of {parent!r}: no such entry") from exc
    if parent_entry.track != track_id:
        raise AutoresearchError(
            f"cannot file a branch of {parent!r} on track "
            f"{track_id!r}: a branch stays on its parent's track")
    depth, walked, lineage = 1, parent, {parent}
    while walked:
        walked = store.load(walked).parent
        if walked:
            if walked in lineage:
                raise AutoresearchError(
                    f"lineage of {parent!r} is cyclic at {walked!r}")
            lineage.add(walked)
            depth += 1
    max_depth = config.tree_max_depth
    if max_depth and depth > max_depth:
        raise AutoresearchError(
            f"branch depth {depth} exceeds tree_max_depth={max_depth}; "
            "deepen the record by closing work, or raise the cap")
    max_children = config.tree_max_children
    if max_children:
        siblings = sum(1 for e in store.all() if e.parent == parent)
        if siblings + 1 > max_children:
            raise AutoresearchError(
                f"parent {parent!r} already has {siblings} branch(es); "
                f"tree_max_children={max_children}")
    stagnation = getattr(config, "branch_stagnation", 0)
    if stagnation:
        from .rank import stagnant_parents
        stagnant = stagnant_parents(
            store.all(), lambda eid: config.track_for(eid).machine, stagnation)
        if parent in stagnant:
            raise AutoresearchError(
                f"parent {parent!r} is stagnant: {stagnant[parent]} refuted "
                f"branch(es) with none confirmed "
                f"(branch_stagnation={stagnation}); a refusal here is "
                "information, the same one the tree caps give")
