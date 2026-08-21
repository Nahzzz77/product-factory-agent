from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread, current_thread

import pytest

from product_factory.contracts.models import LockOwner, LockRecord
from product_factory.errors import FactoryError
from product_factory.storage import locks
from product_factory.storage.files import atomic_write_json
from product_factory.storage.locks import LockManager


def owner(session_id: str) -> LockOwner:
    return LockOwner(tool="codex", session_id=session_id, pid=1, host="mac")


def test_second_writer_is_blocked_until_lease_expires(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    manager = LockManager(tmp_path, now_fn=lambda: now)
    first = manager.acquire(owner("a"), 0, timedelta(minutes=5))

    with pytest.raises(FactoryError) as caught:
        manager.acquire(owner("b"), 0, timedelta(minutes=5))

    assert caught.value.code == "lock_held"
    assert manager.status() == first


def test_heartbeat_extends_its_active_lease(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    lock = manager.acquire(owner("a"), 3, timedelta(seconds=10))
    current[0] += timedelta(seconds=4)

    refreshed = manager.heartbeat(lock.lock_id, timedelta(seconds=20))

    assert refreshed.lock_id == lock.lock_id
    assert refreshed.acquired_at == lock.acquired_at
    assert refreshed.heartbeat_at == current[0]
    assert refreshed.lease_expires_at == current[0] + timedelta(seconds=20)


def test_heartbeat_rejects_expired_lease(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    lock = manager.acquire(owner("a"), 0, timedelta(seconds=1))
    current[0] += timedelta(seconds=1)

    with pytest.raises(FactoryError) as caught:
        manager.heartbeat(lock.lock_id, timedelta(minutes=1))

    assert caught.value.code == "lock_expired"


def test_release_rejects_a_different_lock_id(tmp_path: Path) -> None:
    manager = LockManager(tmp_path)
    lock = manager.acquire(owner("a"), 0, timedelta(minutes=5))

    with pytest.raises(FactoryError) as caught:
        manager.release("another-lock")

    assert caught.value.code == "lock_owner_mismatch"
    assert manager.status() == lock


def test_require_checks_id_lease_and_state_revision(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    lock = manager.acquire(owner("a"), 3, timedelta(seconds=5))

    assert manager.require(lock.lock_id, 3) == lock

    with pytest.raises(FactoryError) as missing:
        manager.require("another-lock", 3)
    assert missing.value.code == "lock_required"

    with pytest.raises(FactoryError) as stale_revision:
        manager.require(lock.lock_id, 4)
    assert stale_revision.value.code == "lock_revision_mismatch"

    current[0] += timedelta(seconds=5)
    with pytest.raises(FactoryError) as expired:
        manager.require(lock.lock_id, 3)
    assert expired.value.code == "lock_expired"


def test_mutation_propagates_business_oserror_and_releases_the_mutex(tmp_path: Path) -> None:
    manager = LockManager(tmp_path)
    lock = manager.acquire(owner("a"), 0, timedelta(minutes=5))
    disk_failure = OSError("audit disk full")

    with pytest.raises(OSError) as caught:
        with manager.mutation(lock.lock_id, 0):
            raise disk_failure

    assert caught.value is disk_failure
    with manager.mutation(lock.lock_id, 0) as active:
        assert active == lock


def test_takeover_replaces_only_the_expired_expected_lock(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old = manager.acquire(owner("a"), 0, timedelta(seconds=1))
    current[0] += timedelta(seconds=2)

    result = manager.takeover(
        old.lock_id,
        owner("b"),
        4,
        "previous session ended",
        timedelta(minutes=5),
    )

    assert result.lock_id != old.lock_id
    assert result.lock.owner == owner("b")
    assert result.lock.state_revision == 4
    assert result.details == {
        "old_lock_id": old.lock_id,
        "new_lock_id": result.lock_id,
        "reason": "previous session ended",
    }
    assert manager.status() == result.lock


def test_takeover_rejects_active_lock_and_blank_reason(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    active = manager.acquire(owner("a"), 0, timedelta(minutes=5))

    with pytest.raises(FactoryError) as active_error:
        manager.takeover(active.lock_id, owner("b"), 0, "still running", timedelta(minutes=5))
    assert active_error.value.code == "lock_active"

    current[0] += timedelta(minutes=6)
    with pytest.raises(FactoryError) as reason_error:
        manager.takeover(active.lock_id, owner("b"), 0, "   ", timedelta(minutes=5))
    assert reason_error.value.code == "takeover_reason_required"


def test_concurrent_takeovers_never_remove_the_replacement_lock(tmp_path: Path) -> None:
    current = datetime(2026, 8, 20, tzinfo=timezone.utc)
    original = LockManager(tmp_path, now_fn=lambda: current).acquire(owner("a"), 0, timedelta(seconds=1))
    expired = current + timedelta(seconds=2)

    def contender(session_id: str):
        return LockManager(tmp_path, now_fn=lambda: expired).takeover(
            original.lock_id,
            owner(session_id),
            0,
            "previous session ended",
            timedelta(minutes=5),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(contender, session_id) for session_id in ("b", "c")]
        results = []
        errors = []
        for future in futures:
            try:
                results.append(future.result())
            except FactoryError as error:
                errors.append(error)

    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].code in {"lock_busy", "lock_required"}
    assert LockManager(tmp_path, now_fn=lambda: expired).status() == results[0].lock


def test_takeover_rejects_same_id_with_changed_expiry_inside_mutex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old = manager.acquire(owner("a"), 0, timedelta(seconds=1))
    current[0] += timedelta(seconds=2)
    original_status = manager.status
    reads = 0

    def status_with_changed_expiry():
        nonlocal reads
        reads += 1
        record = original_status()
        if reads == 2:
            assert record is not None
            changed = record.model_copy(
                update={"lease_expires_at": record.lease_expires_at - timedelta(microseconds=1)}
            )
            atomic_write_json(manager.path, changed.model_dump(mode="json"))
            return changed
        return record

    monkeypatch.setattr(manager, "status", status_with_changed_expiry)

    with pytest.raises(FactoryError) as caught:
        manager.takeover(old.lock_id, owner("b"), 0, "previous session ended", timedelta(minutes=5))

    assert caught.value.code == "lock_changed"
    assert original_status().lock_id == old.lock_id


def test_exclusive_lock_publication_never_exposes_partial_json_or_corrupts_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    manager = LockManager(tmp_path, now_fn=lambda: now)
    record = manager._new_record(owner("writer"), 0, timedelta(minutes=1))
    winner = manager._new_record(owner("winner"), 0, timedelta(minutes=1))
    partial_write_completed = Event()
    allow_writer_to_finish = Event()
    original_write = locks.os.write
    writes = 0
    outcome: list[bool] = []

    def pause_after_partial_write(descriptor: int, content: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            midpoint = max(1, len(content) // 2)
            written = original_write(descriptor, content[:midpoint])
            partial_write_completed.set()
            assert allow_writer_to_finish.wait(timeout=5)
            return written
        return original_write(descriptor, content)

    monkeypatch.setattr(locks.os, "write", pause_after_partial_write)
    writer = Thread(
        target=lambda: outcome.append(manager._exclusive_create_record(manager.path, record))
    )
    writer.start()
    assert partial_write_completed.wait(timeout=5)

    assert manager.status() is None
    assert manager._exclusive_create_record(manager.path, winner)
    assert manager.status() == winner

    allow_writer_to_finish.set()
    writer.join(timeout=5)
    assert not writer.is_alive()
    assert outcome == [False]
    assert manager.status() == winner
    assert list(manager.paths.metadata.glob("*.tmp")) == []


def test_mutation_mutex_blocks_expired_takeover_until_original_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old_lock = manager.acquire(owner("old"), 0, timedelta(seconds=1))
    current[0] += timedelta(seconds=2)
    old_commit_started = Event()
    allow_old_commit = Event()
    original_replace = manager._replace_expired_lock
    results = []
    errors = []

    def pause_old_replace(*args, **kwargs):
        if current_thread().name == "old-takeover":
            old_commit_started.set()
            assert allow_old_commit.wait(timeout=5)
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(manager, "_replace_expired_lock", pause_old_replace)

    def old_takeover() -> None:
        try:
            results.append(
                manager.takeover(
                    old_lock.lock_id,
                    owner("old-takeover"),
                    1,
                    "original owner ended",
                    timedelta(minutes=5),
                )
            )
        except FactoryError as error:
            errors.append(error)

    old = Thread(target=old_takeover, name="old-takeover")
    old.start()
    assert old_commit_started.wait(timeout=5)

    current[0] += timedelta(minutes=1)
    with pytest.raises(FactoryError) as blocked:
        manager.takeover(
            old_lock.lock_id,
            owner("successor"),
            2,
            "cannot pass mutex",
            timedelta(minutes=5),
        )
    assert blocked.value.code == "lock_busy"
    allow_old_commit.set()
    old.join(timeout=5)

    assert not old.is_alive()
    assert errors == []
    assert len(results) == 1
    assert manager.status() == results[0].lock
    assert LockRecord.model_validate_json(manager.path.read_text(encoding="utf-8")) == results[0].lock


def test_takeover_status_observes_old_or_new_lock_never_a_publication_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old = manager.acquire(owner("old"), 0, timedelta(seconds=1))
    current[0] += timedelta(seconds=2)
    entered_replace = Event()
    allow_replace = Event()
    original_write = locks.atomic_write_json
    result: list[object] = []

    def pause_atomic_replace(path: Path, payload: dict) -> None:
        entered_replace.set()
        assert allow_replace.wait(timeout=5)
        original_write(path, payload)

    monkeypatch.setattr(locks, "atomic_write_json", pause_atomic_replace)

    def take_over() -> None:
        result.append(manager.takeover(old.lock_id, owner("new"), 0, "expired", timedelta(minutes=1)))

    worker = Thread(target=take_over)
    worker.start()
    assert entered_replace.wait(timeout=5)
    assert manager.status() == old
    allow_replace.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert manager.status() == result[0].lock


def test_heartbeat_and_release_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    lock = manager.acquire(owner("old"), 0, timedelta(minutes=5))
    heartbeat_started = Event()
    allow_heartbeat = Event()
    original_replace = manager._replace_record
    errors: list[FactoryError] = []

    def pause_heartbeat_replace(*args, **kwargs):
        if current_thread().name == "heartbeat":
            heartbeat_started.set()
            assert allow_heartbeat.wait(timeout=5)
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(manager, "_replace_record", pause_heartbeat_replace)

    def heartbeat() -> None:
        try:
            manager.heartbeat(lock.lock_id, timedelta(minutes=10))
        except FactoryError as error:
            errors.append(error)

    worker = Thread(target=heartbeat, name="heartbeat")
    worker.start()
    assert heartbeat_started.wait(timeout=5)

    with pytest.raises(FactoryError) as blocked:
        manager.release(lock.lock_id)
    assert blocked.value.code == "lock_busy"
    allow_heartbeat.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert manager.status() is not None
    manager.release(lock.lock_id)
    assert manager.status() is None


def test_status_is_read_only_and_reads_a_legacy_canonical_lock(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    record = manager._new_record(owner("legacy"), 0, timedelta(minutes=5))
    atomic_write_json(manager.path, record.model_dump(mode="json"))
    content_before = manager.path.read_text(encoding="utf-8")
    file_mtime_before = manager.path.stat().st_mtime_ns
    directory_entries_before = sorted(path.name for path in manager.paths.metadata.iterdir())
    directory_mtime_before = manager.paths.metadata.stat().st_mtime_ns

    assert manager.status() == record

    assert manager.path.read_text(encoding="utf-8") == content_before
    assert manager.path.stat().st_mtime_ns == file_mtime_before
    assert sorted(path.name for path in manager.paths.metadata.iterdir()) == directory_entries_before
    assert manager.paths.metadata.stat().st_mtime_ns == directory_mtime_before
    assert not manager.mutex_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is a POSIX probe")
def test_status_rejects_fifo_and_outside_symlink_without_following_or_blocking(tmp_path: Path) -> None:
    manager = LockManager(tmp_path)
    manager.paths.metadata.mkdir(parents=True)
    os.mkfifo(manager.path)
    result: list[object] = []
    def check_fifo() -> None:
        try:
            manager.status()
        except FactoryError as error:
            result.append(error)
    worker = Thread(target=check_fifo)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert isinstance(result[0], FactoryError) and result[0].code == "lock_invalid"

    manager.path.unlink()
    outside = tmp_path.parent / "outside-lock.json"
    atomic_write_json(outside, manager._new_record(owner("outside"), 0, timedelta(minutes=1)).model_dump(mode="json"))
    manager.path.symlink_to(outside)
    with pytest.raises(FactoryError, match="执行锁文件无效") as caught:
        manager.status()
    assert caught.value.code == "lock_invalid"


def test_prepared_takeover_audit_failure_preserves_old_lock_before_publication(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old = manager.acquire(owner("old"), 0, timedelta(seconds=1))
    old_bytes = manager.path.read_bytes()
    current[0] += timedelta(seconds=2)

    def prepared(_old: LockRecord):
        def fail(_replacement: LockRecord) -> None:
            raise OSError("audit storage unavailable")
        return 7, fail

    with pytest.raises(OSError, match="audit storage unavailable"):
        manager.takeover(old.lock_id, owner("new"), 0, "recover", timedelta(minutes=1), prepared=prepared)
    assert manager.path.read_bytes() == old_bytes
    assert manager.status() == old


def test_mutex_is_released_when_holder_process_is_terminated(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[2] / "src"
    script = "\n".join(
        [
            "from pathlib import Path",
            "from product_factory.storage.locks import LockManager",
            "import sys, time",
            f"manager = LockManager(Path({str(tmp_path)!r}))",
            "with manager._mutation_mutex():",
            "    print('locked', flush=True)",
            "    time.sleep(30)",
        ]
    )
    environment = {**os.environ, "PYTHONPATH": str(source_root)}
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        cwd=source_root.parent,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "locked\n"
        process.terminate()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    manager = LockManager(tmp_path)
    record = manager.acquire(owner("recovered"), 0, timedelta(minutes=5))

    assert manager.status() == record
