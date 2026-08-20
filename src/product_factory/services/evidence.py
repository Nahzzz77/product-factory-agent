"""Immutable evidence manifests and reproducible source verification."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import yaml
from pydantic import ValidationError

from product_factory.contracts.models import CompletionLevel, EvidenceManifest, ProjectRecord, StateRecord, WorkflowState
from product_factory.domain.evidence import evaluate_evidence
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.storage.files import read_contained_regular_bytes
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


_STATIC_EXCLUDED_PARTS = frozenset(
    {".git", ".product-factory", ".venv", "build", "dist", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def compute_source_digest(root: Path, source_excludes: list[str]) -> str:
    """Hash a stable, contained snapshot of source files in protocol order.

    The source tree is enumerated twice.  A concurrent create, delete, rename,
    metadata update, or descriptor identity change therefore fails explicitly
    rather than publishing a digest assembled from two different tree states.
    """
    root = root.resolve()
    try:
        before = _source_snapshot(root, source_excludes)
        digest = hashlib.sha256()
        for relative, _signature in before:
            content = read_contained_regular_bytes(root, relative.parts)
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        after = _source_snapshot(root, source_excludes)
    except FactoryError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _unstable_digest_error() from exc
    if before != after:
        raise _unstable_digest_error()
    return digest.hexdigest()


def record_evidence(
    root: Path, authoring_path: Path, lock_id: str, expected_revision: int
) -> EvidenceManifest:
    """Validate authoring content, replace identity fields with current facts, and publish once."""
    root = root.resolve()
    manager = LockManager(root)
    with manager.mutation(lock_id, expected_revision):
        repo = ProjectRepository(root)
        project, state = _current_context(repo, expected_revision, "record_evidence")
        _require_recordable_state(state)
        authored = _load_authoring(root, authoring_path)
        manifest = authored.model_copy(
            update={
                "stage_id": state.current_stage.id,
                "state_revision": state.revision,
                "factory_version": project.factory_version,
                "prd_sha256": project.prd.sha256,
                "source_digest": compute_source_digest(root, project.source_excludes),
            }
        )
        repo.save_evidence(manifest)
        return manifest


def verify_stage(root: Path, evidence_id: str, lock_id: str, expected_revision: int) -> StateRecord:
    """Accept current evidence and atomically advance the implementation completion level."""
    root = root.resolve()
    manager = LockManager(root)
    with manager.mutation(lock_id, expected_revision):
        repo = ProjectRepository(root)
        project, state = _current_context(repo, expected_revision, "verify_stage")
        try:
            manifest = repo.load_evidence(state.current_stage.id, evidence_id)
        except (FileNotFoundError, OSError, ValueError, ValidationError) as exc:
            raise FactoryError(
                "evidence_missing",
                ErrorCategory.IMPLEMENTATION_FAILED,
                "找不到可验证的阶段证据",
                "verify_stage",
                True,
                "登记新的验证证据后重试",
                {"evidence_id": evidence_id},
            ) from exc
        reasons = evaluate_evidence(
            manifest, project, state, compute_source_digest(root, project.source_excludes)
        )
        if reasons:
            raise FactoryError(
                "evidence_invalid",
                ErrorCategory.IMPLEMENTATION_FAILED,
                "系统验证证据未通过",
                "verify_stage",
                True,
                "修复后重新登记证据",
                {"reasons": reasons},
            )
        # Import lazily: WorkflowService uses evidence_current as its normal
        # acceptance validator, while this operation needs its already-held helper.
        from product_factory.services.workflow import WorkflowService

        return WorkflowService(root)._mark_system_verified_locked(
            repo, state, evidence_id, expected_revision
        )


def evidence_current(
    repo: ProjectRepository, project: ProjectRecord, state: StateRecord, evidence_id: str
) -> bool:
    """Return whether an existing manifest still matches project facts right now."""
    try:
        manifest = repo.load_evidence(state.current_stage.id, evidence_id)
    except (FileNotFoundError, OSError, ValueError, ValidationError, FactoryError):
        return False
    return not evaluate_evidence(
        manifest, project, state, compute_source_digest(repo.paths.root, project.source_excludes)
    )


def _current_context(
    repo: ProjectRepository, expected_revision: int, step: str
) -> tuple[ProjectRecord, StateRecord]:
    project = repo.load_project()
    state = repo.load_state()
    if state.project_id != project.project_id:
        raise FactoryError(
            "project_identity_mismatch",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "项目状态与项目元数据的标识不一致",
            step,
            False,
            "修复项目元数据后重试",
        )
    if state.revision != expected_revision:
        raise FactoryError(
            "revision_conflict",
            ErrorCategory.ENVIRONMENT_BLOCKED,
            "状态已被其他会话修改",
            step,
            True,
            "重新运行 status 或 resume",
            {"expected": expected_revision, "actual": state.revision},
        )
    return project, state


def _require_recordable_state(state: StateRecord) -> None:
    if (
        state.workflow_state is not WorkflowState.SYSTEM_VERIFICATION
        or state.current_stage.completion_level is not CompletionLevel.IMPLEMENTED
        or state.waiting_on is not None
    ):
        raise FactoryError(
            "transition_not_allowed",
            ErrorCategory.POLICY_BLOCKED,
            "当前状态不能登记系统验证证据",
            "record_evidence",
            False,
            "先完成当前阶段实现并进入系统验证",
        )


def _load_authoring(root: Path, supplied: Path) -> EvidenceManifest:
    relative = _authoring_relative_path(root, supplied)
    try:
        raw = read_contained_regular_bytes(root, relative.parts)
        decoded = raw.decode("utf-8")
        if relative.suffix.lower() == ".json":
            payload = json.loads(decoded)
        else:
            payload = yaml.safe_load(decoded)
        if not isinstance(payload, dict):
            raise ValueError("evidence authoring must be a mapping")
        return EvidenceManifest.model_validate(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise FactoryError(
            "evidence_authoring_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "证据清单无效或无法安全读取",
            "record_evidence",
            False,
            "修正项目内的 YAML 或 JSON 证据清单后重试",
        ) from exc


def _authoring_relative_path(root: Path, supplied: Path) -> PurePosixPath:
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise FactoryError(
            "evidence_authoring_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "证据清单必须位于项目目录内",
            "record_evidence",
            False,
            "将证据清单放入项目目录后重试",
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise FactoryError(
            "evidence_authoring_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "证据清单路径无效",
            "record_evidence",
            False,
            "使用项目内的普通文件",
        )
    return PurePosixPath(*relative.parts)


def _source_snapshot(root: Path, patterns: list[str]) -> tuple[tuple[PurePosixPath, tuple[int, int, int, int, int]], ...]:
    if not root.is_dir():
        raise ValueError("source root is not a directory")
    if os.name != "nt" and hasattr(os, "O_NOFOLLOW"):
        return tuple(_walk_posix_snapshot(root, patterns))
    return tuple(_walk_fallback_snapshot(root, patterns))


def _walk_posix_snapshot(
    root: Path, patterns: list[str]
) -> Iterator[tuple[PurePosixPath, tuple[int, int, int, int, int]]]:
    root_fd: int | None = None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        yield from _walk_posix_directory(root_fd, PurePosixPath(), patterns)
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _walk_posix_directory(
    directory_fd: int, prefix: PurePosixPath, patterns: list[str]
) -> Iterator[tuple[PurePosixPath, tuple[int, int, int, int, int]]]:
    entries = sorted(list(os.scandir(directory_fd)), key=lambda item: item.name)
    for entry in entries:
        relative = prefix / entry.name
        if _excluded(relative, patterns):
            continue
        entry_stat = entry.stat(follow_symlinks=False)
        if stat.S_ISREG(entry_stat.st_mode):
            yield relative, _signature(entry_stat)
        elif stat.S_ISDIR(entry_stat.st_mode):
            child_fd: int | None = None
            try:
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                yield from _walk_posix_directory(child_fd, relative, patterns)
            finally:
                if child_fd is not None:
                    os.close(child_fd)


def _walk_fallback_snapshot(
    root: Path, patterns: list[str]
) -> Iterator[tuple[PurePosixPath, tuple[int, int, int, int, int]]]:
    def walk(directory: Path, prefix: PurePosixPath) -> Iterator[tuple[PurePosixPath, tuple[int, int, int, int, int]]]:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = prefix / entry.name
            if _excluded(relative, patterns):
                continue
            item_stat = entry.lstat()
            if stat.S_ISREG(item_stat.st_mode):
                resolved = entry.resolve(strict=True)
                if root != resolved and root not in resolved.parents:
                    raise ValueError("source file escaped root")
                yield relative, _signature(item_stat)
            elif stat.S_ISDIR(item_stat.st_mode):
                resolved = entry.resolve(strict=True)
                if root != resolved and root not in resolved.parents:
                    raise ValueError("source directory escaped root")
                yield from walk(entry, relative)

    yield from walk(root, PurePosixPath())


def _excluded(relative: PurePosixPath, patterns: list[str]) -> bool:
    parts = relative.parts
    if any(part in _STATIC_EXCLUDED_PARTS for part in parts):
        return True
    if any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in parts):
        return True
    value = relative.as_posix()
    return any(fnmatch.fnmatchcase(value, pattern) or relative.match(pattern) for pattern in patterns)


def _signature(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)


def _unstable_digest_error() -> FactoryError:
    return FactoryError(
        "source_digest_unstable",
        ErrorCategory.ENVIRONMENT_BLOCKED,
        "源码在计算摘要时发生变化或包含不安全文件",
        "source_digest",
        True,
        "停止并发写入后重新计算摘要",
    )
