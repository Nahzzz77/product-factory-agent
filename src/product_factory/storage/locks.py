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


class LockManager:
    """Own the durable lease file for one project root."""

    def __init__(self, root: Path, now_fn: Callable[[], datetime] | None = None):
        self.paths = ProjectPaths(root.resolve())
        self.path = self.paths.lock
        self.takeover_guard_dir = self.path.with_name("execution-lock.takeover-guards")
        self.now = now_fn or (lambda: datetime.now(timezone.utc))

    def acquire(self, owner: LockOwner, state_revision: int, lease: timedelta) -> LockRecord:
        self._require_positive_lease(lease)
        with self._guard(owner):
            existing = self.status()
            if existing is not None:
                raise FactoryError(
                    "lock_held",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "执行锁已被占用",
                    "lock acquire",
                    True,
                    "运行 lock status，或在租约过期后显式接管",
                )
            record = self._new_record(owner, state_revision, lease)
            if not self._exclusive_create_record(self.path, record):
                raise FactoryError(
                    "lock_held",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "执行锁已被占用",
                    "lock acquire",
                    True,
                    "运行 lock status，或在租约过期后显式接管",
                )
            return record

    def status(self) -> LockRecord | None:
        try:
            content = self.path.read_text(encoding="utf-8")
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
        try:
            return LockRecord.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise FactoryError(
                "lock_invalid",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "执行锁文件无效",
                "lock status",
                True,
                "释放或显式接管执行锁",
            ) from exc

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
        with self._guard(self._guard_owner(lock_id)):
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
            atomic_write_json(self.path, refreshed.model_dump(mode="json"))
            return refreshed

    def release(self, lock_id: str) -> None:
        with self._guard(self._guard_owner(lock_id)):
            self._matching_record(lock_id, "lock release")
            try:
                self.path.unlink()
                _fsync_parent_directory(self.path)
            except FileNotFoundError as exc:
                raise self._owner_mismatch("lock release") from exc

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
        with self._guard(owner):
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

            # This is deliberately inside the exclusive takeover guard. A contender
            # must prove the same expired record is still present immediately before
            # unlinking it, so it cannot delete another contender's new lease.
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
            self.path.unlink()
            _fsync_parent_directory(self.path)
            replacement = self._new_record(owner, state_revision, lease)
            if not self._exclusive_create_record(self.path, replacement):
                raise FactoryError(
                    "lock_held",
                    ErrorCategory.ENVIRONMENT_BLOCKED,
                    "执行锁在接管期间被重新获取",
                    "lock takeover",
                    True,
                    "运行 lock status 后重试",
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

    @contextmanager
    def _guard(self, owner: LockOwner) -> Iterator[None]:
        guard = self._acquire_guard(owner)
        try:
            yield
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

    def _takeover_in_progress(self) -> FactoryError:
        return FactoryError(
            "takeover_in_progress",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "另一个会话正在变更执行锁",
            "lock takeover",
            True,
            "稍后重试",
        )

    def _exclusive_create_record(self, path: Path, record: LockRecord) -> bool:
        return self._exclusive_create_payload(path, record.model_dump(mode="json"))

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
