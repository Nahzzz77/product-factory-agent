"""End-to-end evidence recording and verification rules."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from product_factory.contracts.models import (
    CompletionLevel,
    GateType,
    LockOwner,
    WorkflowState,
)
from product_factory.domain.approvals import APPROVAL_STATEMENT
from product_factory.errors import FactoryError
from product_factory.services.evidence import (
    compute_source_digest,
    evidence_current,
    record_evidence,
    verify_stage,
)
from product_factory.services.initialize import check_inputs, initialize_project
from product_factory.services.workflow import WorkflowService
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


def _intake(path: Path) -> None:
    keys = (
        "target_user_and_core_task",
        "input_process_output",
        "user_flow_and_confirmations",
        "scope_and_priority",
        "acceptance_criteria",
        "model_cost_platform",
        "data_privacy_performance_deployment",
    )
    path.write_text(
        "schema_version: '1.0'\nproject_id: demo-web\nprd_confirmed: true\n"
        "confirmed_by: owner\nconfirmed_at: '2026-08-20T00:00:00Z'\nrequirements:\n"
        + "".join(f"  {key}:\n    status: present\n    source: PRD\n" for key in keys),
        encoding="utf-8",
    )


def _lock(root: Path, revision: int):
    return LockManager(root).acquire(
        LockOwner(tool="pytest", session_id=f"evidence-{revision}", pid=1, host="local"),
        state_revision=revision,
        lease=timedelta(minutes=5),
    )


def _root(tmp_path: Path, *, requires_real_model: bool = False) -> Path:
    prd = tmp_path / "prd.md"
    intake = tmp_path / "intake.yaml"
    prd.write_text("# PRD\n", encoding="utf-8")
    _intake(intake)
    root = tmp_path / "product"
    initialize_project(
        root,
        "demo-web",
        "Demo",
        prd,
        intake,
        [("stage-01", "Core", requires_real_model)],
        Path.cwd(),
    )
    (root / "backend/app.py").write_text("value = 1\n", encoding="utf-8")
    lock = _lock(root, 0)
    check_inputs(root, lock.lock_id, 0)
    LockManager(root).release(lock.lock_id)
    (root / "docs/technical-adaptation.md").write_text("offline path\n", encoding="utf-8")
    service = WorkflowService(root)
    lock = _lock(root, 1)
    service.request_approval(GateType.TECHNICAL_ADAPTATION, Path("docs/technical-adaptation.md"), lock.lock_id, 1)
    LockManager(root).release(lock.lock_id)
    lock = _lock(root, 2)
    service.approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 2)
    LockManager(root).release(lock.lock_id)
    lock = _lock(root, 3)
    state = service.start_verification(lock.lock_id, 3)
    LockManager(root).release(lock.lock_id)
    assert state.workflow_state is WorkflowState.SYSTEM_VERIFICATION
    assert state.current_stage.completion_level is CompletionLevel.IMPLEMENTED
    return root


def _authoring(
    root: Path,
    *,
    evidence_id: str = "evidence-01",
    exit_status: int = 0,
    mode: str = "mock",
    blocking: bool = False,
    ready: bool = True,
) -> Path:
    path = root / "evidence-authoring.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "evidence_id": evidence_id,
                "stage_id": "forged-stage",
                "state_revision": 999,
                "factory_version": "forged",
                "prd_sha256": "a" * 64,
                "source_digest": "b" * 64,
                "checks": [
                    {
                        "name": "pytest",
                        "command": "pytest -q",
                        "started_at": "2026-08-20T00:00:00Z",
                        "ended_at": "2026-08-20T00:00:01Z",
                        "exit_status": exit_status,
                        "summary": "done",
                        "mode": mode,
                    }
                ],
                "known_issues": [
                    {"summary": "blocking", "severity": "high", "blocking": blocking}
                ] if blocking else [],
                "ready_for_human_acceptance": ready,
            }
        ),
        encoding="utf-8",
    )
    return path


def _record(root: Path, authoring: Path):
    lock = _lock(root, 4)
    try:
        return record_evidence(root, authoring, lock.lock_id, 4)
    finally:
        LockManager(root).release(lock.lock_id)


def test_digest_ignores_factory_state_but_detects_source_change(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/app.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".product-factory").mkdir()
    (tmp_path / ".product-factory/state.json").write_text("{}", encoding="utf-8")

    first = compute_source_digest(tmp_path, [])
    (tmp_path / ".product-factory/state.json").write_text('{"revision": 2}', encoding="utf-8")
    assert compute_source_digest(tmp_path, []) == first
    (tmp_path / "backend/app.py").write_text("value = 2\n", encoding="utf-8")
    assert compute_source_digest(tmp_path, []) != first


def test_digest_excludes_secrets_but_includes_env_example_and_configured_globs(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret=one\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("secret=two\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("secret=sample\n", encoding="utf-8")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated/build.txt").write_text("ignored\n", encoding="utf-8")
    first = compute_source_digest(tmp_path, ["generated/**"])
    (tmp_path / ".env").write_text("secret=changed\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("secret=changed\n", encoding="utf-8")
    (tmp_path / "generated/build.txt").write_text("changed\n", encoding="utf-8")
    assert compute_source_digest(tmp_path, ["generated/**"]) == first
    (tmp_path / ".env.example").write_text("secret=changed sample\n", encoding="utf-8")
    assert compute_source_digest(tmp_path, ["generated/**"]) != first


def test_record_evidence_recomputes_identity_fields_and_never_reuses_an_id(tmp_path: Path) -> None:
    root = _root(tmp_path)
    manifest = _record(root, _authoring(root))
    repo = ProjectRepository(root)
    assert manifest.stage_id == "stage-01"
    assert manifest.state_revision == 4
    assert manifest.factory_version == repo.load_project().factory_version
    assert manifest.prd_sha256 == repo.load_project().prd.sha256
    assert manifest.source_digest == compute_source_digest(root, repo.load_project().source_excludes)
    assert repo.evidence_path("stage-01", "evidence-01") == (
        root / ".product-factory/evidence/stage-01/evidence-01/manifest.json"
    )
    lock = _lock(root, 4)
    with pytest.raises(FactoryError) as caught:
        record_evidence(root, Path("evidence-authoring.json"), lock.lock_id, 4)
    assert caught.value.code == "evidence_exists"
    LockManager(root).release(lock.lock_id)


@pytest.mark.parametrize(
    ("requires_real_model", "exit_status", "mode", "blocking", "ready", "mutate", "reason"),
    [
        (False, 0, "mock", False, True, lambda root: (root / "backend/app.py").write_text("changed\n"), "source_changed"),
        (False, 1, "mock", False, True, lambda _root: None, "check_failed"),
        (True, 0, "mock", False, True, lambda _root: None, "real_model_missing"),
        (False, 0, "mock", True, True, lambda _root: None, "blocking_issue"),
        (False, 0, "mock", False, False, lambda _root: None, "not_ready"),
    ],
)
def test_verify_stage_reports_each_invalid_evidence_reason(
    tmp_path: Path,
    requires_real_model: bool,
    exit_status: int,
    mode: str,
    blocking: bool,
    ready: bool,
    mutate: object,
    reason: str,
) -> None:
    root = _root(tmp_path, requires_real_model=requires_real_model)
    _record(root, _authoring(root, exit_status=exit_status, mode=mode, blocking=blocking, ready=ready))
    mutate(root)  # type: ignore[operator]
    lock = _lock(root, 4)
    with pytest.raises(FactoryError) as caught:
        verify_stage(root, "evidence-01", lock.lock_id, 4)
    assert caught.value.code == "evidence_invalid"
    assert caught.value.details["reasons"] == [reason]
    LockManager(root).release(lock.lock_id)


def test_verify_stage_marks_valid_non_model_evidence_and_current_validator_rechecks_it(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _record(root, _authoring(root))
    repo = ProjectRepository(root)
    before = repo.load_state()
    assert evidence_current(repo, repo.load_project(), before, "evidence-01") is True
    lock = _lock(root, 4)
    verified = verify_stage(root, "evidence-01", lock.lock_id, 4)
    LockManager(root).release(lock.lock_id)
    assert verified.current_stage.completion_level is CompletionLevel.SYSTEM_VERIFIED
    assert verified.last_valid_evidence_id == "evidence-01"
    assert verified.revision == 5
    (root / "backend/app.py").write_text("changed\n", encoding="utf-8")
    assert evidence_current(repo, repo.load_project(), verified, "evidence-01") is False


def test_stage_acceptance_uses_current_evidence_before_request_and_consume(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _record(root, _authoring(root))
    lock = _lock(root, 4)
    verify_stage(root, "evidence-01", lock.lock_id, 4)
    LockManager(root).release(lock.lock_id)

    lock = _lock(root, 5)
    pending = WorkflowService(root).request_approval(GateType.STAGE_ACCEPTANCE, None, lock.lock_id, 5)
    LockManager(root).release(lock.lock_id)
    assert pending.workflow_state is WorkflowState.HUMAN_ACCEPTANCE_PENDING
    (root / "backend/app.py").write_text("changed\n", encoding="utf-8")
    lock = _lock(root, 6)
    with pytest.raises(FactoryError) as caught:
        WorkflowService(root).approve(APPROVAL_STATEMENT, "owner", lock.lock_id, 6)
    assert caught.value.code == "evidence_invalid"
    LockManager(root).release(lock.lock_id)
