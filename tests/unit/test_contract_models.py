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
from product_factory.contracts.identifiers import is_portable_path_component


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


@pytest.mark.parametrize("value", ["CONIN$", "clock$.txt", "COM¹.log", "LPT³", "e\u0301"])
def test_portable_identifier_rejects_windows_devices_and_non_nfc(value: str) -> None:
    assert not is_portable_path_component(value)


def test_portable_identifier_uses_a_schema_expressible_unicode_subset() -> None:
    assert is_portable_path_component("证据-01")
    assert not is_portable_path_component("é")
    assert not is_portable_path_component("é" * 128)
    assert not is_portable_path_component("a" * 86)


def test_workflow_collector_rejects_inputs_checked_as_implemented() -> None:
    from product_factory.services.recovery import _validate_workflow_invariants

    state = StateRecord(
        schema_version="1.0", project_id="demo", revision=0,
        workflow_state=WorkflowState.INPUTS_CHECKED,
        current_stage=CurrentStage(id="stage-01", sequence=1, completion_level=CompletionLevel.IMPLEMENTED),
        updated_at=datetime.now(timezone.utc),
    )
    findings: list[str] = []
    _validate_workflow_invariants(state, findings)
    assert findings == ["workflow_invariant_invalid", "workflow_revision_invalid"]


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_not_applicable_requirement_rejects_blank_reason(reason: str | None) -> None:
    with pytest.raises(ValidationError):
        RequirementDeclaration(
            status=RequirementStatus.NOT_APPLICABLE,
            source="产品负责人确认",
            reason=reason,
        )
