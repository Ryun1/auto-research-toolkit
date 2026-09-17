import json
import multiprocessing
import os
from pathlib import Path

import pytest

from autoresearch.errors import ClaimError
from autoresearch.workspaces import Pool


def _hold_operation_lock(root, connection):
    from autoresearch.config import DomainConfig
    with Pool(DomainConfig.load(root), "live-holder")._lock():
        connection.send("locked")
        connection.recv()


def _recover_and_reacquire(root, connection):
    from autoresearch.config import DomainConfig
    pool = Pool(DomainConfig.load(root), "operator")
    row = pool.inspect("retained")
    connection.send("inspected")
    receipt = pool.recover("retained", holder=row["holder"], token=row["token"],
                           confirm_inactive=True)
    pool.acquire("retained")
    connection.send(receipt)


def _crash_during_recovery(root, checkpoint):
    from autoresearch.config import DomainConfig
    pool = Pool(DomainConfig.load(root), "operator")
    row = pool.inspect("retained")
    if checkpoint == "moved":
        rename = os.rename

        def crash_after_move(source, destination):
            rename(source, destination)
            os._exit(71)

        os.rename = crash_after_move
    elif checkpoint == "branch-rename":
        import autoresearch.workspaces as workspaces

        def interrupt_branch_rename(root, *args, **kwargs):
            if args[:2] == ("worktree", "move"):
                os.rename(args[2], args[3])
            else:
                assert args[:2] == ("branch", "-m")
                os._exit(71)

        workspaces._git = interrupt_branch_rename
    else:
        unlink = Path.unlink

        def crash_after_marker_removal(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path == pool.root / "retained.json":
                os._exit(71)

        Path.unlink = crash_after_marker_removal
    pool.recover("retained", holder=row["holder"], token=row["token"],
                 confirm_inactive=True)


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


def test_release_all_reports_a_release_it_could_not_do(sandbox, capsys):
    """A release that fails must not vanish into `except: pass`: the leaked
    slot is named, and the count says nothing was freed."""
    pool = Pool(sandbox, "c")
    pool.acquire("w1")
    (pool.root / "w1.json").write_text("{not json")
    assert pool.release_all() == 0
    err = capsys.readouterr().err
    assert "w1" in err and "release failed" in err


def test_recovery_archives_evidence_and_frees_slot(sandbox):
    pool = Pool(sandbox, "departed")
    slot = pool.acquire("retained")
    (slot.path / "evidence.bin").write_bytes(b"irreplaceable")
    pool.retain("retained", "harvest failed")
    recovery = Pool(sandbox, "operator")
    inspection = recovery.inspect("retained")
    with pytest.raises(ClaimError, match="confirmation"):
        recovery.recover("retained", holder="departed", token=inspection["token"],
                         confirm_inactive=False)
    with pytest.raises(ClaimError, match="changed"):
        recovery.recover("retained", holder="other", token=inspection["token"],
                         confirm_inactive=True)
    pool.retain("retained", "new evidence arrived")
    with pytest.raises(ClaimError, match="changed"):
        recovery.recover("retained", holder="departed", token=inspection["token"],
                         confirm_inactive=True)
    inspection = recovery.inspect("retained")
    receipt = recovery.recover("retained", holder="departed", token=inspection["token"],
                               confirm_inactive=True)
    from pathlib import Path
    assert (Path(receipt["archive"]) / "evidence.bin").read_bytes() == b"irreplaceable"
    assert not (pool.root / "retained.json").exists()
    replacement = recovery.acquire("retained")
    assert replacement.path.exists()
    recovery.release_all()


def test_recovery_refuses_redirected_marker(sandbox):
    import json
    pool = Pool(sandbox, "departed")
    pool.acquire("retained")
    marker = pool.root / "retained.json"
    record = json.loads(marker.read_text())
    record["path"] = str(sandbox.paths.root)
    marker.write_text(json.dumps(record))
    with pytest.raises(ClaimError, match="invalid workspace marker"):
        Pool(sandbox, "operator").inspect("retained")


def test_operation_lock_serializes_live_owner_and_releases_on_crash(sandbox):
    pool = Pool(sandbox, "departed")
    slot = pool.acquire("retained")
    evidence = b"\x00unharvested\xff\r\n"
    (slot.path / "evidence.bin").write_bytes(evidence)
    ctx = multiprocessing.get_context("spawn")
    owner_connection, child_owner = ctx.Pipe()
    waiter_connection, child_waiter = ctx.Pipe()
    owner = ctx.Process(target=_hold_operation_lock,
                        args=(sandbox.paths.root, child_owner))
    waiter = ctx.Process(target=_recover_and_reacquire,
                         args=(sandbox.paths.root, child_waiter))
    owner.start()
    try:
        assert owner_connection.poll(10), "lock holder did not start"
        assert owner_connection.recv() == "locked"
        waiter.start()
        assert waiter_connection.poll(10), "waiter did not inspect"
        assert waiter_connection.recv() == "inspected"
        assert not waiter_connection.poll(0.3), "live lock owner was bypassed"
        assert slot.path.exists()
        owner.kill()
        owner.join(10)
        assert owner.exitcode is not None
        assert waiter_connection.poll(10), "crashed owner wedged recovery"
        receipt = waiter_connection.recv()
        waiter.join(10)
        assert waiter.exitcode == 0
        assert (Path(receipt["archive"]) / "evidence.bin").read_bytes() == evidence
        assert pool.inspect("retained")["holder"] == "operator"
        assert slot.path.exists()
    finally:
        for process in (owner, waiter):
            if process.pid is not None:
                if process.is_alive():
                    process.kill()
                process.join(10)
        for connection in (owner_connection, child_owner, waiter_connection, child_waiter):
            connection.close()


@pytest.mark.parametrize("checkpoint", ["moved", "branch-rename", "bookkept"])
def test_interrupted_recovery_preserves_original_archive(sandbox, checkpoint):
    pool = Pool(sandbox, "departed")
    slot = pool.acquire("retained")
    evidence = b"\x00original\xffevidence\r\n"
    (slot.path / "evidence.bin").write_bytes(evidence)
    if checkpoint == "branch-rename":
        marker = pool.root / "retained.json"
        row = json.loads(marker.read_text())
        row.update(kind="worktree", branch="ar/retained")
        marker.write_text(json.dumps(row))
    inspection = pool.inspect("retained")
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_crash_during_recovery,
                          args=(sandbox.paths.root, checkpoint))
    process.start()
    try:
        process.join(10)
        assert process.exitcode == 71
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
    archives = list((pool.root / "recovered").iterdir())
    assert len(archives) == 1
    archive = archives[0]
    receipt_bytes = (archive / "receipt.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["token"] == inspection["token"]
    assert receipt["holder"] == "departed"
    assert (Path(receipt["archive"]) / "evidence.bin").read_bytes() == evidence
    recovery = Pool(sandbox, "retrying-operator")
    with pytest.raises(ClaimError) as error:
        recovery.recover("retained", holder="departed", token=inspection["token"],
                         confirm_inactive=True)
    assert str(archive) in str(error.value)
    with pytest.raises(ClaimError) as error:
        recovery.acquire("retained")
    assert str(archive) in str(error.value)
    assert list((pool.root / "recovered").iterdir()) == archives
    assert (archive / "receipt.json").read_bytes() == receipt_bytes
    assert (Path(receipt["archive"]) / "evidence.bin").read_bytes() == evidence
