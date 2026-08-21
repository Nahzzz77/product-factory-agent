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
from product_factory.domain.approvals import require_exact_approval
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
    return _collect_validation(root)


def _collect_validation(root: Path) -> ValidationReport:
    """Collect business-record findings only; lock status is intentionally separate."""
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

    _validate_approvals(approvals, state, findings)
    _validate_events(events, project, state, findings)
    if state is not None:
        _validate_evidence(repo, project, state, findings)
    return ValidationReport(valid=not findings, findings=findings)


def resume_project(root: Path) -> RecoverySummary:
    """Summarize safe recovery facts; this function never repairs or initializes."""
    root = _validated_root(root)
    report = _collect_validation(root)
    repo = ProjectRepository(root)
    state = _read_json_model(repo.paths.state, StateRecord, "state", [])
    lock_status = _lock_status(root)
    audit_status = _audit_status(report.findings)
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
    if lock_status == "invalid":
        next_command = "product-factory validate"
    elif lock_status == "active":
        # A lease belongs to another session from this read-only process.
        next_command = "product-factory status"
    elif lock_status == "expired":
        next_command = "product-factory lock takeover"
    elif audit_status == "missing_referenced_event" and _only_repairable_audit_gap(report.findings):
        next_command = "product-factory repair-audit"
    elif report.findings:
        # A summary may contain useful partial facts, but a damaged protocol
        # record never authorizes a guessed state-changing follow-up.
        next_command = "product-factory validate"
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
        # Keep the complete decision under the same fence as the append.  The
        # collector deliberately excludes lock status, because this caller is
        # itself the active, validated lease holder.
        report = _collect_validation(root)
        if not report.findings:
            raise _repair_not_needed()
        if not _only_repairable_audit_gap(report.findings):
            raise _repair_unsafe(report.findings)
        before = _read_state_bytes(repo)
        state = _repair_state(before)
        if state.revision != expected_revision:
            raise _revision_conflict(expected_revision, state.revision)
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


def _validate_approvals(
    approvals: list[ApprovalRecord], state: StateRecord | None, findings: list[str]
) -> None:
    consumed = [item.consumed_by_revision for item in approvals]
    if len(consumed) != len(set(consumed)):
        _add(findings, "duplicate_approval_consumption")
    approval_ids = [item.approval_id for item in approvals]
    if len(approval_ids) != len(set(approval_ids)):
        _add(findings, "duplicate_approval_id")
    if state is None:
        return
    pending = [item for item in approvals if item.consumed_by_revision == state.revision + 1]
    later = [item for item in approvals if item.consumed_by_revision > state.revision + 1]
    if later:
        _add(findings, "approval_revision_future")
    if not pending:
        return
    if state.waiting_on is None:
        _add(findings, "approval_revision_future")
        return
    if len(pending) != 1:
        _add(findings, "duplicate_approval_consumption")
        return
    try:
        # This is the intentional approval-first crash point: one exact record
        # can exist while the state still waits for its consumption transition.
        require_exact_approval(pending[0], state.waiting_on, state.revision)
    except FactoryError:
        _add(findings, "approval_pending_mismatch")


