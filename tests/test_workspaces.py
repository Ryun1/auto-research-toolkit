import pytest

from autoresearch.errors import ClaimError
from autoresearch.workspaces import Pool


def test_slot_is_created_and_isolated(sandbox):
    with Pool(sandbox, "coordinator") as pool:
        slot = pool.acquire("w1")
        assert slot.path.exists()
        assert (slot.path / "bin" / "measure").exists()
    assert not slot.path.exists()


def test_reusing_a_name_is_refused(sandbox):
    """H70: `create` silently reused a worktree when the session name already
    existed, so two ticks shared one index."""
    with Pool(sandbox, "coordinator") as pool:
        pool.acquire("w1")
        with pytest.raises(ClaimError, match="already exists"):
            pool.acquire("w1")


def test_release_by_a_non_holder_is_refused(sandbox):
    """H57/H59: teardown with no ownership check removed other sessions' slots."""
    pool_a = Pool(sandbox, "coordinator-a")
    pool_a.acquire("w1")
    pool_b = Pool(sandbox, "coordinator-b")
    with pytest.raises(ClaimError, match="not by 'coordinator-b'"):
        pool_b.release("w1")
    pool_a.release_all()


def test_release_all_is_wired_into_the_context_manager(sandbox):
    """H91: teardown documented in a runbook and wired into no loop left 64 of
    64 merged worktrees on disk."""
    pool = Pool(sandbox, "c")
    with pool:
        pool.acquire("w1")
        pool.acquire("w2")
        assert len(pool.slots) == 2
    assert pool.slots == {}
    assert not (sandbox.paths.workspaces / "w1").exists()


def test_release_all_survives_a_slot_that_vanished(sandbox):
    import shutil
    pool = Pool(sandbox, "c")
    slot = pool.acquire("w1")
    shutil.rmtree(slot.path)
    assert pool.release_all() == 1
