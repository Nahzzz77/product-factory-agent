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


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkflowState.INITIALIZED, WorkflowState.RELEASE_READY),
        (WorkflowState.NEXT_STAGE_OR_FRONTEND, WorkflowState.INPUTS_CHECKED),
        (WorkflowState.RELEASE_READY, WorkflowState.DEPLOYMENT_PENDING_APPROVAL),
        (WorkflowState.DEPLOYMENT_PENDING_APPROVAL, WorkflowState.DEPLOYED_PENDING_ACCEPTANCE),
        (WorkflowState.DEPLOYED_PENDING_ACCEPTANCE, WorkflowState.PRODUCTION_ACCEPTED),
        (WorkflowState.PRODUCTION_ACCEPTED, WorkflowState.OBSERVING),
        (WorkflowState.OBSERVING, WorkflowState.OBSERVING),
    ],
)
def test_post_milestone_one_protocol_states_are_unsupported(
    current: WorkflowState, target: WorkflowState
) -> None:
    """Routing toward any unimplemented protocol phase must not look invalid."""
    with pytest.raises(FactoryError) as caught:
        require_transition(
            current,
            target,
            CompletionLevel.HUMAN_ACCEPTED,
            has_approval=True,
            has_valid_evidence=True,
        )

    assert caught.value.code == "unsupported_transition"


@pytest.mark.parametrize(
    ("current", "target", "completion", "has_approval", "has_valid_evidence"),
    [
        (WorkflowState.INITIALIZED, WorkflowState.INPUTS_CHECKED, CompletionLevel.NONE, False, False),
        (
            WorkflowState.INPUTS_CHECKED,
            WorkflowState.ADAPTATION_PENDING_APPROVAL,
            CompletionLevel.NONE,
            False,
            False,
        ),
        (
            WorkflowState.ADAPTATION_PENDING_APPROVAL,
            WorkflowState.STAGE_DEVELOPMENT,
            CompletionLevel.NONE,
            True,
            False,
        ),
        (
            WorkflowState.STAGE_DEVELOPMENT,
            WorkflowState.SYSTEM_VERIFICATION,
            CompletionLevel.NONE,
            False,
            False,
        ),
        (
            WorkflowState.SYSTEM_VERIFICATION,
            WorkflowState.HUMAN_ACCEPTANCE_PENDING,
            CompletionLevel.SYSTEM_VERIFIED,
            False,
            True,
        ),
        (
            WorkflowState.HUMAN_ACCEPTANCE_PENDING,
            WorkflowState.NEXT_STAGE_OR_FRONTEND,
            CompletionLevel.SYSTEM_VERIFIED,
            True,
            True,
        ),
    ],
)
def test_all_milestone_one_transitions_accept_exactly_their_gates(
    current: WorkflowState,
    target: WorkflowState,
    completion: CompletionLevel,
    has_approval: bool,
    has_valid_evidence: bool,
) -> None:
    """Each promised milestone-one edge remains available with its exact inputs."""
    assert require_transition(
        current,
        target,
        completion,
        has_approval=has_approval,
        has_valid_evidence=has_valid_evidence,
    )


@pytest.mark.parametrize(
    ("completion", "has_approval", "has_valid_evidence", "code", "category"),
    [
        (
            CompletionLevel.IMPLEMENTED,
            False,
            False,
            "completion_mismatch",
            "implementation_failed",
        ),
        (
            CompletionLevel.SYSTEM_VERIFIED,
            False,
            False,
            "approval_missing",
            "approval_required",
        ),
        (
            CompletionLevel.SYSTEM_VERIFIED,
            True,
            False,
            "evidence_missing",
            "implementation_failed",
        ),
    ],
)
def test_final_gate_reports_first_unsatisfied_requirement_in_rule_order(
    completion: CompletionLevel,
    has_approval: bool,
    has_valid_evidence: bool,
    code: str,
    category: str,
) -> None:
    """Changing error precedence must not hide the next user action."""
    with pytest.raises(FactoryError) as caught:
        require_transition(
            WorkflowState.HUMAN_ACCEPTANCE_PENDING,
            WorkflowState.NEXT_STAGE_OR_FRONTEND,
            completion,
            has_approval=has_approval,
            has_valid_evidence=has_valid_evidence,
        )

    assert caught.value.code == code
    assert caught.value.category == category
