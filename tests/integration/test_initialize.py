from datetime import datetime, timedelta, timezone
import hashlib
import os
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
from product_factory.storage import files
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


def test_initialize_rejects_a_non_empty_target_without_reading_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "new-product"
    target.mkdir()
    (target / "existing.txt").write_text("do not overwrite", encoding="utf-8")
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "intake.yaml"
    write_intake(intake)

    def sources_must_not_be_read(self: Path, *args: object, **kwargs: object) -> bytes:
        if self in {prd, intake}:
            raise AssertionError("non-empty target must win before source reads")
        return original_read_bytes(self, *args, **kwargs)

    original_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", sources_must_not_be_read)

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            target, "demo-web", "Demo Web", prd, intake, [("stage-01", "Core", False)], Path.cwd()
        )

    assert caught.value.code == "project_exists"
    assert (target / "existing.txt").read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.parametrize("preexisting_empty_target", [False, True])
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("missing_prd", "prd_unreadable"),
        ("unreadable_prd", "prd_unreadable"),
        ("empty_stages", "stage_plan_invalid"),
        ("invalid_stages", "stage_plan_invalid"),
        ("missing_handbook", "handbook_invalid"),
        ("malformed_handbook", "handbook_invalid"),
        ("missing_constraints", "constraints_unreadable"),
    ],
)
def test_initialize_preflight_failures_do_not_mutate_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting_empty_target: bool,
    failure: str,
    expected_code: str,
) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake)
    target = tmp_path / "new-product"
    if preexisting_empty_target:
        target.mkdir()
    factory_root = Path.cwd()
    constraints: Path | None = None
    stages: object = [("stage-01", "Core", False)]

    if failure == "missing_prd":
        prd = tmp_path / "missing-prd.md"
    elif failure == "unreadable_prd":
        original_read_bytes = Path.read_bytes

        def unreadable_prd(self: Path, *args: object, **kwargs: object) -> bytes:
            if self == prd:
                raise OSError("permission denied")
            return original_read_bytes(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", unreadable_prd)
    elif failure == "empty_stages":
        stages = []
    elif failure == "invalid_stages":
        stages = [("bad",)]
    elif failure == "missing_handbook":
        factory_root = tmp_path / "factory-without-handbooks"
    elif failure == "malformed_handbook":
        factory_root = tmp_path / "factory-malformed"
        manifest = factory_root / "references/handbooks/manifest.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("schema_version: '1.0'\ndocuments: not-a-list\n", encoding="utf-8")
    elif failure == "missing_constraints":
        constraints = tmp_path / "missing-constraints.md"

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            target,
            "demo-web",
            "Demo Web",
            prd,
            intake,
            stages,  # type: ignore[arg-type]
            factory_root,
            constraints,
        )

    assert caught.value.code == expected_code
    if preexisting_empty_target:
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()


@pytest.mark.parametrize("preexisting_empty_target", [False, True])
@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/tmp/handbook.md",
        "C:/outside/handbook.md",
        "../outside.md",
        "references/handbooks/../../outside.md",
        "references\\handbooks\\outside.md",
    ],
)
def test_initialize_rejects_unsafe_handbook_paths_before_target_writes(
    tmp_path: Path, preexisting_empty_target: bool, unsafe_path: str
) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake)
    factory_root = tmp_path / "factory"
    manifest = factory_root / "references/handbooks/manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "documents": [
                    {"title": "Unsafe", "version": "1", "path": unsafe_path, "sha256": "0" * 64}
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target = tmp_path / "target"
    if preexisting_empty_target:
        target.mkdir()

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            target, "demo-web", "Demo", prd, intake, [("stage-01", "Core", False)], factory_root
        )

    assert caught.value.code == "handbook_invalid"
    assert list(target.iterdir()) == [] if target.exists() else not target.exists()


