import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from product_factory.contracts.models import (
    CompletionLevel,
    CurrentStage,
    EvidenceCheck,
    EvidenceManifest,
    StateRecord,
    WorkflowState,
)
from product_factory.errors import FactoryError
from product_factory.storage import repository as repository_module
from product_factory.storage.repository import ProjectRepository


def make_state(revision: int) -> StateRecord:
    return StateRecord(
        schema_version="1.0",
        project_id="demo",
        revision=revision,
        workflow_state=WorkflowState.INITIALIZED,
        current_stage=CurrentStage(id="stage-01", sequence=1, completion_level=CompletionLevel.NONE),
        updated_at=datetime.now(timezone.utc),
    )


def make_evidence(source_digest: str) -> EvidenceManifest:
    now = datetime.now(timezone.utc)
    return EvidenceManifest(
        schema_version="1.0",
        evidence_id="evidence-01",
        stage_id="stage-01",
        state_revision=1,
        factory_version="0.1.0",
        prd_sha256="a" * 64,
        source_digest=source_digest,
        checks=[
            EvidenceCheck(
                name="unit",
                command="pytest",
                started_at=now,
                ended_at=now,
                exit_status=0,
                summary="passed",
                mode="real",
            )
        ],
        ready_for_human_acceptance=True,
    )


def test_save_state_rejects_stale_expected_revision(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path)
    repo.write_initial_state(make_state(0))

    with pytest.raises(FactoryError) as caught:
        repo.save_state(make_state(2), expected_revision=1)

    assert caught.value.code == "revision_conflict"


@pytest.mark.parametrize("evidence_id", ["CON", "con.txt", "evidence:01", "name.", "trailing "])
def test_evidence_identifiers_are_portable_path_components(tmp_path: Path, evidence_id: str) -> None:
    repo = ProjectRepository(tmp_path)
    with pytest.raises(FactoryError) as caught:
        repo.evidence_path("stage-01", evidence_id)
    assert caught.value.code == "evidence_identifier_invalid"


def test_evidence_identifier_accepts_unicode_portable_name(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path)
    assert repo.evidence_path("stage-01", "证据-01").name == "manifest.json"


def test_repository_construction_is_read_only_for_missing_and_existing_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing-project"
    ProjectRepository(missing)
    assert not missing.exists()

    metadata = tmp_path / "existing/.product-factory"
    metadata.mkdir(parents=True)
    approval = metadata / "approvals.jsonl"
    event = metadata / "events.jsonl"
    approval.write_text("existing approval\n", encoding="utf-8")
    event.write_text("existing event\n", encoding="utf-8")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (approval, event)
    }

    repo = ProjectRepository(metadata.parent)

    assert repo.paths.approvals == approval
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (approval, event)
    } == before


def test_missing_log_reads_do_not_create_protocol_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repo = ProjectRepository(root)

    with pytest.raises(FileNotFoundError):
        repo.read_approvals()
    with pytest.raises(FileNotFoundError):
        repo.read_events()

    assert not repo.paths.metadata.exists()


def test_save_state_rejects_a_non_sequential_next_revision(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path)
    repo.write_initial_state(make_state(0))

    with pytest.raises(FactoryError) as caught:
        repo.save_state(make_state(2), expected_revision=0)

    assert caught.value.code == "revision_conflict"


