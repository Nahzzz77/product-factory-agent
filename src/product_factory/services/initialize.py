"""New-project initialization and deterministic PRD intake validation."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PureWindowsPath
from pathlib import PurePosixPath
from typing import Iterable

import yaml
from pydantic import ValidationError

from product_factory.contracts.models import (
    CompletionLevel,
    CurrentStage,
    HandbookReference,
    IntakeRecord,
    ProjectRecord,
    PrdReference,
    RequirementStatus,
    StagePlanItem,
    StateRecord,
    WorkflowState,
)
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.domain.states import require_transition
from product_factory.services.mutations import commit_state_change
from product_factory.storage.files import load_yaml, read_contained_regular_bytes
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository
from product_factory.version import __version__


def copy_baseline(source: Path, destination: Path) -> str:
    """Copy exact bytes and return their SHA-256 digest."""
    return _copy_baseline_bytes(source.read_bytes(), destination)


def _copy_baseline_bytes(content: bytes, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def initialize_project(
    target: Path,
    project_id: str,
    name: str,
    prd_source: Path,
    intake_source: Path,
    stage_specs: Iterable[tuple[str, str, bool]],
    factory_root: Path,
    constraints_source: Path | None = None,
) -> StateRecord:
    """Create a fresh managed Web-product directory without adopting existing content."""
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise FactoryError(
            "project_exists",
            ErrorCategory.POLICY_BLOCKED,
            "目标目录非空，不能覆盖已有项目",
            "init",
            False,
            "使用新的空目录",
        )

    prepared = _prepare_project(
        project_id=project_id,
        name=name,
        prd_source=prd_source,
        intake_source=intake_source,
        stage_specs=stage_specs,
        factory_root=factory_root.resolve(),
        constraints_source=constraints_source,
    )

    # No target mutation occurs before every source byte, handbook, protocol model,
    # and stage item has been successfully read and validated above.
    target.mkdir(parents=True, exist_ok=True)
    for directory in (
        target / "inputs/assets",
        target / ".product-factory/evidence",
        target / "docs",
        target / "backend",
        target / "frontend",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    # Repository construction is deliberately read-only.  Initialize the protocol
    # directory and its two empty append-only logs explicitly after input validation.
    metadata = target / ".product-factory"
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "approvals.jsonl").write_bytes(b"")
    (metadata / "events.jsonl").write_bytes(b"")

    _copy_baseline_bytes(prepared.prd_bytes, target / "inputs/PRD.md")
    _copy_baseline_bytes(prepared.intake_bytes, target / ".product-factory/intake.yaml")
    constraints_path = target / "inputs/constraints.md"
    _copy_baseline_bytes(prepared.constraints_bytes, constraints_path)

    repository = ProjectRepository(target)
    repository.save_project(prepared.project)
    repository.write_initial_state(prepared.initial_state)
    return prepared.initial_state


@dataclass(frozen=True, slots=True)
class _PreparedProject:
    intake_bytes: bytes
    prd_bytes: bytes
    constraints_bytes: bytes
    project: ProjectRecord
    initial_state: StateRecord


def _prepare_project(
    *,
    project_id: str,
    name: str,
    prd_source: Path,
    intake_source: Path,
    stage_specs: Iterable[tuple[str, str, bool]],
    factory_root: Path,
    constraints_source: Path | None,
) -> _PreparedProject:
    """Read and validate every potentially failing initialization input before writes."""
    resolved_intake = _source_path(intake_source, factory_root)
    resolved_prd = _source_path(prd_source, factory_root)
    resolved_constraints = (
        _source_path(constraints_source, factory_root) if constraints_source is not None else None
    )
    intake_bytes = _read_valid_intake(resolved_intake)
    prd_bytes = _read_source_bytes(resolved_prd, "prd_unreadable", "PRD 基线文件不可读取")
    constraints_bytes = (
        b""
        if resolved_constraints is None
        else _read_source_bytes(resolved_constraints, "constraints_unreadable", "约束基线文件不可读取")
    )
    stages = _stage_plan(stage_specs)
    handbooks = _load_handbooks(factory_root)
    now = datetime.now(timezone.utc)
    try:
        project = ProjectRecord(
            schema_version="1.0",
            project_id=project_id,
            name=name,
            created_at=now,
            factory_version=__version__,
            prd=PrdReference(path="inputs/PRD.md", sha256=hashlib.sha256(prd_bytes).hexdigest()),
            constraints_path="inputs/constraints.md",
            handbooks=handbooks,
            stage_plan=stages,
            source_excludes=[],
        )
        initial = StateRecord(
            schema_version="1.0",
            project_id=project_id,
            revision=0,
            workflow_state=WorkflowState.INITIALIZED,
            current_stage=CurrentStage(
                id=stages[0].id,
                sequence=stages[0].sequence,
                completion_level=CompletionLevel.NONE,
            ),
            updated_at=now,
        )
    except (ValidationError, ValueError, IndexError) as exc:
        raise FactoryError(
            "project_definition_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "项目定义或阶段计划无效",
            "init",
            False,
            "修正项目 ID、名称或阶段计划后重试",
        ) from exc
    return _PreparedProject(intake_bytes, prd_bytes, constraints_bytes, project, initial)


def collect_input_errors(repo: ProjectRepository) -> list[str]:
    """Return input findings in stable protocol order without changing any records."""
    project = repo.load_project()
    intake = repo.load_intake()
    errors: list[str] = []
    if intake.project_id != project.project_id:
        errors.append("project_id_mismatch")
    if not intake.prd_confirmed:
        errors.append("prd_not_confirmed")
    try:
        require_current_prd(repo, project, "check_inputs")
    except FactoryError as exc:
        errors.append(exc.code)
    for key in sorted(intake.requirements):
        if intake.requirements[key].status is RequirementStatus.MISSING:
            errors.append(f"input_requirement_missing:{key}")
    return errors


def canonical_root_relative_parts(value: str) -> tuple[str, ...]:
    """Parse one strict protocol path without accepting aliases or escapes."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("path must use canonical POSIX separators")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or not parsed.parts or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("path is not root-relative")
    if parsed.as_posix() != value:
        raise ValueError("path is not canonical")
    return parsed.parts