def _validate_events(
    events: list[EventRecord], project: ProjectRecord | None, state: StateRecord | None,
    findings: list[str],
) -> None:
    event_ids = [item.event_id for item in events]
    if len(event_ids) != len(set(event_ids)):
        _add(findings, "duplicate_event_id")
    if project is not None and any(item.project_id != project.project_id for item in events):
        _add(findings, "event_project_id_mismatch")
    if any(not _event_revision_is_valid(item) for item in events):
        _add(findings, "event_revision_invalid")
    if state is None:
        return
    if any(item.after_revision > state.revision for item in events):
        _add(findings, "event_revision_future")
    by_id = {item.event_id: item for item in events}
    if state.last_event_id is None:
        if any(item.event_type == "recovered_missing_event" for item in events):
            _add(findings, "recovered_event_reference_mismatch")
        return
    referenced = by_id.get(state.last_event_id)
    if referenced is None:
        _add(findings, "missing_referenced_event")
        return
    if referenced.after_revision != state.revision:
        _add(findings, "referenced_event_revision_mismatch")
    for event in events:
        if event.event_type == "recovered_missing_event" and event.event_id != state.last_event_id:
            _add(findings, "recovered_event_reference_mismatch")


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
    except FileNotFoundError:
        _add(findings, "missing_referenced_evidence")
        return
    except (ValueError, ValidationError):
        _add(findings, "referenced_evidence_invalid")
        return
    except OSError:
        _add(findings, "referenced_evidence_unverifiable")
        return
    except FactoryError as exc:
        _add_evidence_factory_error(findings, exc)
        return
    identity_invalid = False
    if manifest.evidence_id != state.last_valid_evidence_id:
        _add(findings, "referenced_evidence_id_mismatch")
        identity_invalid = True
    if manifest.stage_id != state.current_stage.id:
        _add(findings, "referenced_evidence_stage_mismatch")
        identity_invalid = True
    if manifest.state_revision > state.revision:
        _add(findings, "referenced_evidence_revision_future")
        identity_invalid = True
    if identity_invalid:
        return
    try:
        digest = compute_source_digest(repo.paths.root, project.source_excludes)
        if evaluate_evidence(manifest, project, state, digest):
            _add(findings, "stale_referenced_evidence")
    except (OSError, ValueError, RuntimeError):
        _add(findings, "referenced_evidence_unverifiable")
    except FactoryError as exc:
        _add_evidence_factory_error(findings, exc)


def _add_evidence_factory_error(findings: list[str], error: FactoryError) -> None:
    """Classify deterministic protocol errors separately from transient reads.

    Repository input/policy errors describe an invalid reference (for example a
    path-traversal evidence ID), so they must not look like a retryable storage
    outage.  The explicitly retryable environment category is reserved for
    unstable digest/source observations; unknown FactoryErrors default to the
    conservative invalid result.
    """
    if error.code == "evidence_identifier_invalid":
        _add(findings, "referenced_evidence_invalid")
        return
    if error.category is ErrorCategory.ENVIRONMENT_BLOCKED and error.retryable:
        _add(findings, "referenced_evidence_unverifiable")
        return
    _add(findings, "referenced_evidence_invalid")


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
    if "referenced_evidence_unverifiable" in findings:
        return "unverifiable"
    if "stale_referenced_evidence" in findings:
        return "stale"
    if any(
        finding in {
            "referenced_evidence_invalid",
            "referenced_evidence_id_mismatch",
            "referenced_evidence_stage_mismatch",
            "referenced_evidence_revision_future",
        }
        for finding in findings
    ):
        return "invalid"
    return "current"


def _audit_status(findings: list[str]) -> str:
    audit_findings = [finding for finding in findings if _is_audit_finding(finding)]
    if audit_findings == ["missing_referenced_event"]:
        return "missing_referenced_event"
    if audit_findings:
        return "invalid"
    return "complete"


def _is_audit_finding(finding: str) -> bool:
    if finding.startswith(
        (
            "approval_",
            "event_",
            "duplicate_approval",
            "duplicate_event",
            "referenced_event",
            "recovered_event",
        )
    ):
        return True
    return finding in {
        "missing_referenced_event",
        "project_id_mismatch",
        "project_invalid",
        "intake_invalid",
        "state_invalid",
        "unknown_current_stage",
        "current_stage_sequence_mismatch",
    }


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
        raise _repair_unsafe(["state_invalid"]) from exc


def _repair_state(raw: bytes) -> StateRecord:
    try:
        return StateRecord.model_validate_json(raw)
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise _repair_unsafe(["state_invalid"]) from exc


def _repair_not_needed() -> FactoryError:
    return FactoryError(
        "audit_repair_not_needed", ErrorCategory.POLICY_BLOCKED,
        "没有可修复的审计缺口", "repair_audit", False, "运行 resume",
    )


def _repair_unsafe(findings: list[str]) -> FactoryError:
    return FactoryError(
        "audit_repair_unsafe", ErrorCategory.ENVIRONMENT_BLOCKED,
        "审计缺口之外还存在项目不一致", "repair_audit", False,
        "先运行 validate 并修复项目文件", {"findings": findings},
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
