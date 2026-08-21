"""Read-only project recovery and the deliberately narrow audit repair."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread

import pytest

from product_factory.contracts.models import (
    ApprovalRecord, CompletionLevel, CurrentStage, EventRecord, GateType, LockOwner, WaitingOn, WorkflowState,
)
from product_factory.domain.approvals import APPROVAL_STATEMENT
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.services.initialize import initialize_project
from product_factory.storage.files import atomic_write_json
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


def _intake(path: Path) -> None:
    keys = (
        "target_user_and_core_task", "input_process_output", "user_flow_and_confirmations",
        "scope_and_priority", "acceptance_criteria", "model_cost_platform",
        "data_privacy_performance_deployment",
    )
    path.write_text(
        "schema_version: '1.0'\nproject_id: demo-web\nprd_confirmed: true\n"
        "confirmed_by: owner\nconfirmed_at: '2026-08-20T00:00:00Z'\nrequirements:\n"
        + "".join(f"  {key}:\n    status: present\n    source: PRD\n" for key in keys),
        encoding="utf-8",
    )


def _root(tmp_path: Path) -> Path:
    prd, intake, root = tmp_path / "prd.md", tmp_path / "intake.yaml", tmp_path / "product"
    prd.write_text("# PRD\n", encoding="utf-8")
    _intake(intake)
    initialize_project(root, "demo-web", "Demo", prd, intake, [("stage-01", "Core", False)], Path.cwd())
    return root


def _two_stage_root(tmp_path: Path) -> Path:
    prd, intake, root = tmp_path / "prd.md", tmp_path / "intake.yaml", tmp_path / "product"
    prd.write_text("# PRD\n", encoding="utf-8")
    _intake(intake)
    initialize_project(
        root, "demo-web", "Demo", prd, intake,
        [("stage-01", "Core", False), ("stage-02", "Frontend", False)], Path.cwd(),
    )
    return root


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        item.relative_to(root).as_posix(): (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_mtime_ns, item.stat().st_size)
        for item in sorted(root.rglob("*")) if item.is_file()
    }


def _lock(root: Path, revision: int = 0) -> str:
    return LockManager(root).acquire(
        LockOwner(tool="pytest", session_id="recovery", pid=1, host="local"),
        revision, timedelta(minutes=5),
    ).lock_id


def test_validate_and_resume_are_read_only_for_a_valid_project(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project, validate_project

    root = _root(tmp_path)
    before = _snapshot(root)
    report = validate_project(root)
    summary = resume_project(root)
    assert report.valid is True
    assert summary.workflow_state == "initialized"
    assert summary.revision == 0
    assert summary.audit_status == "complete"
    assert _snapshot(root) == before


def test_validation_collects_each_bad_record_without_stopping(tmp_path: Path) -> None:
    from product_factory.services.recovery import validate_project

    root = _root(tmp_path)
    metadata = root / ".product-factory"
    metadata.joinpath("intake.yaml").write_text("[bad", encoding="utf-8")
    metadata.joinpath("approvals.jsonl").write_text("{not-json}\n{}\n", encoding="utf-8")
    metadata.joinpath("events.jsonl").write_text("{not-json}\n{}\n", encoding="utf-8")
    report = validate_project(root)
    assert report.valid is False
    assert report.findings == [
        "intake_invalid",
        "approval_invalid:line:1",
        "approval_invalid:line:2",
        "event_invalid:line:1",
        "event_invalid:line:2",
    ]


def test_validation_rejects_illegal_waiting_on_and_changed_prd(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project, validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={
        "revision": 1,
        "waiting_on": WaitingOn(
            type="approval", request_id="bad", gate_type=GateType.TECHNICAL_ADAPTATION, scope={}
        ),
    }), 0)
    (root / "inputs/PRD.md").write_text("# changed\n", encoding="utf-8")

    report = validate_project(root)
    assert "waiting_on_invalid_for_workflow_state" in report.findings
    assert "prd_digest_mismatch" in report.findings
    assert resume_project(root).next_command == "product-factory validate"


def test_missing_project_root_raises_stable_factory_error(tmp_path: Path) -> None:
    from product_factory.services.recovery import validate_project

    with pytest.raises(FactoryError) as caught:
        validate_project(tmp_path / "missing")
    assert caught.value.code == "project_unreadable"


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is a POSIX probe")
def test_recovery_never_blocks_on_a_state_fifo(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project, validate_project

    root = _root(tmp_path)
    state = root / ".product-factory/state.json"
    state.unlink()
    os.mkfifo(state)
    result: list[object] = []
    worker = Thread(target=lambda: result.append((validate_project(root), resume_project(root))))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    report, summary = result[0]
    assert "state_invalid" in report.findings
    assert summary.workflow_state == "unknown"


def test_resume_reports_missing_audit_event_without_repairing(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    current = repo.load_state()
    # Mimic the state-first crash point after the inputs-checked transition
    # committed state and reserved an event id, but before the append.
    repo.save_state(current.model_copy(update={
        "revision": 1,
        "workflow_state": WorkflowState.INPUTS_CHECKED,
        "last_event_id": "lost-event",
    }), 0)
    before = _snapshot(root)
    summary = resume_project(root)
    assert summary.audit_status == "missing_referenced_event"
    assert summary.next_command == "product-factory repair-audit"
    assert _snapshot(root) == before


def test_v1_initialized_revision_one_with_a_real_event_is_invalid(tmp_path: Path) -> None:
    from product_factory.services.recovery import validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state().model_copy(update={"revision": 1, "last_event_id": "event-01"})
    atomic_write_json(repo.paths.state, state.model_dump(mode="json"))
    repo.append_event(EventRecord(
        schema_version="1.0", event_id="event-01", event_type="inputs_checked", project_id=state.project_id,
        before_revision=0, after_revision=1, created_at=datetime.now(timezone.utc), details={},
    ))
    assert "workflow_revision_invalid" in validate_project(root).findings


@pytest.mark.parametrize("with_event", [False, True])
def test_initialized_revision_one_is_never_repairable(tmp_path: Path, with_event: bool) -> None:
    from product_factory.services.recovery import repair_audit, resume_project, validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state().model_copy(update={"revision": 1, "last_event_id": "event-01"})
    atomic_write_json(repo.paths.state, state.model_dump(mode="json"))
    if with_event:
        repo.append_event(EventRecord(
            schema_version="1.0", event_id="event-01", event_type="inputs_checked", project_id=state.project_id,
            before_revision=0, after_revision=1, created_at=datetime.now(timezone.utc), details={},
        ))
    report = validate_project(root)
    assert "workflow_revision_invalid" in report.findings
    assert resume_project(root).next_command == "product-factory validate"
    lock_id = _lock(root, 1)
    with pytest.raises(FactoryError) as caught:
        repair_audit(root, lock_id, 1)
    assert caught.value.code == "audit_repair_unsafe"


def test_v1_state_must_stay_on_the_first_stage(tmp_path: Path) -> None:
    from product_factory.services.recovery import validate_project

    root = _two_stage_root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state().model_copy(update={
        "current_stage": CurrentStage(id="stage-02", sequence=2, completion_level=CompletionLevel.NONE)
    })
    atomic_write_json(repo.paths.state, state.model_dump(mode="json"))
    report = validate_project(root)
    assert "workflow_stage_invalid" in report.findings
    assert "current_stage_sequence_mismatch" not in report.findings


def test_resume_with_active_lock_recommends_only_status(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project

    root = _root(tmp_path)
    _lock(root)
    assert resume_project(root).next_command == "product-factory status"


def test_resume_never_recommends_repair_when_the_audit_log_is_damaged(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={
        "revision": 1,
        "workflow_state": WorkflowState.INPUTS_CHECKED,
        "last_event_id": "lost-event",
    }), 0)
    repo.paths.events.write_text("{partial", encoding="utf-8")
    summary = resume_project(root)
    assert summary.audit_status == "invalid"
    assert summary.next_command == "product-factory validate"


def test_pending_approval_written_before_state_commit_is_a_valid_resume_point(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project, validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    current = repo.load_state()
    waiting = WaitingOn(
        type="approval", request_id="request-01", gate_type=GateType.TECHNICAL_ADAPTATION,
        scope={"artifact": "docs/technical-adaptation.md"},
    )
    revision_one = current.model_copy(update={"revision": 1, "last_event_id": "event-01"})
    repo.save_state(revision_one, 0)
    pending = revision_one.model_copy(update={
        "revision": 2, "workflow_state": WorkflowState.ADAPTATION_PENDING_APPROVAL, "waiting_on": waiting,
        "last_event_id": "event-02",
    })
    repo.save_state(pending, 1)
    repo.append_event(EventRecord(
        schema_version="1.0", event_id="event-02", event_type="approval_requested",
        project_id=pending.project_id, before_revision=1, after_revision=2,
        created_at=datetime.now(timezone.utc), details={},
    ))
    repo.append_approval(ApprovalRecord(
        schema_version="1.0", approval_id="approval-01", request_id=waiting.request_id,
        gate_type=waiting.gate_type, scope=waiting.scope, state_revision=2,
        statement=APPROVAL_STATEMENT, actor="owner", source="interactive_cli",
        created_at=datetime.now(timezone.utc), consumed_by_revision=3,
    ))
    assert validate_project(root).valid is True
    assert resume_project(root).next_command == "product-factory approve"


def test_resume_reports_expired_and_invalid_locks_without_mutation_guidance(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project

    root = _root(tmp_path)
    expired = LockManager(root, now_fn=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc))
    expired.acquire(LockOwner(tool="pytest", session_id="expired", pid=1, host="local"), 0, timedelta(minutes=1))
    summary = resume_project(root)
    assert summary.lock_status == "expired"
    assert summary.next_command == "product-factory lock takeover"
    (root / ".product-factory/execution-lock.json").write_bytes(b"\xff")
    summary = resume_project(root)
    assert summary.lock_status == "invalid"
    assert summary.next_command == "product-factory validate"


def test_validation_binds_event_and_evidence_records_to_current_state(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project, validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    project, state = repo.load_project(), repo.load_state()
    repo.append_event(EventRecord(
        schema_version="1.0", event_id="future", event_type="inputs_checked", project_id=project.project_id,
        before_revision=98, after_revision=99, created_at=datetime.now(timezone.utc), details={},
    ))
    repo.save_state(state.model_copy(update={"revision": 1, "last_valid_evidence_id": "wanted"}), 0)
    path = repo.evidence_path("stage-01", "wanted")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": "1.0", "evidence_id": "other", "stage_id": "stage-01", "state_revision": 1,
        "factory_version": project.factory_version, "prd_sha256": project.prd.sha256, "source_digest": "a" * 64,
        "checks": [{"name": "check", "command": "true", "started_at": "2026-08-20T00:00:00Z", "ended_at": "2026-08-20T00:00:01Z", "exit_status": 0, "summary": "ok", "mode": "mock"}],
        "ready_for_human_acceptance": True,
    }), encoding="utf-8")
    report = validate_project(root)
    assert "event_revision_future" in report.findings
    assert "referenced_evidence_id_mismatch" in report.findings
    assert resume_project(root).evidence_status == "invalid"


def test_resume_reports_unverifiable_evidence_without_mutation_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from product_factory.services import recovery

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    project, state = repo.load_project(), repo.load_state()
    repo.save_state(state.model_copy(update={"revision": 1, "last_valid_evidence_id": "wanted"}), 0)
    path = repo.evidence_path("stage-01", "wanted")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": "1.0", "evidence_id": "wanted", "stage_id": "stage-01", "state_revision": 1,
        "factory_version": project.factory_version, "prd_sha256": project.prd.sha256, "source_digest": "a" * 64,
        "checks": [{"name": "check", "command": "true", "started_at": "2026-08-20T00:00:00Z", "ended_at": "2026-08-20T00:00:01Z", "exit_status": 0, "summary": "ok", "mode": "mock"}],
        "ready_for_human_acceptance": True,
    }), encoding="utf-8")
    monkeypatch.setattr(
        recovery,
        "compute_source_digest",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("digest worker interrupted")),
    )
    summary = recovery.resume_project(root)
    assert summary.evidence_status == "unverifiable"
    assert summary.next_command == "product-factory validate"


def test_recovery_classifies_retryable_environment_digest_error_as_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from product_factory.services import recovery

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    project, state = repo.load_project(), repo.load_state()
    repo.save_state(state.model_copy(update={"revision": 1, "last_valid_evidence_id": "wanted"}), 0)
    path = repo.evidence_path("stage-01", "wanted")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": "1.0", "evidence_id": "wanted", "stage_id": "stage-01", "state_revision": 1,
        "factory_version": project.factory_version, "prd_sha256": project.prd.sha256, "source_digest": "a" * 64,
        "checks": [{"name": "check", "command": "true", "started_at": "2026-08-20T00:00:00Z", "ended_at": "2026-08-20T00:00:01Z", "exit_status": 0, "summary": "ok", "mode": "mock"}],
        "ready_for_human_acceptance": True,
    }), encoding="utf-8")
    error = FactoryError(
        "source_digest_unstable", ErrorCategory.ENVIRONMENT_BLOCKED, "source changed during digest",
        "validate", True, "retry validation after source activity stops",
    )
    monkeypatch.setattr(
        recovery,
        "compute_source_digest",
        lambda *_args: (_ for _ in ()).throw(error),
    )

    report = recovery.validate_project(root)
    summary = recovery.resume_project(root)

    assert "referenced_evidence_unverifiable" in report.findings
    assert summary.evidence_status == "unverifiable"
    assert summary.next_command == "product-factory validate"


def test_recovery_classifies_unknown_factory_digest_error_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from product_factory.services import recovery

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    project, state = repo.load_project(), repo.load_state()
    repo.save_state(state.model_copy(update={"revision": 1, "last_valid_evidence_id": "wanted"}), 0)
    path = repo.evidence_path("stage-01", "wanted")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": "1.0", "evidence_id": "wanted", "stage_id": "stage-01", "state_revision": 1,
        "factory_version": project.factory_version, "prd_sha256": project.prd.sha256, "source_digest": "a" * 64,
        "checks": [{"name": "check", "command": "true", "started_at": "2026-08-20T00:00:00Z", "ended_at": "2026-08-20T00:00:01Z", "exit_status": 0, "summary": "ok", "mode": "mock"}],
        "ready_for_human_acceptance": True,
    }), encoding="utf-8")
    error = FactoryError(
        "digest_contract_invalid", ErrorCategory.IMPLEMENTATION_FAILED, "digest protocol violated",
        "validate", False, "inspect the evidence digest implementation",
    )
    monkeypatch.setattr(
        recovery,
        "compute_source_digest",
        lambda *_args: (_ for _ in ()).throw(error),
    )

    report = recovery.validate_project(root)
    summary = recovery.resume_project(root)

    assert "referenced_evidence_invalid" in report.findings
    assert summary.evidence_status == "invalid"
    assert summary.next_command == "product-factory validate"


def test_recovery_marks_invalid_evidence_identifier_as_invalid(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project, validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={"revision": 1, "last_valid_evidence_id": "../escape"}), 0)

    report = validate_project(root)
    assert "state_invalid" in report.findings
    assert "referenced_evidence_unverifiable" not in report.findings
    summary = resume_project(root)
    assert summary.evidence_status == "invalid"
    assert summary.next_command == "product-factory validate"


def test_repair_appends_only_the_referenced_event_and_retries_are_singleton(tmp_path: Path) -> None:
    from product_factory.services.recovery import repair_audit, validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={
        "revision": 1,
        "workflow_state": WorkflowState.INPUTS_CHECKED,
        "last_event_id": "lost-event",
    }), 0)
    before_state = repo.paths.state.read_bytes()
    assert validate_project(root).findings == ["missing_referenced_event"]
    lock_id = _lock(root, 1)
    event = repair_audit(root, lock_id, 1)
    assert event.event_id == "lost-event"
    assert repo.paths.state.read_bytes() == before_state
    assert [record.event_id for record in repo.read_events()] == ["lost-event"]
    report = validate_project(root)
    assert report.valid is True
    assert report.findings == []
    with pytest.raises(FactoryError) as caught:
        repair_audit(root, lock_id, 1)
    assert caught.value.code == "audit_repair_not_needed"


def test_repair_requires_current_lock_and_never_appends_after_malformed_tail(tmp_path: Path) -> None:
    from product_factory.services.recovery import repair_audit

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={
        "revision": 1,
        "workflow_state": WorkflowState.INPUTS_CHECKED,
        "last_event_id": "lost-event",
    }), 0)
    with pytest.raises(FactoryError) as caught:
        repair_audit(root, "missing-lock", 1)
    assert caught.value.code == "lock_required"
    repo.paths.events.write_text("{partial", encoding="utf-8")
    before = repo.paths.events.read_bytes()
    lock_id = _lock(root, 1)
    with pytest.raises(FactoryError) as caught:
        repair_audit(root, lock_id, 1)
    assert caught.value.code == "audit_repair_unsafe"
    assert caught.value.details == {"findings": ["event_invalid:line:1", "missing_referenced_event"]}
    assert repo.paths.events.read_bytes() == before
