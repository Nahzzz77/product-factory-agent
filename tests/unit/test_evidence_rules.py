from datetime import UTC, datetime

from product_factory.contracts.models import (
    CompletionLevel,
    CurrentStage,
    EvidenceCheck,
    EvidenceManifest,
    KnownIssue,
    PrdReference,
    ProjectRecord,
    StagePlanItem,
    StateRecord,
    WorkflowState,
)
from product_factory.domain.evidence import evaluate_evidence


def _project() -> ProjectRecord:
    return ProjectRecord(
        schema_version="1.0",
        project_id="project-123",
        name="Factory test",
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
        factory_version="0.1.0",
        prd=PrdReference(path="prd.md", sha256="a" * 64),
        constraints_path="constraints.md",
        handbooks=[],
        stage_plan=[
            StagePlanItem(
                id="api-core",
                name="API core",
                sequence=1,
                requires_real_model=True,
            )
        ],
        source_excludes=[],
    )


def _state() -> StateRecord:
    return StateRecord(
        schema_version="1.0",
        project_id="project-123",
        revision=7,
        workflow_state=WorkflowState.SYSTEM_VERIFICATION,
        current_stage=CurrentStage(
            id="api-core",
            sequence=1,
            completion_level=CompletionLevel.SYSTEM_VERIFIED,
        ),
        updated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def _check(*, exit_status: int = 0, mode: str = "real") -> EvidenceCheck:
    return EvidenceCheck(
        name="pytest",
        command="pytest -q",
        started_at=datetime(2026, 8, 20, tzinfo=UTC),
        ended_at=datetime(2026, 8, 20, tzinfo=UTC),
        exit_status=exit_status,
        summary="passed" if exit_status == 0 else "failed",
        mode=mode,
    )


def _manifest(**overrides: object) -> EvidenceManifest:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "evidence_id": "evidence-123",
        "stage_id": "api-core",
        "state_revision": 7,
        "factory_version": "0.1.0",
        "prd_sha256": "a" * 64,
        "source_digest": "b" * 64,
        "checks": [_check()],
        "ready_for_human_acceptance": True,
    }
    values.update(overrides)
    return EvidenceManifest(**values)


def test_valid_evidence_has_no_invalidation_reasons() -> None:
    """A matching real-model check must leave evidence usable for approval."""
    assert evaluate_evidence(_manifest(), _project(), _state(), "b" * 64) == []


def test_evidence_reasons_are_complete_and_stably_ordered() -> None:
    """Broken evidence must report each user-actionable invalidation in protocol order."""
    manifest = _manifest(
        stage_id="web-ui",
        state_revision=8,
        factory_version="0.2.0",
        prd_sha256="c" * 64,
        source_digest="d" * 64,
        checks=[_check(exit_status=1, mode="mock")],
        known_issues=[KnownIssue(summary="security issue", severity="high", blocking=True)],
        ready_for_human_acceptance=False,
    )

    assert evaluate_evidence(manifest, _project(), _state(), "b" * 64) == [
        "stage_mismatch",
        "future_revision",
        "factory_version_mismatch",
        "prd_changed",
        "source_changed",
        "check_failed",
        "real_model_missing",
        "blocking_issue",
        "not_ready",
    ]


def test_nonblocking_issues_and_successful_mock_do_not_replace_real_model() -> None:
    """A passing mock check cannot meet a stage's real-model evidence obligation."""
    manifest = _manifest(
        checks=[_check(mode="mock")],
        known_issues=[KnownIssue(summary="copy polish", severity="low", blocking=False)],
    )

    assert evaluate_evidence(manifest, _project(), _state(), "b" * 64) == [
        "real_model_missing",
    ]
