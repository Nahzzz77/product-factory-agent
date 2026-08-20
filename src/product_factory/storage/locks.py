"""Cross-platform, file-backed single-writer lease locks."""

from __future__ import annotations

import json
import os
import sqlite3
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


class LockManager:
    """Own the canonical lease file with a local SQLite mutation mutex."""

    def __init__(self, root: Path, now_fn: Callable[[], datetime] | None = None):
        self.paths = ProjectPaths(root.resolve())
        self.path = self.paths.lock
        self.mutex_path = self.path.with_name("execution-lock.mutex.sqlite3")
        self.now = now_fn or (lambda: datetime.now(timezone.utc))

    def acquire(self, owner: LockOwner, state_revision: int, lease: timedelta) -> LockRecord:
        self._require_positive_lease(lease)
        with self._mutation_mutex():
            if self.status() is not None:
                raise self._lock_held()
            record = self._new_record(owner, state_revision, lease)
            if not self._exclusive_create_record(self.path, record):
                raise self._lock_held()
            return record

    def status(self) -> LockRecord | None:
        """Read only the canonical JSON lock; this never creates a sidecar."""
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as exc:
            raise FactoryError(
                "lock_invalid",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "执行锁文件无效",
                "lock status",
                True,
                "释放或显式接管执行锁",
            ) from exc
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

    @contextmanager
    def mutation(self, lock_id: str, expected_revision: int) -> Iterator[LockRecord]:
        """Fence a complete business-state mutation with the canonical lease mutex.

        The lock identity and bound revision are checked only after the SQLite
        mutex is held, so lease lifecycle operations cannot interleave with the
        caller's state snapshot, write, or audit append.
        """
        with self._mutation_mutex():
            yield self.require(lock_id, expected_revision)

    def heartbeat(self, lock_id: str, lease: timedelta) -> LockRecord:
        self._require_positive_lease(lease)
        with self._mutation_mutex():
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
            self._replace_record(refreshed)
            return refreshed

    def release(self, lock_id: str) -> None:
        with self._mutation_mutex():
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
        with self._mutation_mutex():
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
                raise self._takeover_changed()
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
            self._replace_expired_lock(replacement)
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

    @contextmanager
    def _mutation_mutex(self) -> Iterator[None]:
        """Serialize canonical-file mutations with a crash-recoverable SQLite lock."""
        connection: sqlite3.Connection | None = None
        try:
            self.paths.metadata.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.mutex_path, timeout=0, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise self._mutex_error(exc) from exc
        try:
            connection.execute("BEGIN IMMEDIATE")
        except (OSError, sqlite3.Error) as exc:
            try:
                connection.close()
            except (OSError, sqlite3.Error):
                pass
            raise self._mutex_error(exc) from exc

        primary_error = False
        try:
            yield
        except BaseException:
            primary_error = True
            try:
                connection.rollback()
            except (OSError, sqlite3.Error):
                pass
            raise
        else:
            try:
                connection.commit()
            except (OSError, sqlite3.Error) as exc:
                primary_error = True
                raise self._mutex_error(exc) from exc
        finally:
            try:
                connection.close()
            except (OSError, sqlite3.Error) as exc:
                if not primary_error:
                    raise self._mutex_error(exc) from exc

    def _is_mutex_busy(self, exc: BaseException) -> bool:
        return isinstance(exc, sqlite3.OperationalError) and (
            "locked" in str(exc).lower() or "busy" in str(exc).lower()
        )

    def _mutex_error(self, exc: BaseException) -> FactoryError:
        return self._lock_busy() if self._is_mutex_busy(exc) else self._mutex_unavailable()

    def _exclusive_create_record(self, path: Path, record: LockRecord) -> bool:
        return self._exclusive_create_payload(path, record.model_dump(mode="json"))

    def _replace_record(self, record: LockRecord) -> None:
        atomic_write_json(self.path, record.model_dump(mode="json"))

    def _replace_expired_lock(self, replacement: LockRecord) -> None:
        try:
            self.path.unlink()
            _fsync_parent_directory(self.path)
        except FileNotFoundError as exc:
            raise self._takeover_changed() from exc
        if not self._exclusive_create_record(self.path, replacement):
            raise self._lock_held()

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

    def _lock_busy(self) -> FactoryError:
        return FactoryError(
            "lock_busy",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "另一个会话正在变更执行锁",
            "lock mutation",
            True,
            "稍后重试",
        )

    def _mutex_unavailable(self) -> FactoryError:
        return FactoryError(
            "lock_mutex_unavailable",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "无法获取本地执行锁互斥器",
            "lock mutation",
            True,
            "检查项目目录权限",
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
