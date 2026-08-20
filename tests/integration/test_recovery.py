"""Read-only project recovery and the deliberately narrow audit repair."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from product_factory.contracts.models import LockOwner
from product_factory.errors import FactoryError
from product_factory.services.initialize import initialize_project
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


def _intake(path: Path) -> None:
    keys = (
        "target_user_and_core_task", "input_process_output", "user_flow_and_confirmations",
        "scope_and_priority", "acceptance_criteria", "model_cost_platform",
        "data_privacy_performance_deployment",
    )
    path.write_text(
        "schema_version: '1.0'\nproject_id: demo-web\nprd_confirmed: true\n"
        "confirmed_by: owner\nconfirmed_at: '2026-08-20T00:00:00Z'\nrequirements:\n"
        + "".join(f"  {key}:\n    status: present\n    source: PRD\n" for key in keys),
        encoding="utf-8",
    )


def _root(tmp_path: Path) -> Path:
    prd, intake, root = tmp_path / "prd.md", tmp_path / "intake.yaml", tmp_path / "product"
    prd.write_text("# PRD\n", encoding="utf-8")
    _intake(intake)
    initialize_project(root, "demo-web", "Demo", prd, intake, [("stage-01", "Core", False)], Path.cwd())
    return root


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        item.relative_to(root).as_posix(): (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_mtime_ns, item.stat().st_size)
        for item in sorted(root.rglob("*")) if item.is_file()
    }


def _lock(root: Path, revision: int = 0) -> str:
    return LockManager(root).acquire(
        LockOwner(tool="pytest", session_id="recovery", pid=1, host="local"),
        revision, timedelta(minutes=5),
    ).lock_id


def test_validate_and_resume_are_read_only_for_a_valid_project(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project, validate_project

    root = _root(tmp_path)
    before = _snapshot(root)
    report = validate_project(root)
    summary = resume_project(root)
    assert report.valid is True
    assert summary.workflow_state == "initialized"
    assert summary.revision == 0
    assert summary.audit_status == "complete"
    assert _snapshot(root) == before


def test_validation_collects_each_bad_record_without_stopping(tmp_path: Path) -> None:
    from product_factory.services.recovery import validate_project

    root = _root(tmp_path)
    metadata = root / ".product-factory"
    metadata.joinpath("intake.yaml").write_text("[bad", encoding="utf-8")
    metadata.joinpath("approvals.jsonl").write_text("{not-json}\n{}\n", encoding="utf-8")
    metadata.joinpath("events.jsonl").write_text("{not-json}\n{}\n", encoding="utf-8")
    report = validate_project(root)
    assert report.valid is False
    assert report.findings == [
        "intake_invalid",
        "approval_invalid:line:1",
        "approval_invalid:line:2",
        "event_invalid:line:1",
        "event_invalid:line:2",
    ]


def test_missing_project_root_raises_stable_factory_error(tmp_path: Path) -> None:
    from product_factory.services.recovery import validate_project

    with pytest.raises(FactoryError) as caught:
        validate_project(tmp_path / "missing")
    assert caught.value.code == "project_unreadable"


def test_resume_reports_missing_audit_event_without_repairing(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    current = repo.load_state()
    # Mimic the state-first crash point without appending the reserved event.
    repo.save_state(current.model_copy(update={"revision": 1, "last_event_id": "lost-event"}), 0)
    before = _snapshot(root)
    summary = resume_project(root)
    assert summary.audit_status == "missing_referenced_event"
    assert summary.next_command == "product-factory repair-audit"
    assert _snapshot(root) == before


def test_resume_with_active_lock_recommends_only_status(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project

    root = _root(tmp_path)
    _lock(root)
    assert resume_project(root).next_command == "product-factory status"


def test_resume_never_recommends_repair_when_the_audit_log_is_damaged(tmp_path: Path) -> None:
    from product_factory.services.recovery import resume_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={"revision": 1, "last_event_id": "lost-event"}), 0)
    repo.paths.events.write_text("{partial", encoding="utf-8")
    summary = resume_project(root)
    assert summary.audit_status == "invalid"
    assert summary.next_command == "product-factory validate"


def test_repair_appends_only_the_referenced_event_and_retries_are_singleton(tmp_path: Path) -> None:
    from product_factory.services.recovery import repair_audit, validate_project

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={"revision": 1, "last_event_id": "lost-event"}), 0)
    before_state = repo.paths.state.read_bytes()
    lock_id = _lock(root, 1)
    event = repair_audit(root, lock_id, 1)
    assert event.event_id == "lost-event"
    assert repo.paths.state.read_bytes() == before_state
    assert [record.event_id for record in repo.read_events()] == ["lost-event"]
    assert validate_project(root).valid is True
    with pytest.raises(FactoryError) as caught:
        repair_audit(root, lock_id, 1)
    assert caught.value.code == "audit_repair_not_needed"


def test_repair_requires_current_lock_and_never_appends_after_malformed_tail(tmp_path: Path) -> None:
    from product_factory.services.recovery import repair_audit

    root = _root(tmp_path)
    repo = ProjectRepository(root)
    state = repo.load_state()
    repo.save_state(state.model_copy(update={"revision": 1, "last_event_id": "lost-event"}), 0)
    with pytest.raises(FactoryError) as caught:
        repair_audit(root, "missing-lock", 1)
    assert caught.value.code == "lock_required"
    repo.paths.events.write_text("{partial", encoding="utf-8")
    before = repo.paths.events.read_bytes()
    lock_id = _lock(root, 1)
    with pytest.raises(FactoryError) as caught:
        repair_audit(root, lock_id, 1)
    assert caught.value.code == "audit_repair_not_safe"
    assert repo.paths.events.read_bytes() == before
