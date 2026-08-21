from datetime import timedelta
from pathlib import Path

import pytest

from product_factory.contracts.models import (
    CompletionLevel,
    GateType,
    LockOwner,
    WorkflowState,
)
from product_factory.domain.approvals import APPROVAL_STATEMENT
from product_factory.errors import FactoryError
from product_factory.services.initialize import check_inputs, initialize_project
from product_factory.services.workflow import WorkflowService
from product_factory.storage import files
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


def _intake(path: Path) -> None:
    requirements = {
        key: {"status": "present", "source": "PRD"}
        for key in (
            "target_user_and_core_task",
            "input_process_output",
            "user_flow_and_confirmations",
            "scope_and_priority",
            "acceptance_criteria",
            "model_cost_platform",
            "data_privacy_performance_deployment",
        )
    }
    path.write_text(
        "schema_version: '1.0'\nproject_id: demo-web\nprd_confirmed: true\n"
        "confirmed_by: owner\nconfirmed_at: '2026-08-20T00:00:00Z'\nrequirements:\n"
        + "".join(f"  {key}:\n    status: present\n    source: PRD\n" for key in requirements),
        encoding="utf-8",
    )


def _root(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    prd = tmp_path / "prd.md"
    intake = tmp_path / "intake.yaml"
    prd.write_text("# PRD\n", encoding="utf-8")
    _intake(intake)
    root = tmp_path / "product"
    initialize_project(
        root, "demo-web", "Demo", prd, intake, [("stage-01", "Core", False)], Path.cwd()
    )
    return root


def _lock(root: Path, revision: int):
    return LockManager(root).acquire(
        LockOwner(tool="pytest", session_id=f"revision-{revision}", pid=1, host="local"),
        state_revision=revision,
        lease=timedelta(minutes=5),
    )


def _release(root: Path, lock_id: str) -> None:
    LockManager(root).release(lock_id)


def _inputs_checked(root: Path):
    lock = _lock(root, 0)
    state = check_inputs(root, lock.lock_id, expected_revision=0)
    _release(root, lock.lock_id)
    return state


def test_adaptation_approval_is_audited_and_exact(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _inputs_checked(root)
    artifact = root / "docs/technical-adaptation.md"
    artifact.write_text("selected offline path\n", encoding="utf-8")
    service = WorkflowService(root, evidence_current=lambda *_args: True)
    lock = _lock(root, 1)

    pending = service.request_approval(
        gate=GateType.TECHNICAL_ADAPTATION,
        artifact=Path("docs/technical-adaptation.md"),
        lock_id=lock.lock_id,
        expected_revision=1,
    )
    assert pending.workflow_state is WorkflowState.ADAPTATION_PENDING_APPROVAL
    assert pending.waiting_on is not None
    assert pending.waiting_on.gate_type is GateType.TECHNICAL_ADAPTATION
    _release(root, lock.lock_id)

    lock = _lock(root, 2)
    with pytest.raises(FactoryError) as caught:
        service.approve("wrong words", "owner", lock.lock_id, expected_revision=2)
    assert caught.value.code == "approval_statement_mismatch"

    approved = service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, expected_revision=2)
    assert approved.workflow_state is WorkflowState.STAGE_DEVELOPMENT
    assert approved.waiting_on is None
    approval = ProjectRepository(root).read_approvals()[0]
    assert approval.state_revision == 2
    assert approval.consumed_by_revision == 3
    assert ProjectRepository(root).read_events()[-1].event_type == "approval_consumed"
    _release(root, lock.lock_id)

    lock = _lock(root, 3)
    with pytest.raises(FactoryError) as caught:
        service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, expected_revision=3)
    assert caught.value.code == "approval_not_pending"


def test_adaptation_approval_rejects_changed_or_unsafe_artifact(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _inputs_checked(root)
    artifact = root / "docs/technical-adaptation.md"
    artifact.write_text("v1", encoding="utf-8")
    lock = _lock(root, 1)
    service = WorkflowService(root, evidence_current=lambda *_args: True)
    service.request_approval(GateType.TECHNICAL_ADAPTATION, Path("docs/technical-adaptation.md"), lock.lock_id, 1)
    _release(root, lock.lock_id)
    artifact.write_text("v2", encoding="utf-8")

    lock = _lock(root, 2)
    with pytest.raises(FactoryError) as caught:
        service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 2)
    assert caught.value.code == "approval_scope_changed"
    _release(root, lock.lock_id)

    unsafe_root = _root(tmp_path / "unsafe")
    _inputs_checked(unsafe_root)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (unsafe_root / "docs/link.md").symlink_to(outside)
    lock = _lock(unsafe_root, 1)
    with pytest.raises(FactoryError) as caught:
        WorkflowService(unsafe_root).request_approval(
            GateType.TECHNICAL_ADAPTATION, Path("docs/link.md"), lock.lock_id, 1
        )
    assert caught.value.code == "approval_artifact_invalid"


