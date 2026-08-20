import errno
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import yaml


_DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP})


def contained_path(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError("path is outside project root")
    return candidate


def read_contained_regular_bytes(root: Path, parts: tuple[str, ...]) -> bytes:
    """Read one project-contained regular file from the same verified descriptor.

    Callers supply already-normalized relative components.  POSIX opens every
    component from a root directory descriptor with ``O_NOFOLLOW``; the fallback
    retains descriptor/path identity checks for platforms without that primitive.
    """
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unsafe relative file path")
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*parts)
    try:
        resolved_before = candidate.resolve(strict=True)
        _require_contained(resolved_root, resolved_before)
        if os.name != "nt" and hasattr(os, "O_NOFOLLOW"):
            return _read_posix_contained_file(resolved_root, candidate, parts)
        return _read_fallback_contained_file(resolved_root, candidate)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("contained regular file is invalid or changed") from exc


def _read_posix_contained_file(root: Path, candidate: Path, parts: tuple[str, ...]) -> bytes:
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        before = candidate.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ValueError("symlink is not a regular file")
        _require_contained(root, candidate.resolve(strict=True))
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        for part in parts[:-1]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("not a regular file")
        content = _read_descriptor(file_fd)
        _require_path_identity(root, candidate, opened)
        return content
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _read_fallback_contained_file(root: Path, candidate: Path) -> bytes:
    descriptor: int | None = None
    try:
        before = candidate.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ValueError("symlink is not a regular file")
        _require_contained(root, candidate.resolve(strict=True))
        descriptor = os.open(candidate, os.O_RDONLY)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("not a regular file")
        content = _read_descriptor(descriptor)
        _require_path_identity(root, candidate, opened)
        return content
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_path_identity(root: Path, candidate: Path, opened: os.stat_result) -> None:
    after = candidate.lstat()
    if stat.S_ISLNK(after.st_mode):
        raise ValueError("file changed to a symlink")
    followed = candidate.stat()
    if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or (
        opened.st_dev,
        opened.st_ino,
    ) != (followed.st_dev, followed.st_ino):
        raise ValueError("file changed while being read")
    _require_contained(root, candidate.resolve(strict=True))


def _require_contained(root: Path, candidate: Path) -> None:
    if candidate == root or root not in candidate.parents:
        raise ValueError("path is outside root")


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _fsync_parent_directory(path: Path) -> None:
    """Persist an atomic rename, except where directory fsync is unsupported."""
    directory_fd: int | None = None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        os.fsync(directory_fd)
    except OSError as exc:
        if directory_fd is None and os.name == "nt" and exc.errno in {errno.EACCES, errno.EPERM}:
            return
        if exc.errno not in _DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS:
            raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent_directory(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def exclusive_create_json(path: Path, payload: dict[str, Any]) -> bool:
    """Publish a complete JSON document once without replacing an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _fsync_parent_directory(path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one complete JSONL record, or preserve the complete previous file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.exists() else b""
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _write_all(descriptor, previous + line)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_parent_directory(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written == 0:
            raise OSError("could not write complete file")
        remaining = remaining[written:]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected mapping in {path}")
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
    return records
