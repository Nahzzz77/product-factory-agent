import os
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
    _fsync_parent_directory,
    append_jsonl,
    atomic_write_json,
    atomic_write_yaml,
    contained_path,
    load_json,
    load_yaml,
    read_jsonl,
)
from product_factory.storage.paths import ProjectPaths


class ProjectRepository:
    def __init__(self, root: Path):
        self.paths = ProjectPaths(root.resolve())

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
        return self._evidence_directory(stage_id, evidence_id) / "manifest.json"

    def save_evidence(self, record: EvidenceManifest) -> Path:
        directory = self._evidence_directory(record.stage_id, record.evidence_id)
        try:
            directory.parent.mkdir(parents=True, exist_ok=True)
            # ``mkdir`` is the reservation point: it neither reuses an empty
            # pre-existing evidence directory nor replaces any populated one.
            os.mkdir(directory)
        except FileExistsError as exc:
            raise FactoryError(
                "evidence_exists",
                ErrorCategory.POLICY_BLOCKED,
                "证据 ID 已存在",
                "record_evidence",
                False,
                "使用新的 evidence_id",
            ) from exc
        # Persist the newly-created directory entry before manifest publication.
        # Unsupported Windows directory handles are tolerated by the shared
        # Task 3 helper; every other durability error leaves this ID reserved.
        _fsync_parent_directory(directory)
        path = directory / "manifest.json"
        # If durable publication fails, retain the reservation.  Future calls
        # must use a new ID rather than overwriting an interrupted evidence run.
        atomic_write_json(path, record.model_dump(mode="json"))
        return path

    def load_evidence(self, stage_id: str, evidence_id: str) -> EvidenceManifest:
        return EvidenceManifest.model_validate(load_json(self.evidence_path(stage_id, evidence_id)))

    def _evidence_directory(self, stage_id: str, evidence_id: str) -> Path:
        if not _is_path_component(stage_id) or not _is_path_component(evidence_id):
            raise FactoryError(
                "evidence_identifier_invalid",
                ErrorCategory.INPUT_REQUIRED,
                "证据阶段和 ID 必须是单个安全路径名称",
                "record_evidence",
                False,
                "使用不含路径分隔符的新 evidence_id",
            )
        return contained_path(self.paths.evidence, f"{stage_id}/{evidence_id}")


def _is_path_component(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value
