import pytest

from product_factory.contracts.models import CompletionLevel, WorkflowState
from product_factory.domain.states import require_transition
from product_factory.errors import FactoryError


def test_cannot_skip_adaptation_approval() -> None:
    """Removing the approval gate must still block direct development entry."""
    with pytest.raises(FactoryError) as caught:
        require_transition(
            WorkflowState.INPUTS_CHECKED,
            WorkflowState.STAGE_DEVELOPMENT,
            CompletionLevel.NONE,
            has_approval=False,
            has_valid_evidence=False,
        )

    assert caught.value.code == "transition_not_allowed"


def test_later_protocol_state_is_recognized_but_unsupported() -> None:
    """Treating future milestone states as implemented would bypass its policy."""
    with pytest.raises(FactoryError) as caught:
        require_transition(
            WorkflowState.NEXT_STAGE_OR_FRONTEND,
            WorkflowState.RELEASE_READY,
            CompletionLevel.HUMAN_ACCEPTED,
            has_approval=True,
            has_valid_evidence=True,
        )

    assert caught.value.code == "unsupported_transition"


@pytest.mark.parametrize(
    ("completion", "has_approval", "has_valid_evidence", "code"),
    [
        (CompletionLevel.NONE, True, True, "completion_mismatch"),
        (CompletionLevel.SYSTEM_VERIFIED, True, False, "evidence_missing"),
    ],
)
def test_human_acceptance_gate_enforces_its_required_inputs(
    completion: CompletionLevel,
    has_approval: bool,
    has_valid_evidence: bool,
    code: str,
) -> None:
    """Omitting either verification or evidence must block acceptance entry."""
    with pytest.raises(FactoryError) as caught:
        require_transition(
            WorkflowState.SYSTEM_VERIFICATION,
            WorkflowState.HUMAN_ACCEPTANCE_PENDING,
            completion,
            has_approval=has_approval,
            has_valid_evidence=has_valid_evidence,
        )

    assert caught.value.code == code


def test_matching_adaptation_approval_allows_development() -> None:
    """The allowed milestone transition must remain usable after gate checks."""
    rule = require_transition(
        WorkflowState.ADAPTATION_PENDING_APPROVAL,
        WorkflowState.STAGE_DEVELOPMENT,
        CompletionLevel.NONE,
        has_approval=True,
        has_valid_evidence=False,
    )

    assert rule.requires_approval is True
    assert rule.requires_evidence is False
