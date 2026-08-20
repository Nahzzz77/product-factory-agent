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


def test_repository_initialization_creates_empty_event_and_approval_logs(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path)

    assert repo.paths.approvals.read_text(encoding="utf-8") == ""
    assert repo.paths.events.read_text(encoding="utf-8") == ""


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
    original_exists = Path.exists
    both_checked_for_absence = threading.Barrier(2)
    start_together = threading.Barrier(2)

    def synchronized_exists(path: Path) -> bool:
        exists = original_exists(path)
        if path == target and not exists:
            both_checked_for_absence.wait(timeout=5)
        return exists

    monkeypatch.setattr(Path, "exists", synchronized_exists)

    def contender(record: EvidenceManifest) -> tuple[str, EvidenceManifest]:
        start_together.wait(timeout=5)
        try:
            repo.save_evidence(record)
        except FactoryError as exc:
            assert exc.code == "evidence_exists"
            return "error", record
        return "success", record

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(contender, manifests))

    winners = [record for status, record in outcomes if status == "success"]
    losers = [record for status, record in outcomes if status == "error"]
    assert len(winners) == 1
    assert len(losers) == 1
    assert repo.load_evidence("stage-01", "evidence-01") == winners[0]
