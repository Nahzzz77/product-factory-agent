"""Cross-platform, file-backed single-writer lease locks."""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from product_factory.contracts.models import LockOwner, LockRecord
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.storage.files import _fsync_parent_directory, atomic_write_json
from product_factory.storage.paths import ProjectPaths


_GUARD_LEASE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class TakeoverResult:
    """Replacement lease plus the event-safe facts that justified it."""

    lock: LockRecord
    details: dict[str, Any]

    @property
    def lock_id(self) -> str:
        """Compatibility shorthand for callers that only need the new lease ID."""
        return self.lock.lock_id

    def __iter__(self) -> Iterator[LockRecord | dict[str, Any]]:
        yield self.lock
        yield self.details


@dataclass(frozen=True, slots=True)
class _TakeoverGuard:
    guard_id: str
    owner: LockOwner
    acquired_at: datetime
    expires_at: datetime
    generation: int

    def payload(self) -> dict[str, Any]:
        return {
            "guard_id": self.guard_id,
            "owner": self.owner.model_dump(mode="json"),
            "acquired_at": self.acquired_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "generation": self.generation,
        }


@dataclass(frozen=True, slots=True)
class _LockTransition:
    """An immutable, fencing-ordered change to the authoritative lock state."""

    generation: int
    guard_id: str
    state: str
    record: LockRecord | None
    prior_lock_id: str | None
    prior_lease_expires_at: datetime | None

    def payload(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "guard_id": self.guard_id,
            "state": self.state,
            "record": None if self.record is None else self.record.model_dump(mode="json"),
            "prior_lock_id": self.prior_lock_id,
            "prior_lease_expires_at": (
                None if self.prior_lease_expires_at is None else self.prior_lease_expires_at.isoformat()
            ),
        }


