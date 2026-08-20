"""Read-only consistency inspection and explicit state-first audit recovery."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from product_factory.contracts.models import (
    ApprovalRecord,
    EventRecord,
    IntakeRecord,
    ProjectRecord,
    StateRecord,
    WorkflowState,
)
from product_factory.domain.evidence import evaluate_evidence
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.services.evidence import compute_source_digest
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    findings: list[str]


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    workflow_state: str
    revision: int
    lock_status: str
    evidence_status: str
    waiting_on: dict | None
    audit_status: str
    next_command: str


NEXT_COMMAND = {
    WorkflowState.INITIALIZED: "product-factory check-inputs",
    WorkflowState.INPUTS_CHECKED: "product-factory request-approval --gate technical_adaptation --artifact docs/technical-adaptation.md",
    WorkflowState.ADAPTATION_PENDING_APPROVAL: "product-factory approve",
    WorkflowState.STAGE_DEVELOPMENT: "product-factory transition --to system_verification",
    WorkflowState.SYSTEM_VERIFICATION: "product-factory record-evidence --manifest evidence-authoring.yaml",
    WorkflowState.HUMAN_ACCEPTANCE_PENDING: "product-factory approve",
    WorkflowState.NEXT_STAGE_OR_FRONTEND: "等待下一里程碑设计",
}

_Model = TypeVar("_Model", bound=BaseModel)


def validate_project(root: Path) -> ValidationReport:
    """Return all safely observable protocol findings without changing the tree.

    Parsing is deliberately record-local.  A damaged approval/event line must
    never conceal independent damage further down the append-only audit log.
    """
    root = _validated_root(root)
    repo = ProjectRepository(root)
    findings: list[str] = []
    project = _read_yaml_model(repo.paths.project, ProjectRecord, "project", findings)
    intake = _read_yaml_model(repo.paths.intake, IntakeRecord, "intake", findings)
    state = _read_json_model(repo.paths.state, StateRecord, "state", findings)
    approvals = _read_jsonl_models(repo.paths.approvals, ApprovalRecord, "approval", findings)
    events = _read_jsonl_models(repo.paths.events, EventRecord, "event", findings)

    if project is not None and intake is not None and state is not None:
        if {project.project_id, intake.project_id, state.project_id} != {project.project_id}:
            _add(findings, "project_id_mismatch")
    if project is not None and state is not None:
        stage_ids = {stage.id for stage in project.stage_plan}
        stage = next((item for item in project.stage_plan if item.id == state.current_stage.id), None)
        if state.current_stage.id not in stage_ids:
            _add(findings, "unknown_current_stage")
        elif stage is not None and state.current_stage.sequence != stage.sequence:
            _add(findings, "current_stage_sequence_mismatch")

    if len([item.consumed_by_revision for item in approvals]) != len(
        set(item.consumed_by_revision for item in approvals)
    ):
        _add(findings, "duplicate_approval_consumption")
    if len([item.approval_id for item in approvals]) != len(set(item.approval_id for item in approvals)):
        _add(findings, "duplicate_approval_id")
    if state is not None and any(item.consumed_by_revision > state.revision for item in approvals):
        _add(findings, "approval_revision_future")

    event_ids = [item.event_id for item in events]
    if len(event_ids) != len(set(event_ids)):
        _add(findings, "duplicate_event_id")
    if project is not None:
        if any(item.project_id != project.project_id for item in events):
            _add(findings, "event_project_id_mismatch")
    if any(not _event_revision_is_valid(item) for item in events):
        _add(findings, "event_revision_invalid")
    if state is not None:
        by_id = {item.event_id: item for item in events}
        if state.last_event_id is not None:
            referenced = by_id.get(state.last_event_id)
            if referenced is None:
                _add(findings, "missing_referenced_event")
            elif referenced.after_revision != state.revision:
                _add(findings, "referenced_event_revision_mismatch")
        _validate_evidence(repo, project, state, findings)
    return ValidationReport(valid=not findings, findings=findings)


def resume_project(root: Path) -> RecoverySummary:
    """Summarize safe recovery facts; this function never repairs or initializes."""
    root = _validated_root(root)
    report = validate_project(root)
    repo = ProjectRepository(root)
    state = _read_json_model(repo.paths.state, StateRecord, "state", [])
    lock_status = _lock_status(root)
    event_log_damaged = any(finding.startswith("event_invalid:") for finding in report.findings)
    if event_log_damaged:
        audit_status = "invalid"
    elif "missing_referenced_event" in report.findings:
        audit_status = "missing_referenced_event"
    else:
        audit_status = "complete"
    evidence_status = _evidence_status(report.findings, state)
    if state is None:
        return RecoverySummary(
            workflow_state="unknown",
            revision=-1,
            lock_status=lock_status,
            evidence_status=evidence_status,
            waiting_on=None,
            audit_status="unknown",
            next_command="product-factory validate",
        )
    next_command = NEXT_COMMAND.get(state.workflow_state, "product-factory validate")
    if audit_status == "missing_referenced_event" and _only_repairable_audit_gap(report.findings):
        next_command = "product-factory repair-audit"
    elif report.findings:
        # A summary may contain useful partial facts, but a damaged protocol
        # record never authorizes a guessed state-changing follow-up.
        next_command = "product-factory validate"
    # A lease means another session owns every mutation recommendation, including repair.
    if lock_status == "active":
        next_command = "product-factory status"
    return RecoverySummary(
        workflow_state=state.workflow_state.value,
        revision=state.revision,
        lock_status=lock_status,
        evidence_status=evidence_status,
        waiting_on=state.waiting_on.model_dump(mode="json") if state.waiting_on else None,
        audit_status=audit_status,
        next_command=next_command,
    )


def repair_audit(root: Path, lock_id: str, expected_revision: int) -> EventRecord:
    """Append only the event reserved by state; do not alter business state.

    The complete decision and append are protected by the same SQLite-backed
    mutation fence as every state transition.  A retry therefore sees the
    first append and reports ``audit_repair_not_needed`` instead of duplicating
    an event.
    """
    root = _validated_root(root)
    manager = LockManager(root)
    with manager.mutation(lock_id, expected_revision):
        repo = ProjectRepository(root)
        before = _read_state_bytes(repo)
        state = _repair_state(before)
        if state.revision != expected_revision:
            raise _revision_conflict(expected_revision, state.revision)
        project = _repair_project(repo)
        intake = _repair_intake(repo)
        if {project.project_id, intake.project_id, state.project_id} != {project.project_id}:
            raise _repair_not_safe("项目标识不一致")
        events, issues = _read_jsonl_models_safely(repo.paths.events, EventRecord)
        if issues:
            raise _repair_not_safe("审计日志包含损坏记录")
        if any(item.project_id != project.project_id for item in events):
            raise _repair_not_safe("审计日志项目标识不一致")
        if state.last_event_id is None or any(item.event_id == state.last_event_id for item in events):
            raise _repair_not_needed()
        # ``append_jsonl`` is an atomic replacement, so a non-newline tail could
        # turn a damaged partial record into a different one.  Refuse rather
        # than silently normalizing or truncating external corruption.
        try:
            previous = repo.paths.events.read_bytes()
        except OSError as exc:
            raise _repair_not_safe("审计日志不可读取") from exc
        if previous and not previous.endswith(b"\n"):
            raise _repair_not_safe("审计日志尾部不完整")
        event = EventRecord(
            schema_version="1.0",
            event_id=state.last_event_id,
            event_type="recovered_missing_event",
            project_id=state.project_id,
            before_revision=state.revision,
            after_revision=state.revision,
            created_at=datetime.now(timezone.utc),
            details={"workflow_state": state.workflow_state.value},
        )
        repo.append_event(event)
        if repo.paths.state.read_bytes() != before:
            raise RuntimeError("repair_audit changed state.json")
        return event


def _validated_root(root: Path) -> Path:
    try:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise OSError("project root is not a directory")
        # Opening the directory catches inaccessible roots on platforms where
        # ``exists``/``is_dir`` alone can conceal a permission failure.
        with os.scandir(resolved):
            pass
        return resolved
    except (OSError, RuntimeError) as exc:
        raise FactoryError(
            "project_unreadable", ErrorCategory.ENVIRONMENT_BLOCKED,
            "项目目录不存在或不可读取", "validate", True,
            "检查项目目录路径和权限",
        ) from exc


def _read_yaml_model(path: Path, model: type[_Model], label: str, findings: list[str]) -> _Model | None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("expected mapping")
        return model.model_validate(payload)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError, TypeError):
        _add(findings, f"{label}_invalid")
        return None


def _read_json_model(path: Path, model: type[_Model], label: str, findings: list[str]) -> _Model | None:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
        _add(findings, f"{label}_invalid")
        return None


def _read_jsonl_models(
    path: Path, model: type[_Model], label: str, findings: list[str]
) -> list[_Model]:
    records, issues = _read_jsonl_models_safely(path, model)
    for issue in issues:
        _add(findings, f"{label}_invalid:{issue}")
    return records


def _read_jsonl_models_safely(path: Path, model: type[_Model]) -> tuple[list[_Model], list[str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], ["file"]
    records: list[_Model] = []
    issues: list[str] = []
    for number, line in enumerate(lines, start=1):
        try:
            payload: Any = json.loads(line)
            records.append(model.model_validate(payload))
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            issues.append(f"line:{number}")
    return records, issues


def _validate_evidence(
    repo: ProjectRepository, project: ProjectRecord | None, state: StateRecord, findings: list[str]
) -> None:
    if state.last_valid_evidence_id is None:
        return
    if project is None or state.current_stage.id not in {item.id for item in project.stage_plan}:
        _add(findings, "referenced_evidence_unverifiable")
        return
    try:
        manifest = repo.load_evidence(state.current_stage.id, state.last_valid_evidence_id)
    except (OSError, ValueError, ValidationError, FactoryError):
        _add(findings, "missing_referenced_evidence")
        return
    try:
        digest = compute_source_digest(repo.paths.root, project.source_excludes)
        if evaluate_evidence(manifest, project, state, digest):
            _add(findings, "stale_referenced_evidence")
    except (FactoryError, OSError, ValueError, RuntimeError):
        _add(findings, "referenced_evidence_unverifiable")


def _lock_status(root: Path) -> str:
    try:
        record = LockManager(root).status()
    except FactoryError:
        return "invalid"
    if record is None:
        return "unlocked"
    return "active" if record.lease_expires_at > datetime.now(timezone.utc) else "expired"


def _evidence_status(findings: list[str], state: StateRecord | None) -> str:
    if state is None or state.last_valid_evidence_id is None:
        return "not_recorded"
    if "missing_referenced_evidence" in findings:
        return "missing"
    if "stale_referenced_evidence" in findings:
        return "stale"
    if "referenced_evidence_unverifiable" in findings:
        return "unverifiable"
    return "current"


def _only_repairable_audit_gap(findings: list[str]) -> bool:
    """Repair only the isolated, state-first crash signature.

    Any other finding means repair could turn a partially understood project
    into a different one, which is deliberately outside this command's scope.
    """
    return findings == ["missing_referenced_event"]


def _event_revision_is_valid(event: EventRecord) -> bool:
    """A repair event records an already-committed state, so it has no delta."""
    if event.event_type == "recovered_missing_event":
        return event.after_revision == event.before_revision
    return event.after_revision == event.before_revision + 1


def _read_state_bytes(repo: ProjectRepository) -> bytes:
    try:
        return repo.paths.state.read_bytes()
    except OSError as exc:
        raise _repair_not_safe("状态文件不可读取") from exc


def _repair_state(raw: bytes) -> StateRecord:
    try:
        return StateRecord.model_validate_json(raw)
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise _repair_not_safe("状态文件无效") from exc


def _repair_project(repo: ProjectRepository) -> ProjectRecord:
    findings: list[str] = []
    project = _read_yaml_model(repo.paths.project, ProjectRecord, "project", findings)
    if project is None:
        raise _repair_not_safe("项目元数据无效")
    return project


def _repair_intake(repo: ProjectRepository) -> IntakeRecord:
    findings: list[str] = []
    intake = _read_yaml_model(repo.paths.intake, IntakeRecord, "intake", findings)
    if intake is None:
        raise _repair_not_safe("输入元数据无效")
    return intake


def _repair_not_needed() -> FactoryError:
    return FactoryError(
        "audit_repair_not_needed", ErrorCategory.POLICY_BLOCKED,
        "没有可修复的审计缺口", "repair_audit", False, "运行 resume",
    )


def _repair_not_safe(message: str) -> FactoryError:
    return FactoryError(
        "audit_repair_not_safe", ErrorCategory.ENVIRONMENT_BLOCKED,
        message, "repair_audit", False, "先运行 validate 并修复项目文件",
    )


def _revision_conflict(expected: int, actual: int) -> FactoryError:
    return FactoryError(
        "revision_conflict", ErrorCategory.ENVIRONMENT_BLOCKED,
        "状态已被其他会话修改", "repair_audit", True,
        "重新运行 status 或 resume", {"expected": expected, "actual": actual},
    )


def _add(findings: list[str], finding: str) -> None:
    if finding not in findings:
        findings.append(finding)
