from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from product_factory.contracts.models import LockOwner
from product_factory.errors import FactoryError
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
    manager.takeover_guard_path.write_text(
        '{"guard_id":"stale","owner":{"tool":"codex","session_id":"z","pid":1,"host":"mac"},'
        '"acquired_at":"2026-08-20T00:00:00+00:00","expires_at":"2026-08-20T00:00:01+00:00"}\n',
        encoding="utf-8",
    )
    current[0] += timedelta(seconds=2)

    result = manager.takeover(
        old.lock_id,
        owner("b"),
        0,
        "stale guard recovered",
        timedelta(minutes=5),
    )

    assert manager.status() == result.lock
    assert not manager.takeover_guard_path.exists()
