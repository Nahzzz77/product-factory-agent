from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowState(StrEnum):
    INITIALIZED = "initialized"
    INPUTS_CHECKED = "inputs_checked"
    ADAPTATION_PENDING_APPROVAL = "adaptation_pending_approval"
    STAGE_DEVELOPMENT = "stage_development"
    SYSTEM_VERIFICATION = "system_verification"
    HUMAN_ACCEPTANCE_PENDING = "human_acceptance_pending"
    NEXT_STAGE_OR_FRONTEND = "next_stage_or_frontend"
    RELEASE_READY = "release_ready"
    DEPLOYMENT_PENDING_APPROVAL = "deployment_pending_approval"
    DEPLOYED_PENDING_ACCEPTANCE = "deployed_pending_acceptance"
    PRODUCTION_ACCEPTED = "production_accepted"
    OBSERVING = "observing"


class CompletionLevel(StrEnum):
    NONE = "none"
    IMPLEMENTED = "implemented"
    SYSTEM_VERIFIED = "system_verified"
    HUMAN_ACCEPTED = "human_accepted"


class GateType(StrEnum):
    TECHNICAL_ADAPTATION = "technical_adaptation"
    STAGE_ACCEPTANCE = "stage_acceptance"


class RequirementStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class RequirementDeclaration(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"status": {"const": "not_applicable"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {"reason": {"type": "string", "pattern": r"\S"}},
                        "required": ["reason"],
                    },
                }
            ]
        },
    )
    status: RequirementStatus
    source: str = Field(min_length=1)
    reason: str | None = None

    @model_validator(mode="after")
    def require_reason_for_not_applicable(self):
        if self.status is RequirementStatus.NOT_APPLICABLE and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("not_applicable requires reason")
        return self


REQUIREMENT_KEYS = frozenset(
    {
        "target_user_and_core_task",
        "input_process_output",
        "user_flow_and_confirmations",
        "scope_and_priority",
        "acceptance_criteria",
        "model_cost_platform",
        "data_privacy_performance_deployment",
    }
)


class IntakeRecord(StrictModel):
    schema_version: Literal["1.0"]
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    prd_confirmed: bool
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime
    requirements: dict[str, RequirementDeclaration]

    @model_validator(mode="after")
    def require_exact_categories(self):
        if set(self.requirements) != REQUIREMENT_KEYS:
            raise ValueError("requirements must contain the seven protocol categories")
        return self


class PrdReference(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HandbookReference(StrictModel):
    title: str
    version: str
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StagePlanItem(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    name: str
    sequence: int = Field(ge=1)
    kind: Literal["development", "frontend"] = "development"
    requires_real_model: bool = False


class ProjectRecord(StrictModel):
    schema_version: Literal["1.0"]
    project_id: str
    name: str
    created_at: datetime
    factory_version: str
    prd: PrdReference
    constraints_path: str
    handbooks: list[HandbookReference]
    stage_plan: list[StagePlanItem] = Field(min_length=1)
    source_excludes: list[str]

    @model_validator(mode="after")
    def require_unique_ordered_stages(self):
        ids = [item.id for item in self.stage_plan]
        sequences = [item.sequence for item in self.stage_plan]
        if len(ids) != len(set(ids)) or sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("stage_plan requires unique ids and contiguous sequence values")
        return self


class CurrentStage(StrictModel):
    id: str
    sequence: int = Field(ge=1)
    completion_level: CompletionLevel


class WaitingOn(StrictModel):
    type: Literal["approval"]
    request_id: str
    gate_type: GateType
    scope: dict[str, Any]


class StateRecord(StrictModel):
    schema_version: Literal["1.0"]
    project_id: str
    revision: int = Field(ge=0)
    workflow_state: WorkflowState
    current_stage: CurrentStage
    waiting_on: WaitingOn | None = None
    last_valid_evidence_id: str | None = None
    last_event_id: str | None = None
    updated_at: datetime


class ApprovalRecord(StrictModel):
    schema_version: Literal["1.0"]
    approval_id: str
    request_id: str
    gate_type: GateType
    scope: dict[str, Any]
    state_revision: int
    statement: str
    actor: str
    source: Literal["interactive_cli"]
    created_at: datetime
    consumed_by_revision: int


class EventRecord(StrictModel):
    schema_version: Literal["1.0"]
    event_id: str
    event_type: str
    project_id: str
    before_revision: int
    after_revision: int
    created_at: datetime
    details: dict[str, Any]


class LockOwner(StrictModel):
    tool: str
    session_id: str
    pid: int
    host: str


class LockRecord(StrictModel):
    schema_version: Literal["1.0"]
    lock_id: str
    owner: LockOwner
    acquired_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime
    state_revision: int


class EvidenceCheck(StrictModel):
    name: str
    command: str
    started_at: datetime
    ended_at: datetime
    exit_status: int
    summary: str
    mode: Literal["not_applicable", "mock", "real"]
    artifact_paths: list[str] = Field(default_factory=list)


class KnownIssue(StrictModel):
    summary: str
    severity: Literal["low", "medium", "high", "critical"]
    blocking: bool


class EvidenceManifest(StrictModel):
    schema_version: Literal["1.0"]
    evidence_id: str
    stage_id: str
    state_revision: int
    factory_version: str
    prd_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_entry: str | None = None
    checks: list[EvidenceCheck] = Field(min_length=1)
    known_issues: list[KnownIssue] = Field(default_factory=list)
    ready_for_human_acceptance: bool


class ResultEnvelope(StrictModel):
    ok: bool
    code: str
    category: str | None
    message: str
    step: str
    retryable: bool
    action: str
    details: dict[str, Any]
