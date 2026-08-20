from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from product_factory.contracts.models import (
    LockOwner,
    RequirementStatus,
    WorkflowState,
)
from product_factory.errors import FactoryError
from product_factory.services.initialize import check_inputs, initialize_project
from product_factory.services.mutations import commit_state_change
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


REQUIREMENT_KEYS = (
    "target_user_and_core_task",
    "input_process_output",
    "user_flow_and_confirmations",
    "scope_and_priority",
    "acceptance_criteria",
    "model_cost_platform",
    "data_privacy_performance_deployment",
)


def write_intake(
    path: Path,
    *,
    project_id: str = "demo-web",
    prd_confirmed: bool = True,
    missing: str | None = None,
    not_applicable: str | None = None,
) -> None:
    requirements = {
        key: {"status": "present", "source": f"PRD {index}"}
        for index, key in enumerate(REQUIREMENT_KEYS, start=1)
    }
    if missing:
        requirements[missing] = {"status": "missing", "source": "待补充"}
    if not_applicable:
        requirements[not_applicable] = {
            "status": "not_applicable",
            "source": "产品负责人确认",
            "reason": "本项目不适用",
        }
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "project_id": project_id,
                "prd_confirmed": prd_confirmed,
                "confirmed_by": "owner",
                "confirmed_at": "2026-08-20T00:00:00Z",
                "requirements": requirements,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def initialize(tmp_path: Path, **intake_options: object) -> Path:
    prd = tmp_path / "source-prd.md"
    prd.write_bytes(b"# Confirmed PRD\n")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake, **intake_options)
    target = tmp_path / "new-product"
    state = initialize_project(
        target=target,
        project_id="demo-web",
        name="Demo Web",
        prd_source=prd,
        intake_source=intake,
        stage_specs=[("stage-01", "Core flow", False)],
        factory_root=Path.cwd(),
    )
    assert state.workflow_state is WorkflowState.INITIALIZED
    return target


def lock_for_initial_state(root: Path) -> str:
    lock = LockManager(root).acquire(
        LockOwner(tool="pytest", session_id="test", pid=1, host="local"),
        state_revision=0,
        lease=timedelta(minutes=5),
    )
    return lock.lock_id


def test_initialize_copies_baseline_and_creates_protocol_files(tmp_path: Path) -> None:
    target = initialize(tmp_path)

    assert (target / "inputs/PRD.md").read_bytes() == b"# Confirmed PRD\n"
    assert (target / "inputs/constraints.md").read_bytes() == b""
    assert (target / "inputs/assets").is_dir()
    assert (target / ".product-factory/evidence").is_dir()
    assert (target / "docs").is_dir()
    assert (target / "backend").is_dir()
    assert (target / "frontend").is_dir()
    assert (target / ".product-factory/project.yaml").is_file()
    assert (target / ".product-factory/approvals.jsonl").read_text(encoding="utf-8") == ""
    assert (target / ".product-factory/events.jsonl").read_text(encoding="utf-8") == ""


def test_initialize_rejects_a_non_empty_target(tmp_path: Path) -> None:
    target = tmp_path / "new-product"
    target.mkdir()
    (target / "existing.txt").write_text("do not overwrite", encoding="utf-8")
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "intake.yaml"
    write_intake(intake)

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            target, "demo-web", "Demo Web", prd, intake, [("stage-01", "Core", False)], Path.cwd()
        )

    assert caught.value.code == "project_exists"
    assert (target / "existing.txt").read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.parametrize(
    ("intake_options", "expected_code"),
    [
        ({"prd_confirmed": False}, "prd_not_confirmed"),
        ({"missing": "scope_and_priority"}, "input_requirement_missing:scope_and_priority"),
    ],
)
def test_check_inputs_reports_required_declarations(
    tmp_path: Path, intake_options: dict[str, object], expected_code: str
) -> None:
    target = initialize(tmp_path, **intake_options)

    with pytest.raises(FactoryError) as caught:
        check_inputs(target, lock_for_initial_state(target), expected_revision=0)

    assert caught.value.code == expected_code
    assert caught.value.category.value == "input_required"
    assert caught.value.details["errors"][0] == expected_code


def test_check_inputs_reports_a_prd_digest_mismatch(tmp_path: Path) -> None:
    target = initialize(tmp_path)
    (target / "inputs/PRD.md").write_text("# Changed PRD\n", encoding="utf-8")

    with pytest.raises(FactoryError) as caught:
        check_inputs(target, lock_for_initial_state(target), expected_revision=0)

    assert caught.value.code == "prd_digest_mismatch"
    assert caught.value.details == {"errors": ["prd_digest_mismatch"]}


def test_check_inputs_accepts_reasoned_not_applicable_and_advances_state(tmp_path: Path) -> None:
    target = initialize(tmp_path, not_applicable="model_cost_platform")

    state = check_inputs(target, lock_for_initial_state(target), expected_revision=0)

    assert state.revision == 1
    assert state.workflow_state is WorkflowState.INPUTS_CHECKED
    assert ProjectRepository(target).read_events()[0].event_type == "inputs_checked"


def test_state_remains_committed_when_event_append_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = initialize(tmp_path)
    repo = ProjectRepository(target)
    current = repo.load_state()
    next_state = current.model_copy(update={"revision": 1, "updated_at": datetime.now(timezone.utc)})

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repo, "append_event", fail_append)
    with pytest.raises(OSError):
        commit_state_change(repo, current, next_state, "test_event", {"test": True})

    stored = repo.load_state()
    assert stored.revision == 1
    assert stored.last_event_id is not None