def test_concurrent_evidence_reservation_preserves_only_the_winner(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path)
    manifests = [make_evidence("b" * 64), make_evidence("c" * 64)]
    start_together = threading.Barrier(2)

    def contender(record: EvidenceManifest) -> tuple[str, EvidenceManifest]:
        start_together.wait(timeout=5)
        try:
            repo.save_evidence(record)
        except FactoryError as exc:
            assert exc.code == "evidence_exists"
            return "error", record
        return "success", record

    with ThreadPoolExecutor(max_workers=2) as executor:
        contenders = [executor.submit(contender, record) for record in manifests]
        outcomes = [future.result() for future in contenders]

    winners = [record for status, record in outcomes if status == "success"]
    losers = [record for status, record in outcomes if status == "error"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert repo.load_evidence("stage-01", "evidence-01") == winners[0]


@pytest.mark.parametrize("contents", [None, "foreign"])
def test_existing_evidence_directory_is_never_reused(tmp_path: Path, contents: str | None) -> None:
    repo = ProjectRepository(tmp_path)
    directory = repo.evidence_path("stage-01", "evidence-01").parent
    directory.mkdir(parents=True)
    if contents is not None:
        (directory / "foreign.txt").write_text(contents, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    with pytest.raises(FactoryError) as caught:
        repo.save_evidence(make_evidence("b" * 64))

    assert caught.value.code == "evidence_exists"
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_failed_manifest_publication_keeps_the_evidence_id_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ProjectRepository(tmp_path)
    directory = repo.evidence_path("stage-01", "evidence-01").parent
    events: list[str] = []
    synced: list[Path] = []
    original_mkdir = repository_module.os.mkdir
    original_fsync_parent = repository_module._fsync_parent_directory

    def track_reservation_mkdir(path: str | Path, *args: object, **kwargs: object) -> None:
        original_mkdir(path, *args, **kwargs)
        if Path(path) == directory:
            events.append("mkdir")

    def record_reservation_sync(path: Path) -> None:
        if path == directory:
            original_fsync_parent(path)
            synced.append(path)
            events.append("fsync")
            return
        original_fsync_parent(path)

    def fail_publication(*_args: object, **_kwargs: object) -> None:
        events.append("write")
        raise OSError("disk full")

    monkeypatch.setattr(repository_module.os, "mkdir", track_reservation_mkdir)
    monkeypatch.setattr(repository_module, "_fsync_parent_directory", record_reservation_sync)
    monkeypatch.setattr(repository_module, "atomic_write_json", fail_publication)
    with pytest.raises(OSError):
        repo.save_evidence(make_evidence("b" * 64))
    assert synced == [directory]
    assert events == ["mkdir", "fsync", "write"]
    assert directory.is_dir()
    assert not (directory / "manifest.json").exists()
    with pytest.raises(FactoryError) as caught:
        repo.save_evidence(make_evidence("c" * 64))
    assert caught.value.code == "evidence_exists"


def test_evidence_reservation_syncs_parent_before_successful_manifest_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ProjectRepository(tmp_path)
    directory = repo.evidence_path("stage-01", "evidence-01").parent
    events: list[str] = []
    synced: list[Path] = []
    original_mkdir = repository_module.os.mkdir
    original_fsync_parent = repository_module._fsync_parent_directory
    original_atomic_write = repository_module.atomic_write_json

    def track_reservation_mkdir(path: str | Path, *args: object, **kwargs: object) -> None:
        original_mkdir(path, *args, **kwargs)
        if Path(path) == directory:
            events.append("mkdir")

    def track_reservation_sync(path: Path) -> None:
        if path == directory:
            events.append("fsync")
            synced.append(path)
        original_fsync_parent(path)

    def track_manifest_write(path: Path, *args: object, **kwargs: object) -> None:
        if path == directory / "manifest.json":
            events.append("write")
        original_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(repository_module.os, "mkdir", track_reservation_mkdir)
    monkeypatch.setattr(repository_module, "_fsync_parent_directory", track_reservation_sync)
    monkeypatch.setattr(repository_module, "atomic_write_json", track_manifest_write)

    path = repo.save_evidence(make_evidence("b" * 64))

    assert synced == [path.parent]
    assert events == ["mkdir", "fsync", "write"]
    assert path.is_file()


def test_evidence_reservation_sync_failure_propagates_and_keeps_id_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ProjectRepository(tmp_path)
    directory = repo.evidence_path("stage-01", "evidence-01").parent
    events: list[str] = []
    original_mkdir = repository_module.os.mkdir

    def track_reservation_mkdir(path: str | Path, *args: object, **kwargs: object) -> None:
        original_mkdir(path, *args, **kwargs)
        if Path(path) == directory:
            events.append("mkdir")

    def fail_reservation_sync(path: Path) -> None:
        assert path == directory
        events.append("fsync")
        raise OSError("directory sync failed")

    def should_not_write(*_args: object, **_kwargs: object) -> None:
        events.append("write")

    monkeypatch.setattr(repository_module.os, "mkdir", track_reservation_mkdir)
    monkeypatch.setattr(repository_module, "_fsync_parent_directory", fail_reservation_sync)
    monkeypatch.setattr(repository_module, "atomic_write_json", should_not_write)
    with pytest.raises(OSError, match="directory sync failed"):
        repo.save_evidence(make_evidence("b" * 64))
    assert events == ["mkdir", "fsync"]
    assert directory.is_dir()
    assert not (directory / "manifest.json").exists()
    with pytest.raises(FactoryError) as caught:
        repo.save_evidence(make_evidence("c" * 64))
    assert caught.value.code == "evidence_exists"
