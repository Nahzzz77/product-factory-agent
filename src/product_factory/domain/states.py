"""Workflow transition policy for the implemented milestone."""

from dataclasses import dataclass

from product_factory.contracts.models import CompletionLevel, WorkflowState
from product_factory.errors import ErrorCategory, FactoryError


@dataclass(frozen=True, slots=True)
class TransitionRule:
    required_completion: CompletionLevel
    requires_approval: bool = False
    requires_evidence: bool = False


RULES = {
    (WorkflowState.INITIALIZED, WorkflowState.INPUTS_CHECKED): TransitionRule(
        CompletionLevel.NONE
    ),
    (
        WorkflowState.INPUTS_CHECKED,
        WorkflowState.ADAPTATION_PENDING_APPROVAL,
    ): TransitionRule(CompletionLevel.NONE),
    (
        WorkflowState.ADAPTATION_PENDING_APPROVAL,
        WorkflowState.STAGE_DEVELOPMENT,
    ): TransitionRule(CompletionLevel.NONE, requires_approval=True),
    (
        WorkflowState.STAGE_DEVELOPMENT,
        WorkflowState.SYSTEM_VERIFICATION,
    ): TransitionRule(CompletionLevel.NONE),
    (
        WorkflowState.SYSTEM_VERIFICATION,
        WorkflowState.HUMAN_ACCEPTANCE_PENDING,
    ): TransitionRule(CompletionLevel.SYSTEM_VERIFIED, requires_evidence=True),
    (
        WorkflowState.HUMAN_ACCEPTANCE_PENDING,
        WorkflowState.NEXT_STAGE_OR_FRONTEND,
    ): TransitionRule(
        CompletionLevel.SYSTEM_VERIFIED,
        requires_approval=True,
        requires_evidence=True,
    ),
}


POST_MILESTONE_ONE_STATES = frozenset(
    {
        WorkflowState.NEXT_STAGE_OR_FRONTEND,
        WorkflowState.RELEASE_READY,
        WorkflowState.DEPLOYMENT_PENDING_APPROVAL,
        WorkflowState.DEPLOYED_PENDING_ACCEPTANCE,
        WorkflowState.PRODUCTION_ACCEPTED,
        WorkflowState.OBSERVING,
    }
)

FUTURE_TARGET_STATES = POST_MILESTONE_ONE_STATES - {
    WorkflowState.NEXT_STAGE_OR_FRONTEND
}


def require_transition(
    current: WorkflowState,
    target: WorkflowState,
    completion: CompletionLevel,
    *,
    has_approval: bool,
    has_valid_evidence: bool,
) -> TransitionRule:
    """Return the matching rule or raise a stable, actionable policy error."""
    if current in POST_MILESTONE_ONE_STATES or target in FUTURE_TARGET_STATES:
        raise FactoryError(
            "unsupported_transition",
            ErrorCategory.POLICY_BLOCKED,
            "后续流程尚未实现",
            "transition",
            False,
            "等待后续里程碑",
        )

    rule = RULES.get((current, target))
    if rule is None:
        raise FactoryError(
            "transition_not_allowed",
            ErrorCategory.POLICY_BLOCKED,
            "不允许该状态转换",
            "transition",
            False,
            "运行 status 查看允许动作",
        )
    if completion is not rule.required_completion:
        raise FactoryError(
            "completion_mismatch",
            ErrorCategory.IMPLEMENTATION_FAILED,
            "阶段完成级别不满足门禁",
            "transition",
            True,
            "完成当前验证步骤",
        )
    if rule.requires_approval and not has_approval:
        raise FactoryError(
            "approval_missing",
            ErrorCategory.APPROVAL_REQUIRED,
            "缺少匹配审批",
            "transition",
            True,
            "请求并完成审批",
        )
    if rule.requires_evidence and not has_valid_evidence:
        raise FactoryError(
            "evidence_missing",
            ErrorCategory.IMPLEMENTATION_FAILED,
            "缺少当前有效证据",
            "transition",
            True,
            "登记并验证证据",
        )
    return rule
