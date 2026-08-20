from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest

from product_factory.contracts.models import LockOwner
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
    assert errors[0].code in {"lock_required", "takeover_in_progress"}
    assert LockManager(tmp_path, now_fn=lambda: expired).status() == results[0].lock


def test_expired_takeover_guard_is_recovered_before_takeover(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old = manager.acquire(owner("a"), 0, timedelta(seconds=1))
    stale = locks._TakeoverGuard(
        "stale",
        owner("stale"),
        current[0] - timedelta(minutes=1),
        current[0] - timedelta(seconds=1),
        generation=1,
    )
    assert manager._publish_guard_exclusive(stale)
    current[0] += timedelta(seconds=2)

    result = manager.takeover(
        old.lock_id,
        owner("b"),
        0,
        "stale guard recovered",
        timedelta(minutes=5),
    )

    assert manager.status() == result.lock


def test_takeover_rejects_same_id_with_changed_expiry_inside_guard(
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


def test_exclusive_guard_publication_never_exposes_partial_json_or_corrupts_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    manager = LockManager(tmp_path, now_fn=lambda: now)
    guard = locks._TakeoverGuard("guard", owner("writer"), now, now + timedelta(minutes=1), 0)
    winner = locks._TakeoverGuard("winner", owner("winner"), now, now + timedelta(minutes=1), 0)
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
        target=lambda: outcome.append(manager._publish_guard_exclusive(guard))
    )
    writer.start()
    assert partial_write_completed.wait(timeout=5)

    assert manager._read_guard_path(manager._guard_path(0)) is None
    assert manager._publish_guard_exclusive(winner)
    assert manager._read_guard_path(manager._guard_path(0)) == winner

    allow_writer_to_finish.set()
    writer.join(timeout=5)
    assert not writer.is_alive()
    assert outcome == [False]
    assert manager._read_guard_path(manager._guard_path(0)) == winner
    assert list(manager.takeover_guard_dir.glob("*.tmp")) == []


def test_expired_recoverer_cannot_replace_successor_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    stale = locks._TakeoverGuard(
        "stale",
        owner("stale"),
        current[0] - timedelta(minutes=1),
        current[0] - timedelta(seconds=1),
        generation=0,
    )
    assert manager._publish_guard_exclusive(stale)
    old_publish_started = Event()
    allow_old_publish = Event()
    original_publish = manager._publish_guard_exclusive
    results = []
    errors = []

    def pause_old_generation(guard: locks._TakeoverGuard) -> bool:
        if guard.generation == 1 and current_thread().name == "old-recoverer":
            old_publish_started.set()
            assert allow_old_publish.wait(timeout=5)
        return original_publish(guard)

    monkeypatch.setattr(manager, "_publish_guard_exclusive", pause_old_generation)

    def acquire(session_id: str) -> None:
        try:
            results.append(manager._acquire_guard(owner(session_id)))
        except FactoryError as error:
            errors.append(error)

    old = Thread(target=acquire, args=("old",), name="old-recoverer")
    old.start()
    assert old_publish_started.wait(timeout=5)

    current[0] += timedelta(minutes=1)
    successor = manager._acquire_guard(owner("successor"))
    allow_old_publish.set()
    old.join(timeout=5)

    assert not old.is_alive()
    assert results == []
    assert len(errors) == 1
    assert errors[0].code == "takeover_in_progress"
    assert manager._current_guard() == successor


def test_expired_takeover_cannot_commit_below_successor_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old_lock = manager.acquire(owner("old"), 0, timedelta(seconds=1))
    current[0] += timedelta(seconds=2)
    old_commit_started = Event()
    allow_old_commit = Event()
    original_commit = manager._commit_transition
    results = []
    errors = []

    def pause_old_commit(*args, **kwargs):
        guard = args[0]
        if guard.generation == 1 and current_thread().name == "old-takeover":
            old_commit_started.set()
            assert allow_old_commit.wait(timeout=5)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(manager, "_commit_transition", pause_old_commit)

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

    current[0] += timedelta(seconds=31)
    successor = manager.takeover(
        old_lock.lock_id,
        owner("successor"),
        2,
        "fence expired predecessor",
        timedelta(minutes=5),
    )
    assert manager.status() == successor.lock
    allow_old_commit.set()
    old.join(timeout=5)

    assert not old.is_alive()
    assert results == []
    assert len(errors) == 1
    assert errors[0].code == "lock_changed"
    assert manager.status() == successor.lock


@pytest.mark.parametrize("operation", ["heartbeat", "release"])
def test_late_low_generation_mutation_cannot_override_successor_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old_lock = manager.acquire(owner("old"), 0, timedelta(seconds=10))
    old_commit_started = Event()
    allow_old_commit = Event()
    original_commit = manager._commit_transition
    errors = []

    def pause_old_commit(*args, **kwargs):
        guard = args[0]
        if guard.generation == 1 and current_thread().name == f"old-{operation}":
            old_commit_started.set()
            assert allow_old_commit.wait(timeout=5)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(manager, "_commit_transition", pause_old_commit)

    def delayed_mutation() -> None:
        try:
            if operation == "heartbeat":
                manager.heartbeat(old_lock.lock_id, timedelta(minutes=5))
            else:
                manager.release(old_lock.lock_id)
        except FactoryError as error:
            errors.append(error)

    delayed = Thread(target=delayed_mutation, name=f"old-{operation}")
    delayed.start()
    assert old_commit_started.wait(timeout=5)

    current[0] += timedelta(seconds=31)
    successor = manager.takeover(
        old_lock.lock_id,
        owner("successor"),
        2,
        "fence expired predecessor",
        timedelta(minutes=5),
    )
    assert manager.status() == successor.lock
    allow_old_commit.set()
    delayed.join(timeout=5)

    assert not delayed.is_alive()
    assert len(errors) == 1
    assert errors[0].code == "lock_owner_mismatch"
    assert manager.status() == successor.lock


def test_status_uses_highest_transition_even_if_projection_is_stale(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    original = manager.acquire(owner("old"), 0, timedelta(seconds=10))
    current[0] += timedelta(seconds=1)
    refreshed = manager.heartbeat(original.lock_id, timedelta(minutes=5))

    atomic_write_json(manager.path, original.model_dump(mode="json"))

    assert manager.status() == refreshed
    assert manager.require(refreshed.lock_id, 0) == refreshed
