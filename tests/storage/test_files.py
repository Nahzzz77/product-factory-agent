import errno
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from product_factory.errors import FactoryError
from product_factory.storage import files
from product_factory.storage.files import append_jsonl, atomic_write_json, contained_path, read_contained_regular_bytes


def test_atomic_json_leaves_complete_document(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    atomic_write_json(target, {"revision": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"revision": 1}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_contained_path_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside project root"):
        contained_path(tmp_path, "../secret.txt")


def test_parent_directory_fsync_tolerates_unsupported_platform_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory_fd = 91
    monkeypatch.setattr(files.os, "open", lambda path, flags: directory_fd)
    monkeypatch.setattr(files.os, "close", lambda fd: None)
    monkeypatch.setattr(
        files.os,
        "fsync",
        lambda fd: (_ for _ in ()).throw(OSError(errno.EINVAL, "unsupported")),
    )

    files._fsync_parent_directory(tmp_path / "state.json")


def test_parent_directory_fsync_propagates_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory_fd = 92
    monkeypatch.setattr(files.os, "open", lambda path, flags: directory_fd)
    monkeypatch.setattr(files.os, "close", lambda fd: None)
    monkeypatch.setattr(
        files.os,
        "fsync",
        lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")),
    )

    with pytest.raises(OSError) as caught:
        files._fsync_parent_directory(tmp_path / "state.json")

    assert caught.value.errno == errno.EIO


def test_parent_directory_fsync_tolerates_windows_directory_open_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files.os, "name", "nt")
    monkeypatch.setattr(
        files.os,
        "open",
        lambda path, flags: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")),
    )

    files._fsync_parent_directory(tmp_path / "state.json")


def test_parent_directory_fsync_keeps_non_windows_permission_error_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files.os, "name", "posix")
    monkeypatch.setattr(
        files.os,
        "open",
        lambda path, flags: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")),
    )

    with pytest.raises(PermissionError) as caught:
        files._fsync_parent_directory(tmp_path / "state.json")

    assert caught.value.errno == errno.EACCES


def test_fallback_file_snapshot_uses_binary_flags_and_preserves_raw_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = b"CRLF\r\ncontrol\x1a\xff\x00\n"
    (root / "raw.bin").write_bytes(payload)
    binary_flag = 0x40000000
    original_open = files.os.open
    opened_flags: list[int] = []

    def spy_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        opened_flags.append(flags)
        return original_open(path, flags & ~binary_flag, *args, **kwargs)

    monkeypatch.setattr(files.os, "name", "nt")
    monkeypatch.setattr(files.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(files.os, "open", spy_open)

    snapshot = read_contained_regular_bytes(root, ("raw.bin",))
    assert snapshot == payload
    assert hashlib.sha256(snapshot).hexdigest() == hashlib.sha256(payload).hexdigest()
    assert any(flags & binary_flag for flags in opened_flags)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="requires POSIX FIFOs and nonblocking open",
)
def test_fifo_snapshot_is_rejected_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(root / "pipe")
    result: list[BaseException | None] = []

    def read_fifo() -> None:
        try:
            read_contained_regular_bytes(root, ("pipe",))
        except BaseException as exc:
            result.append(exc)
        else:  # pragma: no cover - assertion below explains the failure.
            result.append(None)

    worker = threading.Thread(target=read_fifo)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert time.monotonic() - started < 1
    assert len(result) == 1
    assert isinstance(result[0], ValueError)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="requires POSIX FIFOs and nonblocking open",
)
def test_fifo_replacement_after_precheck_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    entry = root / "entry"
    entry.write_bytes(b"regular")
    original_open = files.os.open
    replaced = False

    def replace_before_final_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal replaced
        if not replaced and path == "entry" and "dir_fd" in kwargs:
            replaced = True
            entry.unlink()
            os.mkfifo(entry)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", replace_before_final_open)
    result: list[BaseException | None] = []

    def read_replaced_file() -> None:
        try:
            read_contained_regular_bytes(root, ("entry",))
        except BaseException as exc:
            result.append(exc)
        else:  # pragma: no cover - assertion below explains the failure.
            result.append(None)

    worker = threading.Thread(target=read_replaced_file)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert time.monotonic() - started < 1
    assert isinstance(result[0], ValueError)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_append_jsonl_rejects_fifo_and_symlinks_without_following(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    os.mkfifo(target)
    with pytest.raises(ValueError, match="regular file"):
        append_jsonl(target, {"event": "blocked"})

    target.unlink()
    outside = tmp_path.parent / "outside-events.jsonl"
    outside.write_bytes(b"outside\n")
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        append_jsonl(target, {"event": "blocked"})
    assert outside.read_bytes() == b"outside\n"

    target.unlink()
    target.symlink_to(tmp_path / "missing-target")
    with pytest.raises(ValueError, match="regular file"):
        append_jsonl(target, {"event": "blocked"})


@pytest.mark.parametrize("initial", [b'{"event":"old"}\n', None])
def test_append_jsonl_rolls_back_after_post_replace_parent_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: bytes | None
) -> None:
    target = tmp_path / "events.jsonl"
    if initial is not None:
        target.write_bytes(initial)
    calls = 0
    original = files._fsync_parent_directory

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "injected parent fsync failure")
        original(path)

    monkeypatch.setattr(files, "_fsync_parent_directory", fail_once)
    with pytest.raises(OSError) as caught:
        append_jsonl(target, {"event": "new"})
    assert caught.value.errno == errno.EIO
    assert target.exists() is (initial is not None)
    assert target.read_bytes() == initial if initial is not None else not target.exists()

    append_jsonl(target, {"event": "new"})
    expected = (initial or b"") + b'{"event": "new"}\n'
    assert target.read_bytes() == expected


