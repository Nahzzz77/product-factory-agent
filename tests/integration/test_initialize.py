from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import pytest
import yaml

from product_factory.contracts.models import (
    LockOwner,
    RequirementStatus,
    WorkflowState,
)
from product_factory.errors import FactoryError
from product_factory.services import initialize as initialize_service
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


def test_initialize_rejects_invalid_intake_before_creating_target(tmp_path: Path) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "invalid-intake.yaml"
    intake.write_text("schema_version: '1.0'\nproject_id: demo-web\n", encoding="utf-8")
    target = tmp_path / "new-product"

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            target, "demo-web", "Demo Web", prd, intake, [("stage-01", "Core", False)], Path.cwd()
        )

    assert caught.value.code == "intake_invalid"
    assert not target.exists()


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


def test_check_inputs_rejects_whitespace_not_applicable_reason(tmp_path: Path) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake, not_applicable="model_cost_platform")
    contents = intake.read_text(encoding="utf-8").replace("reason: 本项目不适用", "reason: '   '")
    intake.write_text(contents, encoding="utf-8")

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            tmp_path / "new-product",
            "demo-web",
            "Demo Web",
            prd,
            intake,
            [("stage-01", "Core", False)],
            Path.cwd(),
        )

    assert caught.value.code == "intake_invalid"


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


def test_state_commit_does_not_leave_a_partial_event_line_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = initialize(tmp_path)
    repo = ProjectRepository(target)
    current = repo.load_state()
    next_state = current.model_copy(update={"revision": 1, "updated_at": datetime.now(timezone.utc)})
    from product_factory.storage import files

    original_write = files.os.write
    wrote_once = False

    def partial_then_fail(descriptor: int, content: bytes | memoryview) -> int:
        nonlocal wrote_once
        if not wrote_once:
            wrote_once = True
            return original_write(descriptor, content[: max(1, len(content) // 2)])
        raise OSError("disk full")

    monkeypatch.setattr(files.os, "write", partial_then_fail)
    with pytest.raises(OSError):
        commit_state_change(repo, current, next_state, "test_event", {"test": True})

    assert repo.paths.events.read_bytes() == b""
    assert repo.read_events() == []
    assert repo.load_state().last_event_id is not None


def test_check_inputs_holds_the_lease_mutex_through_its_state_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = initialize(tmp_path)
    clock = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(target, now_fn=lambda: clock[0])
    old_lock = manager.acquire(
        LockOwner(tool="pytest", session_id="old", pid=1, host="local"), 0, timedelta(seconds=1)
    )
    monkeypatch.setattr(initialize_service, "LockManager", lambda _root: manager)
    entered_save = Event()
    allow_save = Event()
    original_save = ProjectRepository.save_state

    def pause_before_commit(self: ProjectRepository, *args: object, **kwargs: object):
        entered_save.set()
        assert allow_save.wait(timeout=5)
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(ProjectRepository, "save_state", pause_before_commit)
    outcome: list[object] = []

    def run_check() -> None:
        try:
            outcome.append(check_inputs(target, old_lock.lock_id, expected_revision=0))
        except Exception as error:  # pragma: no cover - assertion below reports it.
            outcome.append(error)

    worker = Thread(target=run_check)
    worker.start()
    assert entered_save.wait(timeout=5)
    clock[0] += timedelta(seconds=2)

    with pytest.raises(FactoryError) as caught:
        manager.takeover(
            old_lock.lock_id,
            LockOwner(tool="pytest", session_id="new", pid=2, host="local"),
            0,
            "prior worker is expired",
            timedelta(minutes=5),
        )
    allow_save.set()
    worker.join(timeout=5)

    assert caught.value.code == "lock_busy"
    assert len(outcome) == 1
    assert not isinstance(outcome[0], Exception)
    assert ProjectRepository(target).load_state().workflow_state is WorkflowState.INPUTS_CHECKED