@pytest.mark.parametrize("preexisting_empty_target", [False, True])
def test_initialize_rejects_handbook_symlink_escaping_factory_root(
    tmp_path: Path, preexisting_empty_target: bool
) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    factory_root = tmp_path / "factory"
    handbook = factory_root / "references/handbooks/link.md"
    handbook.parent.mkdir(parents=True)
    handbook.symlink_to(outside)
    manifest = handbook.parent / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "documents": [
                    {
                        "title": "Escaped",
                        "version": "1",
                        "path": "references/handbooks/link.md",
                        "sha256": hashlib.sha256(b"outside").hexdigest(),
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target = tmp_path / "target"
    if preexisting_empty_target:
        target.mkdir()

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            target, "demo-web", "Demo", prd, intake, [("stage-01", "Core", False)], factory_root
        )

    assert caught.value.code == "handbook_invalid"
    assert list(target.iterdir()) == [] if target.exists() else not target.exists()


def test_initialize_keeps_a_safe_nested_handbook_path_and_digest(tmp_path: Path) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake)
    factory_root = tmp_path / "factory"
    handbook = factory_root / "references/handbooks/nested/guide.md"
    handbook.parent.mkdir(parents=True)
    handbook_bytes = b"safe handbook\n"
    handbook.write_bytes(handbook_bytes)
    manifest = factory_root / "references/handbooks/manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "documents": [
                    {
                        "title": "Nested",
                        "version": "1",
                        "path": "references/handbooks/nested/guide.md",
                        "sha256": hashlib.sha256(handbook_bytes).hexdigest(),
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    initialize_project(
        tmp_path / "target", "demo-web", "Demo", prd, intake, [("stage-01", "Core", False)], factory_root
    )

    reference = ProjectRepository(tmp_path / "target").load_project().handbooks[0]
    assert reference.path == "references/handbooks/nested/guide.md"
    assert reference.sha256 == hashlib.sha256(handbook_bytes).hexdigest()


@pytest.mark.parametrize("preexisting_empty_target", [False, True])
@pytest.mark.parametrize("replace_after_open", [False, True])
def test_initialize_rejects_handbook_replacement_races_without_hashing_outside_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preexisting_empty_target: bool, replace_after_open: bool
) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake)
    factory_root = tmp_path / "factory"
    handbook = factory_root / "references/handbooks/nested/guide.md"
    handbook.parent.mkdir(parents=True)
    handbook.write_bytes(b"inside handbook\n")
    outside = tmp_path / "outside.md"
    outside_bytes = b"outside handbook must never be hashed\n"
    outside.write_bytes(outside_bytes)
    manifest = factory_root / "references/handbooks/manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "documents": [
                    {
                        "title": "Race",
                        "version": "1",
                        "path": "references/handbooks/nested/guide.md",
                        # A vulnerable reopen-by-path implementation would accept this.
                        "sha256": hashlib.sha256(outside_bytes).hexdigest(),
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target = tmp_path / "target"
    if preexisting_empty_target:
        target.mkdir()

    if replace_after_open:
        original_read = files._read_descriptor
        replaced = False

        def replace_after_descriptor_open(descriptor: int) -> bytes:
            nonlocal replaced
            if not replaced:
                replaced = True
                handbook.unlink()
                handbook.symlink_to(outside)
            return original_read(descriptor)

        monkeypatch.setattr(files, "_read_descriptor", replace_after_descriptor_open)
    else:
        original_open = files.os.open
        replaced = False

        def replace_before_final_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal replaced
            if not replaced and path == "guide.md" and "dir_fd" in kwargs:
                replaced = True
                handbook.unlink()
                handbook.symlink_to(outside)
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(files.os, "open", replace_before_final_open)

    with pytest.raises(FactoryError) as caught:
        initialize_project(
            target, "demo-web", "Demo", prd, intake, [("stage-01", "Core", False)], factory_root
        )

    assert caught.value.code == "handbook_invalid"
    assert list(target.iterdir()) == [] if target.exists() else not target.exists()


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


def test_initialize_copies_the_same_intake_bytes_it_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    intake = tmp_path / "source-intake.yaml"
    write_intake(intake)
    validated_bytes = intake.read_bytes()
    unvalidated_bytes = b"not: a valid intake\n"
    original_read_text = Path.read_text

    def swap_source_after_validation(self: Path, *args: object, **kwargs: object) -> str:
        contents = original_read_text(self, *args, **kwargs)
        if self == intake:
            self.write_bytes(unvalidated_bytes)
        return contents

    monkeypatch.setattr(Path, "read_text", swap_source_after_validation)
    target = tmp_path / "new-product"

    initialize_project(
        target, "demo-web", "Demo Web", prd, intake, [("stage-01", "Core", False)], Path.cwd()
    )

    assert (target / ".product-factory/intake.yaml").read_bytes() == validated_bytes


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


@pytest.mark.skipif(os.name == "nt", reason="mkfifo is a POSIX probe")
def test_check_inputs_rejects_prd_fifo_without_blocking(tmp_path: Path) -> None:
    target = initialize(tmp_path)
    prd = target / "inputs/PRD.md"
    prd.unlink()
    os.mkfifo(prd)

    with pytest.raises(FactoryError) as caught:
        check_inputs(target, lock_for_initial_state(target), expected_revision=0)

    assert caught.value.code == "prd_unreadable"


def test_check_inputs_accepts_reasoned_not_applicable_and_advances_state(tmp_path: Path) -> None:
    target = initialize(tmp_path, not_applicable="model_cost_platform")

    state = check_inputs(target, lock_for_initial_state(target), expected_revision=0)

    assert state.revision == 1
    assert state.workflow_state is WorkflowState.INPUTS_CHECKED
    assert ProjectRepository(target).read_events()[0].event_type == "inputs_checked"


def test_check_inputs_never_reopens_a_later_workflow_state(tmp_path: Path) -> None:
    target = initialize(tmp_path)
    repo = ProjectRepository(target)
    current = repo.load_state()
    revision_one = current.model_copy(update={"revision": 1})
    repo.save_state(revision_one, 0)
    revision_two = revision_one.model_copy(update={"revision": 2})
    repo.save_state(revision_two, 1)
    advanced = revision_two.model_copy(update={
        "revision": 3,
        "workflow_state": WorkflowState.ADAPTATION_PENDING_APPROVAL,
        "waiting_on": None,
    })
    repo.save_state(advanced, 2)
    before_state = repo.paths.state.read_bytes()
    before_events = repo.paths.events.read_bytes()
    lock = LockManager(target).acquire(
        LockOwner(tool="pytest", session_id="later", pid=1, host="local"), 3, timedelta(minutes=5)
    )

    with pytest.raises(FactoryError) as caught:
        check_inputs(target, lock.lock_id, expected_revision=3)

    assert caught.value.code == "transition_not_allowed"
    assert repo.paths.state.read_bytes() == before_state
    assert repo.paths.events.read_bytes() == before_events


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
