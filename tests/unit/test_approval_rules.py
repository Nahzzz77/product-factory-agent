from datetime import UTC, datetime

import pytest

from product_factory.contracts.models import ApprovalRecord, GateType, WaitingOn
from product_factory.domain.approvals import APPROVAL_STATEMENT, require_exact_approval
from product_factory.errors import FactoryError


def _waiting() -> WaitingOn:
    return WaitingOn(
        type="approval",
        request_id="request-123",
        gate_type=GateType.STAGE_ACCEPTANCE,
        scope={"stage_id": "api-core", "files": ["src/api.py"]},
    )


def _approval(**overrides: object) -> ApprovalRecord:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "approval_id": "approval-123",
        "request_id": "request-123",
        "gate_type": GateType.STAGE_ACCEPTANCE,
        "scope": {"stage_id": "api-core", "files": ["src/api.py"]},
        "state_revision": 7,
        "statement": APPROVAL_STATEMENT,
        "actor": "operator",
        "source": "interactive_cli",
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "consumed_by_revision": 8,
    }
    values.update(overrides)
    return ApprovalRecord(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"statement": "批准进入下一阶段"},
        {"request_id": "other-request"},
        {"gate_type": GateType.TECHNICAL_ADAPTATION},
        {"scope": {"stage_id": "web"}},
        {"state_revision": 6},
        {"consumed_by_revision": 9},
    ],
)
def test_mismatched_approval_cannot_satisfy_current_gate(
    overrides: dict[str, object],
) -> None:
    """Each signed gate attribute prevents reusing an unrelated approval."""
    with pytest.raises(FactoryError) as caught:
        require_exact_approval(_approval(**overrides), _waiting(), current_revision=7)

    assert caught.value.code == "approval_mismatch"


def test_exact_approval_satisfies_current_gate() -> None:
    """A record signed for this exact revision, request, and scope is accepted."""
    require_exact_approval(_approval(), _waiting(), current_revision=7)
