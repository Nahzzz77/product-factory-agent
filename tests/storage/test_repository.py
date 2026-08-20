from datetime import datetime, timezone
from pathlib import Path

import pytest

from product_factory.contracts.models import CompletionLevel, CurrentStage, StateRecord, WorkflowState
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