def read_prd_baseline_bytes(repo: ProjectRepository, project: ProjectRecord, step: str) -> bytes:
    """Read the declared PRD through the shared descriptor-safe containment boundary."""
    try:
        parts = canonical_root_relative_parts(project.prd.path)
    except (TypeError, ValueError) as exc:
        raise FactoryError(
            "prd_path_invalid", ErrorCategory.INPUT_REQUIRED, "PRD 基线路径无效", step,
            False, "将 PRD 路径改为项目目录内的规范相对路径",
        ) from exc
    try:
        return read_contained_regular_bytes(repo.paths.root, parts)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FactoryError(
            "prd_unreadable", ErrorCategory.INPUT_REQUIRED, "PRD 基线文件不可安全读取", step,
            False, "修复项目内 PRD 文件后重试",
        ) from exc


def require_current_prd(repo: ProjectRepository, project: ProjectRecord, step: str) -> bytes:
    """Return current PRD bytes only when they still equal the recorded baseline."""
    content = read_prd_baseline_bytes(repo, project, step)
    if hashlib.sha256(content).hexdigest() != project.prd.sha256:
        raise FactoryError(
            "prd_digest_mismatch", ErrorCategory.INPUT_REQUIRED, "PRD 已偏离确认的基线", step,
            False, "恢复确认过的 PRD 内容，或重新初始化项目",
        )
    return content