def test_stage_acceptance_keeps_evidence_and_requires_verified_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state().model_copy(
        update={
            "revision": 1,
            "workflow_state": WorkflowState.SYSTEM_VERIFICATION,
            "current_stage": repo.load_state().current_stage.model_copy(
                update={"completion_level": CompletionLevel.SYSTEM_VERIFIED}
            ),
            "last_valid_evidence_id": "evidence-01",
        }
    )
    repo.save_state(state, expected_revision=0)
    service = WorkflowService(root, evidence_current=lambda *_args: True)
    lock = _lock(root, 1)
    pending = service.request_approval(GateType.STAGE_ACCEPTANCE, None, lock.lock_id, 1)
    assert pending.workflow_state is WorkflowState.HUMAN_ACCEPTANCE_PENDING
    assert pending.waiting_on is not None
    assert pending.waiting_on.scope == {"stage_id": "stage-01", "evidence_id": "evidence-01"}
    _release(root, lock.lock_id)
    lock = _lock(root, 2)
    approved = service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 2)
    assert approved.workflow_state is WorkflowState.NEXT_STAGE_OR_FRONTEND
    assert approved.current_stage.completion_level is CompletionLevel.HUMAN_ACCEPTED
    assert approved.last_valid_evidence_id == "evidence-01"


def test_rejects_wrong_gate_state_and_changed_stage_acceptance_scope(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _inputs_checked(root)
    service = WorkflowService(root)
    lock = _lock(root, 1)
    with pytest.raises(FactoryError) as caught:
        service.request_approval(GateType.STAGE_ACCEPTANCE, None, lock.lock_id, 1)
    assert caught.value.code == "transition_not_allowed"
    _release(root, lock.lock_id)
    service = WorkflowService(root, evidence_current=lambda *_args: True)

    repo = ProjectRepository(root)
    checked = repo.load_state()
    verified = checked.model_copy(
        update={
            "revision": 2,
            "workflow_state": WorkflowState.SYSTEM_VERIFICATION,
            "current_stage": checked.current_stage.model_copy(
                update={"completion_level": CompletionLevel.SYSTEM_VERIFIED}
            ),
            "last_valid_evidence_id": "evidence-01",
        }
    )
    repo.save_state(verified, 1)
    lock = _lock(root, 2)
    service.request_approval(GateType.STAGE_ACCEPTANCE, None, lock.lock_id, 2)
    _release(root, lock.lock_id)
    pending = repo.load_state()
    changed = pending.model_copy(
        update={"revision": 4, "last_valid_evidence_id": "evidence-02"}
    )
    repo.save_state(changed, 3)
    lock = _lock(root, 4)
    with pytest.raises(FactoryError) as caught:
        service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 4)
    assert caught.value.code == "approval_scope_changed"


def test_development_and_system_verification_are_lock_fenced(tmp_path: Path) -> None:
    root = _root(tmp_path)
    repo = ProjectRepository(root)
    initial = repo.load_state()
    development = initial.model_copy(
        update={"revision": 1, "workflow_state": WorkflowState.STAGE_DEVELOPMENT}
    )
    repo.save_state(development, 0)
    service = WorkflowService(root)
    lock = _lock(root, 1)
    verifying = service.start_verification(lock.lock_id, 1)
    assert verifying.current_stage.completion_level is CompletionLevel.IMPLEMENTED
    _release(root, lock.lock_id)
    lock = _lock(root, 2)
    with pytest.raises(FactoryError) as caught:
        service.mark_system_verified("evidence-01", lock.lock_id, 2)
    assert caught.value.code == "evidence_missing"
    state = ProjectRepository(root).load_state()
    assert state.current_stage.completion_level is CompletionLevel.IMPLEMENTED