class LockManager:
    """Own the durable lease file for one project root."""

    def __init__(self, root: Path, now_fn: Callable[[], datetime] | None = None):
        self.paths = ProjectPaths(root.resolve())
        self.path = self.paths.lock
        self.takeover_guard_dir = self.path.with_name("execution-lock.takeover-guards")
        self.lock_transition_dir = self.path.with_name("execution-lock-transitions")
        self.now = now_fn or (lambda: datetime.now(timezone.utc))

    def acquire(self, owner: LockOwner, state_revision: int, lease: timedelta) -> LockRecord:
        self._require_positive_lease(lease)
        with self._guard(owner) as guard:
            existing = self.status()
            if existing is not None:
                raise self._lock_held()
            record = self._new_record(owner, state_revision, lease)
            self._commit_transition(guard, "present", record, None, self._lock_held)
            return record

    def status(self) -> LockRecord | None:
        transition = self._latest_transition()
        if transition is None:
            return None
        self._synchronize_projection(transition)
        return transition.record

    def require(self, lock_id: str, expected_revision: int) -> LockRecord:
        record = self.status()
        if record is None or record.lock_id != lock_id:
            raise FactoryError(
                "lock_required",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "需要有效执行锁",
                "lock",
                True,
                "先运行 lock acquire",
            )
        if record.lease_expires_at <= self._now():
            raise FactoryError(
                "lock_expired",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "执行锁已过期",
                "lock",
                True,
                "重新获取或显式接管",
            )
        if record.state_revision != expected_revision:
            raise FactoryError(
                "lock_revision_mismatch",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "执行锁绑定了旧状态",
                "lock",
                True,
                "释放后重新获取",
            )
        return record

    def heartbeat(self, lock_id: str, lease: timedelta) -> LockRecord:
        self._require_positive_lease(lease)
        with self._guard(self._guard_owner(lock_id)) as guard:
            record = self._matching_record(lock_id, "lock heartbeat")
            if record.lease_expires_at <= self._now():
                raise FactoryError(
                    "lock_expired",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "执行锁已过期",
                    "lock heartbeat",
                    True,
                    "重新获取或显式接管",
                )
            now = self._now()
            refreshed = record.model_copy(
                update={"heartbeat_at": now, "lease_expires_at": now + lease}
            )
            self._commit_transition(
                guard,
                "present",
                refreshed,
                record,
                lambda: self._owner_mismatch("lock heartbeat"),
            )
            return refreshed

    def release(self, lock_id: str) -> None:
        with self._guard(self._guard_owner(lock_id)) as guard:
            record = self._matching_record(lock_id, "lock release")
            self._commit_transition(
                guard,
                "released",
                None,
                record,
                lambda: self._owner_mismatch("lock release"),
            )

    def takeover(
        self,
        old_lock_id: str,
        owner: LockOwner,
        state_revision: int,
        reason: str,
        lease: timedelta,
    ) -> TakeoverResult:
        self._require_positive_lease(lease)
        if not reason.strip():
            raise FactoryError(
                "takeover_reason_required",
                ErrorCategory.INPUT_REQUIRED,
                "接管执行锁必须提供原因",
                "lock takeover",
                False,
                "提供接管原因",
            )
        with self._guard(owner) as guard:
            old = self.status()
            if old is None or old.lock_id != old_lock_id:
                raise FactoryError(
                    "lock_required",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "需要指定的过期执行锁",
                    "lock takeover",
                    True,
                    "运行 lock status 后重试",
                )
            if old.lease_expires_at > self._now():
                raise FactoryError(
                    "lock_active",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "执行锁租约仍然有效",
                    "lock takeover",
                    True,
                    "等待租约过期或让原持有者释放",
                )

            # Revalidate while holding the guard before creating its fenced
            # transition.  The transition journal, not the projection pathname,
            # remains authoritative if this guard expires during the operation.
            current = self.status()
            if current is None or current.lock_id != old_lock_id:
                raise FactoryError(
                    "lock_required",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "指定的执行锁已改变",
                    "lock takeover",
                    True,
                    "运行 lock status 后重试",
                )
            if current.lease_expires_at != old.lease_expires_at:
                raise FactoryError(
                    "lock_changed",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "指定执行锁的租约已改变",
                    "lock takeover",
                    True,
                    "运行 lock status 后重试",
                )
            if current.lease_expires_at > self._now():
                raise FactoryError(
                    "lock_active",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "执行锁租约仍然有效",
                    "lock takeover",
                    True,
                    "等待租约过期或让原持有者释放",
                )
            replacement = self._new_record(owner, state_revision, lease)
            self._commit_transition(
                guard,
                "present",
                replacement,
                old,
                self._takeover_changed,
            )
            return TakeoverResult(
                lock=replacement,
                details={
                    "old_lock_id": old.lock_id,
                    "new_lock_id": replacement.lock_id,
                    "reason": reason,
                },
            )

    def _new_record(self, owner: LockOwner, state_revision: int, lease: timedelta) -> LockRecord:
        now = self._now()
        return LockRecord(
            schema_version="1.0",
            lock_id=uuid.uuid4().hex,
            owner=owner,
            acquired_at=now,
            heartbeat_at=now,
            lease_expires_at=now + lease,
            state_revision=state_revision,
        )

    def _matching_record(self, lock_id: str, step: str) -> LockRecord:
        record = self.status()
        if record is None or record.lock_id != lock_id:
            raise self._owner_mismatch(step)
        return record

    def _owner_mismatch(self, step: str) -> FactoryError:
        return FactoryError(
            "lock_owner_mismatch",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "执行锁不属于当前会话",
            step,
            False,
            "使用当前锁 ID，或等待租约过期后显式接管",
        )

    def _lock_held(self) -> FactoryError:
        return FactoryError(
            "lock_held",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "执行锁已被占用",
            "lock acquire",
            True,
            "运行 lock status，或在租约过期后显式接管",
        )

    def _takeover_changed(self) -> FactoryError:
        return FactoryError(
            "lock_changed",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "指定执行锁已改变",
            "lock takeover",
            True,
            "运行 lock status 后重试",
        )

    @contextmanager
    def _guard(self, owner: LockOwner) -> Iterator[_TakeoverGuard]:
        guard = self._acquire_guard(owner)
        try:
            yield guard
        finally:
            self._release_guard(guard)

    def _acquire_guard(self, owner: LockOwner) -> _TakeoverGuard:
        self.takeover_guard_dir.mkdir(parents=True, exist_ok=True)
        while True:
            now = self._now()
            latest = self._latest_guard()
            current = None if latest is None or self._retirement_path(latest).exists() else latest
            if current is not None and current.expires_at > now:
                raise self._takeover_in_progress()
            generation = 0 if latest is None else latest.generation + 1
            guard = _TakeoverGuard(uuid.uuid4().hex, owner, now, now + _GUARD_LEASE, generation)
            if self._publish_guard_exclusive(guard):
                return guard

    def _release_guard(self, guard: _TakeoverGuard) -> None:
        self._exclusive_create_payload(
            self._retirement_path(guard),
            {"generation": guard.generation, "guard_id": guard.guard_id},
        )

    def _read_guard_path(self, path: Path) -> _TakeoverGuard | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return self._parse_guard(payload)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError, ValueError) as exc:
            raise FactoryError(
                "takeover_guard_invalid",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "接管保护文件无效，已保留以避免误删",
                "lock takeover",
                True,
                "检查并移除损坏的接管保护文件",
            ) from exc

    def _parse_guard(self, payload: Any) -> _TakeoverGuard:
        if not isinstance(payload, dict):
            raise ValueError("guard must be a JSON object")
        guard_id = payload["guard_id"]
        if not isinstance(guard_id, str) or not guard_id:
            raise ValueError("guard_id must be a non-empty string")
        acquired_at = datetime.fromisoformat(payload["acquired_at"])
        expires_at = datetime.fromisoformat(payload["expires_at"])
        generation = payload["generation"]
        if acquired_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("guard timestamps must be timezone-aware")
        if not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        return _TakeoverGuard(
            guard_id=guard_id,
            owner=LockOwner.model_validate(payload["owner"]),
            acquired_at=acquired_at,
            expires_at=expires_at,
            generation=generation,
        )

    def _guard_path(self, generation: int) -> Path:
        return self.takeover_guard_dir / f"{generation:020d}.json"

    def _retirement_path(self, guard: _TakeoverGuard) -> Path:
        return self.takeover_guard_dir / f"{guard.generation:020d}.retired.json"

    def _publish_guard_exclusive(self, guard: _TakeoverGuard) -> bool:
        return self._exclusive_create_payload(self._guard_path(guard.generation), guard.payload())

    def _latest_guard(self) -> _TakeoverGuard | None:
        if not self.takeover_guard_dir.exists():
            return None
        candidates = [
            path
            for path in self.takeover_guard_dir.glob("*.json")
            if path.stem.isdigit()
        ]
        if not candidates:
            return None
        latest_path = max(candidates, key=lambda path: int(path.stem))
        return self._read_guard_path(latest_path)

    def _current_guard(self) -> _TakeoverGuard | None:
        latest = self._latest_guard()
        if latest is None or self._retirement_path(latest).exists():
            return None
        return latest

    def _commit_transition(
        self,
        guard: _TakeoverGuard,
        state: str,
        record: LockRecord | None,
        prior: LockRecord | None,
        conflict: Callable[[], FactoryError],
    ) -> _LockTransition:
        """Commit one mutation at this guard's immutable fencing generation."""
        if self._now() >= guard.expires_at:
            raise conflict()
        latest = self._latest_transition()
        if latest is not None and latest.generation >= guard.generation:
            raise conflict()
        transition = _LockTransition(
            generation=guard.generation,
            guard_id=guard.guard_id,
            state=state,
            record=record,
            prior_lock_id=None if prior is None else prior.lock_id,
            prior_lease_expires_at=None if prior is None else prior.lease_expires_at,
        )
        if not self._publish_transition_exclusive(transition):
            raise conflict()
        latest = self._latest_transition()
        if latest is None or latest.generation != guard.generation:
            raise conflict()
        self._synchronize_projection(latest)
        return transition

    def _transition_path(self, generation: int) -> Path:
        return self.lock_transition_dir / f"{generation:020d}.json"

    def _publish_transition_exclusive(self, transition: _LockTransition) -> bool:
        return self._exclusive_create_payload(
            self._transition_path(transition.generation), transition.payload()
        )

    def _latest_transition(self) -> _LockTransition | None:
        if not self.lock_transition_dir.exists():
            return None
        candidates = [
            path
            for path in self.lock_transition_dir.glob("*.json")
            if path.stem.isdigit()
        ]
        if not candidates:
            return None
        latest_path = max(candidates, key=lambda path: int(path.stem))
        return self._read_transition_path(latest_path)

    def _read_transition_path(self, path: Path) -> _LockTransition | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            transition = self._parse_transition(payload)
            if transition.generation != int(path.stem):
                raise ValueError("transition generation does not match its path")
            return transition
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FactoryError(
                "lock_unreadable",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "无法读取执行锁",
                "lock status",
                True,
                "检查项目目录权限",
            ) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError, ValueError) as exc:
            raise FactoryError(
                "lock_invalid",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "执行锁转换记录无效",
                "lock status",
                True,
                "检查并移除无效的执行锁转换记录",
            ) from exc

    def _parse_transition(self, payload: Any) -> _LockTransition:
        if not isinstance(payload, dict):
            raise ValueError("transition must be a JSON object")
        generation = payload["generation"]
        guard_id = payload["guard_id"]
        state = payload["state"]
        prior_lock_id = payload["prior_lock_id"]
        prior_lease_expires_at = payload["prior_lease_expires_at"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("transition generation must be a non-negative integer")
        if not isinstance(guard_id, str) or not guard_id:
            raise ValueError("transition guard_id must be a non-empty string")
        if state not in {"present", "released"}:
            raise ValueError("transition state is invalid")
        if (prior_lock_id is None) != (prior_lease_expires_at is None):
            raise ValueError("transition prior identity and expiry must occur together")
        if prior_lock_id is not None and not isinstance(prior_lock_id, str):
            raise ValueError("transition prior lock ID must be a string")
        if prior_lease_expires_at is None:
            prior_expiry = None
        else:
            prior_expiry = datetime.fromisoformat(prior_lease_expires_at)
            if prior_expiry.tzinfo is None:
                raise ValueError("transition prior expiry must be timezone-aware")
        if state == "present":
            record = LockRecord.model_validate(payload["record"])
        else:
            if payload["record"] is not None:
                raise ValueError("released transition cannot contain a lock record")
            record = None
        return _LockTransition(
            generation=generation,
            guard_id=guard_id,
            state=state,
            record=record,
            prior_lock_id=prior_lock_id,
            prior_lease_expires_at=prior_expiry,
        )

    def _synchronize_projection(self, transition: _LockTransition) -> None:
        """Best-effort, non-authoritative compatibility view of the journal state."""
        try:
            if transition.record is None:
                # The tombstone is already authoritative; this only removes the
                # compatibility projection and can never change status semantics.
                self.path.unlink(missing_ok=True)
                _fsync_parent_directory(self.path)
            else:
                atomic_write_json(self.path, transition.record.model_dump(mode="json"))
        except OSError:
            # The transition is durable and authoritative.  A later status call
            # retries projection calibration without changing lock semantics.
            return

    def _takeover_in_progress(self) -> FactoryError:
        return FactoryError(
            "takeover_in_progress",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "另一个会话正在变更执行锁",
            "lock takeover",
            True,
            "稍后重试",
        )

    def _exclusive_create_payload(self, path: Path, payload: dict[str, Any]) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise OSError("could not write lock file")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
            _fsync_parent_directory(path)
            return True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _guard_owner(self, lock_id: str) -> LockOwner:
        return LockOwner(
            tool="product-factory",
            session_id=lock_id,
            pid=os.getpid(),
            host=socket.gethostname(),
        )

    def _now(self) -> datetime:
        now = self.now()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _require_positive_lease(self, lease: timedelta) -> None:
        if lease <= timedelta(0):
            raise FactoryError(
                "lock_lease_invalid",
                ErrorCategory.INPUT_REQUIRED,
                "执行锁租约必须大于零",
                "lock",
                False,
                "提供大于零的租约时长",
            )
