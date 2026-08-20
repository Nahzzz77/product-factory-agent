import os
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
from product_factory.storage import files
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


def test_concurrent_evidence_creation_preserves_only_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ProjectRepository(tmp_path)
    manifests = [make_evidence("b" * 64), make_evidence("c" * 64)]
    target = repo.evidence_path("stage-01", "evidence-01")
    original_open = files.os.open
    start_together = threading.Barrier(2)
    loser_reported = threading.Event()
    release_final_writer = threading.Event()

    def delay_final_file_write(path: str | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = original_open(path, flags, mode)
        if Path(path) == target and flags & os.O_EXCL:
            release_final_writer.wait(timeout=5)
        return descriptor

    monkeypatch.setattr(files.os, "open", delay_final_file_write)

    def contender(record: EvidenceManifest) -> tuple[str, EvidenceManifest]:
        start_together.wait(timeout=5)
        try:
            repo.save_evidence(record)
        except FactoryError as exc:
            assert exc.code == "evidence_exists"
            loser_reported.set()
            return "error", record
        return "success", record

    def read_after_loser_observes_existing_evidence() -> EvidenceManifest:
        assert loser_reported.wait(timeout=5)
        try:
            return repo.load_evidence("stage-01", "evidence-01")
        finally:
            release_final_writer.set()

    with ThreadPoolExecutor(max_workers=3) as executor:
        contenders = [executor.submit(contender, record) for record in manifests]
        reader = executor.submit(read_after_loser_observes_existing_evidence)
        outcomes = [future.result() for future in contenders]
        observed = reader.result()

    winners = [record for status, record in outcomes if status == "success"]
    losers = [record for status, record in outcomes if status == "error"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert observed == winners[0]
    assert repo.load_evidence("stage-01", "evidence-01") == winners[0]