def test_approval_retry_reuses_the_single_durable_record_after_state_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    _inputs_checked(root)
    (root / "docs/technical-adaptation.md").write_text("v1", encoding="utf-8")
    service = WorkflowService(root)
    lock = _lock(root, 1)
    service.request_approval(GateType.TECHNICAL_ADAPTATION, Path("docs/technical-adaptation.md"), lock.lock_id, 1)
    _release(root, lock.lock_id)
    lock = _lock(root, 2)
    original_save = ProjectRepository.save_state
    failed_once = False

    def fail_after_approval(self: ProjectRepository, *args: object, **kwargs: object):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("disk full")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(ProjectRepository, "save_state", fail_after_approval)
    with pytest.raises(OSError):
        service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 2)
    assert len(ProjectRepository(root).read_approvals()) == 1

    with pytest.raises(FactoryError) as caught:
        service.approve(APPROVAL_STATEMENT, "other-owner", lock.lock_id, 2)
    assert caught.value.code == "approval_recovery_required"
    assert len(ProjectRepository(root).read_approvals()) == 1

    approved = service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 2)
    approvals = ProjectRepository(root).read_approvals()
    assert approved.revision == 3
    assert len(approvals) == 1
    assert approvals[0].request_id == approvals[0].request_id
    assert approvals[0].state_revision == 2


def test_stage_acceptance_uses_the_default_current_evidence_validator(tmp_path: Path) -> None:
    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    verified = state.model_copy(
        update={
            "revision": 1,
            "workflow_state": WorkflowState.SYSTEM_VERIFICATION,
            "current_stage": state.current_stage.model_copy(
                update={"completion_level": CompletionLevel.SYSTEM_VERIFIED}
            ),
            "last_valid_evidence_id": "evidence-01",
        }
    )
    repo.save_state(verified, 0)
    lock = _lock(root, 1)
    with pytest.raises(FactoryError) as caught:
        WorkflowService(root).request_approval(GateType.STAGE_ACCEPTANCE, None, lock.lock_id, 1)
    assert caught.value.code == "evidence_invalid"
    with pytest.raises(FactoryError) as caught:
        WorkflowService(root, evidence_current=lambda *_args: False).request_approval(
            GateType.STAGE_ACCEPTANCE, None, lock.lock_id, 1
        )
    assert caught.value.code == "evidence_invalid"
    pending = WorkflowService(root, evidence_current=lambda *_args: True).request_approval(
        GateType.STAGE_ACCEPTANCE, None, lock.lock_id, 1
    )
    assert pending.workflow_state is WorkflowState.HUMAN_ACCEPTANCE_PENDING


def test_descriptor_snapshot_rejects_replacement_with_an_outside_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    _inputs_checked(root)
    artifact = root / "docs/technical-adaptation.md"
    artifact.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside-secret", encoding="utf-8")
    original_open = files.os.open
    replaced = False

    def replace_before_file_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal replaced
        if not replaced and path == "technical-adaptation.md" and "dir_fd" in kwargs:
            replaced = True
            artifact.unlink()
            artifact.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", replace_before_file_open)
    lock = _lock(root, 1)
    with pytest.raises(FactoryError) as caught:
        WorkflowService(root).request_approval(
            GateType.TECHNICAL_ADAPTATION, Path("docs/technical-adaptation.md"), lock.lock_id, 1
        )
    assert caught.value.code == "approval_artifact_invalid"
    state = ProjectRepository(root).load_state()
    assert state.workflow_state is WorkflowState.INPUTS_CHECKED
    assert state.waiting_on is None


def test_project_identity_mismatch_blocks_mutations_without_audit_writes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _inputs_checked(root)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={"project_id": "wrong-project", "revision": 2}), 1)
    (root / "docs/technical-adaptation.md").write_text("v1", encoding="utf-8")
    lock = _lock(root, 2)
    with pytest.raises(FactoryError) as caught:
        WorkflowService(root).request_approval(
            GateType.TECHNICAL_ADAPTATION, Path("docs/technical-adaptation.md"), lock.lock_id, 2
        )
    assert caught.value.code == "project_identity_mismatch"
    assert ProjectRepository(root).read_approvals() == []
    assert [event.event_type for event in ProjectRepository(root).read_events()] == ["inputs_checked"]


def test_all_workflow_identity_failures_leave_business_protocol_files_unchanged(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _inputs_checked(root)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={"project_id": "wrong-project", "revision": 2}), 1)
    (root / "docs/technical-adaptation.md").write_text("v1", encoding="utf-8")
    lock = _lock(root, 2)
    business_files = (
        repo.paths.project,
        repo.paths.intake,
        repo.paths.state,
        repo.paths.approvals,
        repo.paths.events,
    )
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in business_files}
    service = WorkflowService(root)

    operations = (
        lambda: service.request_approval(
            GateType.TECHNICAL_ADAPTATION, Path("docs/technical-adaptation.md"), lock.lock_id, 2
        ),
        lambda: service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 2),
        lambda: service.start_verification(lock.lock_id, 2),
        lambda: service.mark_system_verified("evidence-01", lock.lock_id, 2),
    )
    for operation in operations:
        with pytest.raises(FactoryError) as caught:
            operation()
        assert caught.value.code == "project_identity_mismatch"

    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in business_files}
    assert after == before
