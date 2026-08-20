from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from product_factory.contracts.models import (
    CompletionLevel,
    CurrentStage,
    IntakeRecord,
    RequirementDeclaration,
    RequirementStatus,
    StateRecord,
    WorkflowState,
)


def test_state_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StateRecord.model_validate(
            {
                "schema_version": "1.0",
                "project_id": "demo",
                "revision": 0,
                "workflow_state": "initialized",
                "current_stage": {"id": "stage-01", "sequence": 1, "completion_level": "none"},
                "waiting_on": None,
                "last_valid_evidence_id": None,
                "last_event_id": None,
                "updated_at": "2026-08-20T00:00:00Z",
                "unexpected": True,
            }
        )


def test_intake_requires_all_seven_categories() -> None:
    declaration = RequirementDeclaration(status=RequirementStatus.PRESENT, source="PRD §1")
    with pytest.raises(ValidationError):
        IntakeRecord(
            schema_version="1.0",
            project_id="demo",
            prd_confirmed=True,
            confirmed_by="owner",
            confirmed_at=datetime.now(timezone.utc),
            requirements={"target_user_and_core_task": declaration},
        )


def test_state_enum_values_match_protocol() -> None:
    state = StateRecord(
        schema_version="1.0",
        project_id="demo",
        revision=0,
        workflow_state=WorkflowState.INITIALIZED,
        current_stage=CurrentStage(id="stage-01", sequence=1, completion_level=CompletionLevel.NONE),
        updated_at=datetime.now(timezone.utc),
    )
    assert state.workflow_state.value == "initialized"
