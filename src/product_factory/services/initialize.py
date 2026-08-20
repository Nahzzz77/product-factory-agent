"""New-project initialization and deterministic PRD intake validation."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
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
from product_factory.services.mutations import commit_state_change
from product_factory.storage.files import load_yaml
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository
from product_factory.version import __version__


def copy_baseline(source: Path, destination: Path) -> str:
    """Copy exact bytes and return their SHA-256 digest."""
    content = source.read_bytes()
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

    factory_root = factory_root.resolve()
    resolved_intake = _source_path(intake_source, factory_root)
    resolved_prd = _source_path(prd_source, factory_root)
    resolved_constraints = (
        _source_path(constraints_source, factory_root) if constraints_source is not None else None
    )
    _require_valid_intake(resolved_intake)
    target.mkdir(parents=True, exist_ok=True)
    for directory in (
        target / "inputs/assets",
        target / ".product-factory/evidence",
        target / "docs",
        target / "backend",
        target / "frontend",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    prd_digest = copy_baseline(resolved_prd, target / "inputs/PRD.md")
    copy_baseline(resolved_intake, target / ".product-factory/intake.yaml")
    constraints_path = target / "inputs/constraints.md"
    if resolved_constraints is None:
        constraints_path.write_bytes(b"")
    else:
        copy_baseline(resolved_constraints, constraints_path)

    stages = _stage_plan(stage_specs)
    handbooks = _load_handbooks(factory_root)
    repository = ProjectRepository(target)
    repository.save_project(
        ProjectRecord(
            schema_version="1.0",
            project_id=project_id,
            name=name,
            created_at=datetime.now(timezone.utc),
            factory_version=__version__,
            prd=PrdReference(path="inputs/PRD.md", sha256=prd_digest),
            constraints_path="inputs/constraints.md",
            handbooks=handbooks,
            stage_plan=stages,
            source_excludes=[],
        )
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
        updated_at=datetime.now(timezone.utc),
    )
    repository.write_initial_state(initial)
    return initial


def collect_input_errors(repo: ProjectRepository) -> list[str]:
    """Return input findings in stable protocol order without changing any records."""
    project = repo.load_project()
    intake = repo.load_intake()
    errors: list[str] = []
    if intake.project_id != project.project_id:
        errors.append("project_id_mismatch")
    if not intake.prd_confirmed:
        errors.append("prd_not_confirmed")
    prd_path = repo.paths.root / project.prd.path
    try:
        actual_digest = hashlib.sha256(prd_path.read_bytes()).hexdigest()
    except OSError:
        errors.append("prd_unreadable")
    else:
        if actual_digest != project.prd.sha256:
            errors.append("prd_digest_mismatch")
    for key in sorted(intake.requirements):
        if intake.requirements[key].status is RequirementStatus.MISSING:
            errors.append(f"input_requirement_missing:{key}")
    return errors


def check_inputs(root: Path, lock_id: str, expected_revision: int = 0) -> StateRecord:
    """Validate declared inputs and atomically advance an initialized project once."""
    root = root.resolve()
    manager = LockManager(root)
    with manager.mutation(lock_id, expected_revision):
        repo = ProjectRepository(root)
        current = repo.load_state()
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


def _require_valid_intake(path: Path) -> None:
    try:
        IntakeRecord.model_validate(load_yaml(path))
    except (OSError, ValidationError, ValueError, yaml.YAMLError) as exc:
        raise FactoryError(
            "intake_invalid",
            ErrorCategory.INPUT_REQUIRED,
            "intake 声明无效或无法读取",
            "init",
            False,
            "修正 intake.yaml 的七类输入声明后重试",
        ) from exc


def _stage_plan(stage_specs: Iterable[tuple[str, str, bool]]) -> list[StagePlanItem]:
    return [
        StagePlanItem(
            id=stage_id,
            name=name,
            sequence=sequence,
            requires_real_model=requires_real_model,
        )
        for sequence, (stage_id, name, requires_real_model) in enumerate(stage_specs, start=1)
    ]


def _load_handbooks(factory_root: Path) -> list[HandbookReference]:
    manifest_path = factory_root / "references/handbooks/manifest.yaml"
    manifest = load_yaml(manifest_path)
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise ValueError("handbook manifest documents must be a list")
    handbooks: list[HandbookReference] = []
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("handbook manifest entry must be a mapping")
        relative_path = document["path"]
        if not isinstance(relative_path, str):
            raise ValueError("handbook path must be a string")
        handbooks.append(
            HandbookReference(
                title=document["title"],
                version=document["version"],
                path=relative_path,
                sha256=hashlib.sha256((factory_root / relative_path).read_bytes()).hexdigest(),
            )
        )
    return handbooks