def test_append_jsonl_preserves_rollback_scratch_when_restore_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "events.jsonl"
    old = b'{"event":"old"}\n'
    target.write_bytes(old)
    original_replace = files.os.replace
    original_fsync_parent = files._fsync_parent_directory
    parent_fsync_calls = 0

    def fail_first_parent_fsync(path: Path) -> None:
        nonlocal parent_fsync_calls
        parent_fsync_calls += 1
        if parent_fsync_calls == 1:
            raise OSError(errno.EIO, "injected publish fsync failure")

    def deny_rollback_replace(source: object, destination: object) -> None:
        if str(source).endswith(".rollback"):
            raise OSError(errno.EACCES, "injected rollback replace denial")
        original_replace(source, destination)

    monkeypatch.setattr(files, "_fsync_parent_directory", fail_first_parent_fsync)
    monkeypatch.setattr(files.os, "replace", deny_rollback_replace)

    with pytest.raises(FactoryError) as caught:
        append_jsonl(target, {"event": "new"})

    assert caught.value.code == "audit_rollback_failed"
    rollback = tmp_path / caught.value.details["rollback_path"]
    assert rollback.is_file()
    assert rollback.read_bytes() == old
    # The new complete event may already be canonical.  The retained scratch
    # gives an operator one unambiguous way to restore the old complete log.
    assert target.read_bytes() == b'{"event":"old"}\n{"event": "new"}\n'
    original_replace(rollback, target)
    assert target.read_bytes() == old
    monkeypatch.setattr(files, "_fsync_parent_directory", original_fsync_parent)
    monkeypatch.setattr(files.os, "replace", original_replace)
    append_jsonl(target, {"event": "new"})
    assert target.read_bytes() == b'{"event":"old"}\n{"event": "new"}\n'


def test_append_jsonl_reports_restored_but_unsynced_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "events.jsonl"
    old = b'{"event":"old"}\n'
    target.write_bytes(old)

    def fail_every_parent_fsync(path: Path) -> None:
        raise OSError(errno.EIO, "injected parent fsync failure")

    monkeypatch.setattr(files, "_fsync_parent_directory", fail_every_parent_fsync)
    with pytest.raises(FactoryError) as caught:
        append_jsonl(target, {"event": "new"})

    assert caught.value.code == "audit_rollback_fsync_failed"
    assert target.read_bytes() == old
    assert list(tmp_path.glob("*.rollback")) == []
