from pathlib import Path

from product_factory.contracts.models import (
    ApprovalRecord,
    EvidenceManifest,
    EventRecord,
    IntakeRecord,
    ProjectRecord,
    StateRecord,
)
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.storage.files import (
    append_jsonl,
    atomic_write_json,
    atomic_write_yaml,
    contained_path,
    exclusive_create_json,
    load_json,
    load_yaml,
    read_jsonl,
)
from product_factory.storage.paths import ProjectPaths


class ProjectRepository:
    def __init__(self, root: Path):
        self.paths = ProjectPaths(root.resolve())
        self.paths.metadata.mkdir(parents=True, exist_ok=True)
        self.paths.approvals.touch(exist_ok=True)
        self.paths.events.touch(exist_ok=True)

    def load_state(self) -> StateRecord:
        return StateRecord.model_validate_json(self.paths.state.read_text(encoding="utf-8"))

    def write_initial_state(self, state: StateRecord) -> None:
        if self.paths.state.exists():
            raise FactoryError(
                "project_exists",
                ErrorCategory.POLICY_BLOCKED,
                "项目已经初始化",
                "init",
                False,
                "使用新目录",
            )
        atomic_write_json(self.paths.state, state.model_dump(mode="json"))

    def save_state(self, next_state: StateRecord, expected_revision: int) -> StateRecord:
        """Persist a state transition after the caller acquires the project lease."""
        current = self.load_state()
        if current.revision != expected_revision or next_state.revision != expected_revision + 1:
            raise FactoryError(
                "revision_conflict",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "状态已被其他会话修改",
                "save_state",
                True,
                "重新运行 status 或 resume",
                {"expected": expected_revision, "actual": current.revision},
            )
        atomic_write_json(self.paths.state, next_state.model_dump(mode="json"))
        return next_state

    def load_project(self) -> ProjectRecord:
        return ProjectRecord.model_validate(load_yaml(self.paths.project))

    def load_intake(self) -> IntakeRecord:
        return IntakeRecord.model_validate(load_yaml(self.paths.intake))

    def save_project(self, record: ProjectRecord) -> None:
        atomic_write_yaml(self.paths.project, record.model_dump(mode="json"))

    def save_intake(self, record: IntakeRecord) -> None:
        atomic_write_yaml(self.paths.intake, record.model_dump(mode="json"))

    def append_approval(self, record: ApprovalRecord) -> None:
        append_jsonl(self.paths.approvals, record.model_dump(mode="json"))

    def read_approvals(self) -> list[ApprovalRecord]:
        return [ApprovalRecord.model_validate(item) for item in read_jsonl(self.paths.approvals)]

    def append_event(self, record: EventRecord) -> None:
        append_jsonl(self.paths.events, record.model_dump(mode="json"))

    def read_events(self) -> list[EventRecord]:
        return [EventRecord.model_validate(item) for item in read_jsonl(self.paths.events)]

    def evidence_path(self, stage_id: str, evidence_id: str) -> Path:
        return contained_path(self.paths.evidence, f"{stage_id}/{evidence_id}/manifest.json")

    def save_evidence(self, record: EvidenceManifest) -> Path:
        path = self.evidence_path(record.stage_id, record.evidence_id)
        if not exclusive_create_json(path, record.model_dump(mode="json")):
            raise FactoryError(
                "evidence_exists",
                ErrorCategory.POLICY_BLOCKED,
                "证据 ID 已存在",
                "record_evidence",
                False,
                "使用新的 evidence_id",
            )
        return path

    def load_evidence(self, stage_id: str, evidence_id: str) -> EvidenceManifest:
        return EvidenceManifest.model_validate(load_json(self.evidence_path(stage_id, evidence_id)))
