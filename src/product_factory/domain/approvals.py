"""Approval records must bind exactly to their waiting workflow gate."""

from product_factory.contracts.models import ApprovalRecord, WaitingOn
from product_factory.errors import ErrorCategory, FactoryError


APPROVAL_STATEMENT = "验收通过，批准进入下一阶段。"


def require_exact_approval(
    record: ApprovalRecord,
    waiting: WaitingOn,
    current_revision: int,
) -> None:
    """Reject an approval unless every gate-binding attribute matches."""
    matches = (
        record.statement == APPROVAL_STATEMENT
        and record.request_id == waiting.request_id
        and record.gate_type is waiting.gate_type
        and record.scope == waiting.scope
        and record.state_revision == current_revision
        and record.consumed_by_revision == current_revision + 1
    )
    if not matches:
        raise FactoryError(
            "approval_mismatch",
            ErrorCategory.APPROVAL_REQUIRED,
            "审批与当前门禁不匹配",
            "approve",
            True,
            "重新请求当前门禁审批",
        )
