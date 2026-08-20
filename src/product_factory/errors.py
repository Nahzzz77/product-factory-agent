from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    INPUT_REQUIRED = "input_required"
    APPROVAL_REQUIRED = "approval_required"
    IMPLEMENTATION_FAILED = "implementation_failed"
    EXTERNAL_SERVICE_FAILED = "external_service_failed"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    POLICY_BLOCKED = "policy_blocked"
    INTERRUPTED = "interrupted"


EXIT_BY_CATEGORY = {
    ErrorCategory.INPUT_REQUIRED: 2,
    ErrorCategory.APPROVAL_REQUIRED: 3,
    ErrorCategory.ENVIRONMENT_BLOCKED: 4,
    ErrorCategory.IMPLEMENTATION_FAILED: 5,
    ErrorCategory.POLICY_BLOCKED: 6,
    ErrorCategory.EXTERNAL_SERVICE_FAILED: 10,
    ErrorCategory.INTERRUPTED: 10,
}


@dataclass(slots=True)
class FactoryError(Exception):
    code: str
    category: ErrorCategory
    message: str
    step: str
    retryable: bool
    action: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CATEGORY[self.category]
