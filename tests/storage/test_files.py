import errno
import json
from pathlib import Path

import pytest

from product_factory.storage import files
from product_factory.storage.files import atomic_write_json, contained_path


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