def check_inputs(root: Path, lock_id: str, expected_revision: int = 0) -> StateRecord:
    """Validate declared inputs and atomically advance an initialized project once."""
    root = root.resolve()
    manager = LockManager(root)
    with manager.mutation(lock_id, expected_revision):
        repo = ProjectRepository(root)
        project = repo.load_project()
        current = repo.load_state()
        if current.project_id != project.project_id:
            raise FactoryError(
                "project_identity_mismatch", ErrorCategory.ENVIRONMENT_BLOCKED,
                "项目状态与项目元数据的标识不一致", "check_inputs", False,
                "修复项目元数据后重试",
            )
        if current.revision != expected_revision:
            raise FactoryError(
                "revision_conflict",
                ErrorCategory.ENVIRONMENT_BLOCKED,
                "状态已被其他会话修改",
                "check_inputs",
                True,
                "重新运行 status 或 resume",
                {"expected": expected_revision, "actual": current.revision},
            )
        if (
            expected_revision != 0
            or current.workflow_state is not WorkflowState.INITIALIZED
            or current.current_stage.completion_level is not CompletionLevel.NONE
            or current.waiting_on is not None
        ):
            raise FactoryError(
                "transition_not_allowed", ErrorCategory.POLICY_BLOCKED,
                "当前状态不能确认项目输入", "check_inputs", False,
                "仅可在初始化完成后的第一个步骤执行输入检查",
            )
        require_transition(
            current.workflow_state, WorkflowState.INPUTS_CHECKED,
            current.current_stage.completion_level, has_approval=False, has_valid_evidence=False,
        )
        errors = collect_input_errors(repo)
        if errors:
            raise FactoryError(
                errors[0],
                ErrorCategory.INPUT_REQUIRED,
                "项目输入尚未满足最低要求",
                "check_inputs",
                False,
                "补齐 PRD 或 intake 声明后重试",
                {"errors": errors},
            )
        next_state = current.model_copy(
            update={
                "revision": expected_revision + 1,
                "workflow_state": WorkflowState.INPUTS_CHECKED,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return commit_state_change(repo, current, next_state, "inputs_checked", {"errors": []})


def _source_path(source: Path, factory_root: Path) -> Path:
    return source.resolve() if source.is_absolute() else (factory_root / source).resolve()


def _read_valid_intake(path: Path) -> bytes:
    try:
        content = path.read_bytes()
        payload = yaml.safe_load(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"expected mapping in {path}")
        IntakeRecord.model_validate(payload)
    except (OSError, UnicodeDecodeError, ValidationError, ValueError, yaml.YAMLError) as exc:
        raise FactoryError(
            "intake_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "intake 声明无效或无法读取",
            "init",
            False,
            "修正 intake.yaml 的七类输入声明后重试",
        ) from exc
    return content


def _read_source_bytes(path: Path, code: str, message: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FactoryError(
            code,
            ErrorCategory.INPUT_REQUIRED,
            message,
            "init",
            False,
            "修正或提供可读的基线文件后重试",
        ) from exc


def _stage_plan(stage_specs: Iterable[tuple[str, str, bool]]) -> list[StagePlanItem]:
    try:
        specs = list(stage_specs)
        stages = [
            StagePlanItem(
                id=stage_id,
                name=name,
                sequence=sequence,
                requires_real_model=requires_real_model,
            )
            for sequence, (stage_id, name, requires_real_model) in enumerate(specs, start=1)
        ]
    except (TypeError, ValueError, ValidationError) as exc:
        raise FactoryError(
            "stage_plan_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "阶段计划格式无效",
            "init",
            False,
            "提供至少一个合法阶段",
        ) from exc
    if not stages:
        raise FactoryError(
            "stage_plan_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "阶段计划不能为空",
            "init",
            False,
            "提供至少一个合法阶段",
        )
    return stages


def _load_handbooks(factory_root: Path) -> list[HandbookReference]:
    manifest_path = factory_root / "references/handbooks/manifest.yaml"
    try:
        manifest = load_yaml(manifest_path)
        if manifest.get("schema_version") != "1.0":
            raise ValueError("unsupported handbook manifest schema")
        documents = manifest.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ValueError("handbook manifest documents must be a non-empty list")
        handbooks: list[HandbookReference] = []
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("handbook manifest entry must be a mapping")
            relative_path, parts = _safe_handbook_path(document["path"])
            content = read_contained_regular_bytes(factory_root, parts)
            digest = hashlib.sha256(content).hexdigest()
            if document.get("sha256") != digest:
                raise ValueError("handbook digest mismatch")
            handbooks.append(
                HandbookReference(
                    title=document["title"],
                    version=document["version"],
                    path=relative_path,
                    sha256=digest,
                )
            )
    except (KeyError, OSError, RuntimeError, TypeError, ValidationError, ValueError, yaml.YAMLError) as exc:
        raise FactoryError(
            "handbook_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "技术手册清单或内容无效",
            "init",
            False,
            "修复手册清单及其引用文件后重试",
        ) from exc
    return handbooks


def _safe_handbook_path(value: object) -> tuple[str, tuple[str, ...]]:
    """Return only canonical relative components; descriptor reading verifies containment."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("handbook path must be a canonical relative POSIX path")
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise ValueError("handbook path must not be absolute")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("handbook path contains an unsafe component")
    canonical = "/".join(parts)
    return canonical, parts
