"""Lock-fenced workflow transitions and auditable human approvals."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from product_factory.contracts.models import (
    ApprovalRecord,
    CompletionLevel,
    GateType,
    ProjectRecord,
    StateRecord,
    WaitingOn,
    WorkflowState,
)
from product_factory.domain.approvals import APPROVAL_STATEMENT, require_exact_approval
from product_factory.domain.states import require_transition
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.services.mutations import commit_state_change
from product_factory.storage.files import read_contained_regular_bytes
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


EvidenceCurrentValidator = Callable[[ProjectRepository, ProjectRecord, StateRecord, str], bool]


class WorkflowService:
    """Apply every mutable workflow operation while holding one lease mutex."""

    def __init__(self, root: Path, evidence_current: EvidenceCurrentValidator | None = None):
        self.root = root.resolve()
        self.locks = LockManager(self.root)
        if evidence_current is None:
            # Kept lazy so the evidence service can call the private already-held
            # commit helper without a module-import cycle.
            from product_factory.services.evidence import evidence_current as default_evidence_current

            evidence_current = default_evidence_current
        self.evidence_current = evidence_current

    def request_approval(
        self,
        gate: GateType,
        artifact: Path | None,
        lock_id: str,
        expected_revision: int,
    ) -> StateRecord:
        """Create a scope-bound approval request for the current workflow gate."""
        with self.locks.mutation(lock_id, expected_revision):
            repo = ProjectRepository(self.root)
            project, current = self._context(repo, expected_revision, "request_approval")
            self._require_no_waiting(current, "request_approval")
            if gate is GateType.TECHNICAL_ADAPTATION:
                scope = self._adaptation_scope(artifact)
                target = WorkflowState.ADAPTATION_PENDING_APPROVAL
                has_evidence = False
            elif gate is GateType.STAGE_ACCEPTANCE:
                if artifact is not None:
                    raise self._error(
                        "approval_artifact_unexpected", "阶段验收不接受技术方案工件", "request_approval"
                    )
                scope = {
                    "stage_id": current.current_stage.id,
                    "evidence_id": current.last_valid_evidence_id,
                }
                target = WorkflowState.HUMAN_ACCEPTANCE_PENDING
                has_evidence = current.last_valid_evidence_id is not None
            else:  # Pydantic enum protects callers, but retain a stable service boundary.
                raise self._error("approval_gate_invalid", "未知审批门禁", "request_approval")

            require_transition(
                current.workflow_state,
                target,
                current.current_stage.completion_level,
                has_approval=False,
                has_valid_evidence=has_evidence,
            )
            if gate is GateType.STAGE_ACCEPTANCE:
                self._require_current_evidence(repo, project, current, "request_approval")
            waiting = WaitingOn(
                type="approval",
                request_id=str(uuid4()),
                gate_type=gate,
                scope=scope,
            )
            next_state = current.model_copy(
                update={
                    "revision": expected_revision + 1,
                    "workflow_state": target,
                    "waiting_on": waiting,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            return commit_state_change(
                repo,
                current,
                next_state,
                "approval_requested",
                {"request_id": waiting.request_id, "gate_type": gate.value, "scope": scope},
            )

    def approve(
        self, statement: str, actor: str, lock_id: str, expected_revision: int
    ) -> StateRecord:
        """Consume exactly one current approval request and audit it before state mutation."""
        with self.locks.mutation(lock_id, expected_revision):
            if statement != APPROVAL_STATEMENT:
                raise self._error(
                    "approval_statement_mismatch", "审批声明必须与协议文本完全一致", "approve",
                    category=ErrorCategory.APPROVAL_REQUIRED,
                )
            repo = ProjectRepository(self.root)
            project, current = self._context(repo, expected_revision, "approve")
            waiting = current.waiting_on
            if waiting is None:
                raise self._error(
                    "approval_not_pending", "当前没有待消费的审批", "approve",
                    category=ErrorCategory.APPROVAL_REQUIRED,
                )
            self._require_scope_unchanged(repo, project, current, waiting)
            event_id = str(uuid4())
            proposed = ApprovalRecord(
                schema_version="1.0",
                approval_id=str(uuid4()),
                request_id=waiting.request_id,
                gate_type=waiting.gate_type,
                scope=waiting.scope,
                state_revision=expected_revision,
                statement=statement,
                actor=actor,
                source="interactive_cli",
                created_at=datetime.now(timezone.utc),
                consumed_by_revision=expected_revision + 1,
            )
            approval = self._recover_or_prepare_approval(repo, proposed, waiting, expected_revision)
            target = {
                GateType.TECHNICAL_ADAPTATION: WorkflowState.STAGE_DEVELOPMENT,
                GateType.STAGE_ACCEPTANCE: WorkflowState.NEXT_STAGE_OR_FRONTEND,
            }[waiting.gate_type]
            completion = (
                CompletionLevel.NONE
                if target is WorkflowState.STAGE_DEVELOPMENT
                else CompletionLevel.HUMAN_ACCEPTED
            )
            require_transition(
                current.workflow_state,
                target,
                current.current_stage.completion_level,
                has_approval=True,
                has_valid_evidence=current.last_valid_evidence_id is not None,
            )
            # The approval is intentionally durable before the state transition.  If the
            # subsequent state write fails, the record remains an auditable recovery fact.
            if approval is proposed:
                repo.append_approval(approval)
            next_state = current.model_copy(
                update={
                    "revision": expected_revision + 1,
                    "workflow_state": target,
                    "current_stage": current.current_stage.model_copy(
                        update={"completion_level": completion}
                    ),
                    "waiting_on": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            return commit_state_change(
                repo,
                current,
                next_state,
                "approval_consumed",
                {
                    "approval_id": approval.approval_id,
                    "request_id": approval.request_id,
                    "gate_type": approval.gate_type.value,
                    "scope": approval.scope,
                },
                event_id=event_id,
            )

    def prepare_approval(self, lock_id: str, expected_revision: int) -> None:
        """Check whether approval can be requested without consuming stdin.

        The CLI calls this short, non-mutating preflight before prompting the
        operator.  ``approve`` deliberately repeats every check after input,
        because the lock may have expired or project files may have changed in
        the meantime.
        """
        with self.locks.mutation(lock_id, expected_revision):
            repo = ProjectRepository(self.root)
            project, current = self._context(repo, expected_revision, "approve")
            waiting = current.waiting_on
            if waiting is None:
                raise self._error(
                    "approval_not_pending", "当前没有待消费的审批", "approve",
                    category=ErrorCategory.APPROVAL_REQUIRED,
                )
            self._require_scope_unchanged(repo, project, current, waiting)
            target = {
                GateType.TECHNICAL_ADAPTATION: WorkflowState.STAGE_DEVELOPMENT,
                GateType.STAGE_ACCEPTANCE: WorkflowState.NEXT_STAGE_OR_FRONTEND,
            }[waiting.gate_type]
            require_transition(
                current.workflow_state,
                target,
                current.current_stage.completion_level,
                has_approval=True,
                has_valid_evidence=current.last_valid_evidence_id is not None,
            )

    def start_verification(self, lock_id: str, expected_revision: int) -> StateRecord:
        """Record completed implementation before evidence is authored and validated."""
        with self.locks.mutation(lock_id, expected_revision):
            repo = ProjectRepository(self.root)
            _project, current = self._context(repo, expected_revision, "start_verification")
            self._require_no_waiting(current, "start_verification")
            require_transition(
                current.workflow_state,
                WorkflowState.SYSTEM_VERIFICATION,
                current.current_stage.completion_level,
                has_approval=False,
                has_valid_evidence=False,
            )
            next_state = current.model_copy(
                update={
                    "revision": expected_revision + 1,
                    "workflow_state": WorkflowState.SYSTEM_VERIFICATION,
                    "current_stage": current.current_stage.model_copy(
                        update={"completion_level": CompletionLevel.IMPLEMENTED}
                    ),
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            return commit_state_change(
                repo, current, next_state, "implementation_recorded", {"stage_id": current.current_stage.id}
            )

    def mark_system_verified(
        self, evidence_id: str, lock_id: str, expected_revision: int
    ) -> StateRecord:
        """Compatibility entry point that validates evidence before any state transition."""
        from product_factory.services.evidence import verify_stage

        return verify_stage(self.root, evidence_id, lock_id, expected_revision)

    def _commit_system_verified_locked(
        self,
        repo: ProjectRepository,
        project: ProjectRecord,
        current: StateRecord,
        evidence_id: str,
        expected_revision: int,
    ) -> StateRecord:
        """Commit evidence already validated by ``verify_stage`` inside its held mutation mutex."""
        if current.project_id != project.project_id:
            raise FactoryError(
                "project_identity_mismatch",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "项目状态与项目元数据的标识不一致",
                "mark_system_verified",
                False,
                "修复项目元数据后重试",
            )
        self._require_no_waiting(current, "mark_system_verified")
        if (
            current.workflow_state is not WorkflowState.SYSTEM_VERIFICATION
            or current.current_stage.completion_level is not CompletionLevel.IMPLEMENTED
        ):
            raise self._error("transition_not_allowed", "当前状态不能登记系统验证", "mark_system_verified")
        if not evidence_id:
            raise self._error("evidence_missing", "必须提供已验证证据 ID", "mark_system_verified")
        next_state = current.model_copy(
            update={
                "revision": expected_revision + 1,
                "current_stage": current.current_stage.model_copy(
                    update={"completion_level": CompletionLevel.SYSTEM_VERIFIED}
                ),
                "last_valid_evidence_id": evidence_id,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return commit_state_change(
            repo,
            current,
            next_state,
            "system_verified",
            {"stage_id": current.current_stage.id, "evidence_id": evidence_id},
        )

    def _context(
        self, repo: ProjectRepository, expected_revision: int, step: str
    ) -> tuple[ProjectRecord, StateRecord]:
        project = repo.load_project()
        current = repo.load_state()
        if current.project_id != project.project_id:
            raise FactoryError(
                "project_identity_mismatch",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "项目状态与项目元数据的标识不一致",
                step,
                False,
                "修复项目元数据后重试",
            )
        if current.revision != expected_revision:
            raise FactoryError(
                "revision_conflict",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "状态已被其他会话修改",
                step,
                True,
                "重新运行 status 或 resume",
                {"expected": expected_revision, "actual": current.revision},
            )
        return project, current

    @staticmethod
    def _require_no_waiting(current: StateRecord, step: str) -> None:
        if current.waiting_on is not None:
            raise WorkflowService._error(
                "approval_pending", "当前审批尚未完成", step, category=ErrorCategory.APPROVAL_REQUIRED
            )

    def _adaptation_scope(self, artifact: Path | None, step: str = "request_approval") -> dict[str, str]:
        if artifact is None:
            raise self._error("approval_artifact_required", "技术方案审批需要工件", step)
        relative, content = self._artifact_snapshot(artifact, step)
        return {
            "artifact_path": relative,
            "artifact_sha256": hashlib.sha256(content).hexdigest(),
        }

    def _require_scope_unchanged(
        self, repo: ProjectRepository, project: ProjectRecord, current: StateRecord, waiting: WaitingOn
    ) -> None:
        if waiting.gate_type is GateType.TECHNICAL_ADAPTATION:
            try:
                actual = self._adaptation_scope(Path(str(waiting.scope["artifact_path"])), "approve")
            except (KeyError, TypeError, FactoryError) as exc:
                raise self._error(
                    "approval_scope_changed", "审批工件范围已变化", "approve",
                    category=ErrorCategory.APPROVAL_REQUIRED,
                ) from exc
            if actual != waiting.scope:
                raise self._error(
                    "approval_scope_changed", "审批工件范围已变化", "approve",
                    category=ErrorCategory.APPROVAL_REQUIRED,
                )
        else:
            if waiting.scope != {
                "stage_id": current.current_stage.id,
                "evidence_id": current.last_valid_evidence_id,
            }:
                raise self._error(
                    "approval_scope_changed", "审批证据范围已变化", "approve",
                    category=ErrorCategory.APPROVAL_REQUIRED,
                )
            self._require_current_evidence(repo, project, current, "approve")

    def _require_current_evidence(
        self, repo: ProjectRepository, project: ProjectRecord, current: StateRecord, step: str
    ) -> None:
        evidence_id = current.last_valid_evidence_id
        if evidence_id is None:
            raise self._error(
                "evidence_invalid", "缺少当前有效验证证据", step,
                category=ErrorCategory.IMPLEMENTATION_FAILED,
            )
        if self.evidence_current is None:
            raise self._error(
                "evidence_validation_required", "未配置证据当前性校验器", step,
                category=ErrorCategory.IMPLEMENTATION_FAILED,
            )
        try:
            valid = self.evidence_current(repo, project, current, evidence_id)
        except FactoryError:
            raise
        except Exception as exc:
            raise self._error(
                "evidence_invalid", "证据当前性校验失败", step,
                category=ErrorCategory.IMPLEMENTATION_FAILED,
            ) from exc
        if valid is not True:
            raise self._error(
                "evidence_invalid", "证据已不再反映当前项目事实", step,
                category=ErrorCategory.IMPLEMENTATION_FAILED,
            )

    def _recover_or_prepare_approval(
        self,
        repo: ProjectRepository,
        proposed: ApprovalRecord,
        waiting: WaitingOn,
        expected_revision: int,
    ) -> ApprovalRecord:
        """Reuse one durable approval after a state-save crash, never append a duplicate."""
        related = [record for record in repo.read_approvals() if record.request_id == waiting.request_id]
        exact: list[ApprovalRecord] = []
        for record in related:
            try:
                require_exact_approval(record, waiting, expected_revision)
            except FactoryError:
                continue
            if record.actor == proposed.actor and record.source == proposed.source:
                exact.append(record)
        if len(exact) == 1 and len(related) == 1:
            return exact[0]
        if len(exact) > 1 or related:
            raise self._error(
                "approval_recovery_required", "发现冲突的审批恢复记录", "approve",
                category=ErrorCategory.ENVIRONMENT_BLOCKED,
            )
        require_exact_approval(proposed, waiting, expected_revision)
        return proposed

    def _artifact_snapshot(self, artifact: Path, step: str) -> tuple[str, bytes]:
        supplied = artifact if artifact.is_absolute() else self.root / artifact
        try:
            relative = supplied.relative_to(self.root)
        except ValueError as exc:
            raise self._error("approval_artifact_invalid", "审批工件必须位于项目目录内", step) from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise self._error("approval_artifact_invalid", "审批工件路径无效", step)
        try:
            content = read_contained_regular_bytes(self.root, relative.parts)
        except ValueError as exc:
            raise self._error("approval_artifact_invalid", "审批工件必须是项目内可读普通文件", step) from exc
        return relative.as_posix(), content

    @staticmethod
    def _error(
        code: str,
        message: str,
        step: str,
        *,
        category: ErrorCategory = ErrorCategory.POLICY_BLOCKED,
    ) -> FactoryError:
        return FactoryError(code, category, message, step, False, "修正当前工作流输入后重试")
