import json
import subprocess
import sys
from pathlib import Path

import yaml

from product_factory.domain.approvals import APPROVAL_STATEMENT
from product_factory.storage.repository import ProjectRepository


def run_cli(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "product_factory.cli.main", *args],
        text=True,
        input=input,
        capture_output=True,
        check=False,
    )


def test_status_missing_project_returns_stable_json(tmp_path) -> None:
    result = run_cli("--json", "status", "--project", str(tmp_path))
    payload = json.loads(result.stdout)
    assert result.returncode == 2
    assert list(payload) == [
        "ok", "code", "category", "message", "step", "retryable", "action", "details"
    ]
    assert payload["code"] == "project_not_initialized"
    assert "TEST_SECRET_DO_NOT_PRINT" not in result.stdout + result.stderr


def test_human_failure_is_only_written_to_stderr(tmp_path: Path) -> None:
    result = run_cli("status", "--project", str(tmp_path))
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("未完成：")
    assert "TEST_SECRET_DO_NOT_PRINT" not in result.stderr


def _intake(path: Path) -> None:
    keys = (
        "target_user_and_core_task", "input_process_output", "user_flow_and_confirmations",
        "scope_and_priority", "acceptance_criteria", "model_cost_platform",
        "data_privacy_performance_deployment",
    )
    path.write_text(yaml.safe_dump({
        "schema_version": "1.0", "project_id": "demo-web", "prd_confirmed": True,
        "confirmed_by": "owner", "confirmed_at": "2026-08-20T00:00:00Z",
        "requirements": {key: {"status": "present", "source": "PRD"} for key in keys},
    }, allow_unicode=True), encoding="utf-8")


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    assert result.stderr in {"", "请输入批准语句："}
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "code", "category", "message", "step", "retryable", "action", "details"}
    assert payload["ok"] is True
    assert "TEST_SECRET_DO_NOT_PRINT" not in result.stdout
    return payload


def _run_json(*args: str, input: str | None = None) -> dict:
    return _json(run_cli("--json", *args, input=input))


def _acquire(root: Path, revision: int, session: str) -> str:
    result = _run_json(
        "lock", "acquire", "--project", str(root), "--tool", "pytest-cli",
        "--session-id", session, "--lease-seconds", "120",
    )
    assert result["details"]["lock"]["state_revision"] == revision
    return result["details"]["lock"]["lock_id"]


def _release(root: Path, lock_id: str) -> None:
    _run_json("lock", "release", "--project", str(root), "--lock-id", lock_id)


def test_cli_complete_workflow_and_read_only_commands(tmp_path: Path) -> None:
    prd, intake, root = tmp_path / "prd.md", tmp_path / "intake.yaml", tmp_path / "product"
    prd.write_text("# CLI PRD\n", encoding="utf-8")
    _intake(intake)
    initialized = _run_json(
        "init", "--project", str(root), "--project-id", "demo-web", "--name", "Demo",
        "--prd", str(prd), "--intake", str(intake), "--stage", "stage-01:Core",
    )
    assert initialized["details"]["state"]["revision"] == 0
    lock = _acquire(root, 0, "initial")
    assert _run_json("lock", "status", "--project", str(root))["details"]["lock"]["lock_id"] == lock
    _json(run_cli("--json", "lock", "heartbeat", "--project", str(root), "--lock-id", lock, "--lease-seconds", "120"))
    _run_json("check-inputs", "--project", str(root), "--lock-id", lock, "--expected-revision", "0")
    _release(root, lock)

    adaptation = root / "docs/technical-adaptation.md"
    adaptation.write_text("offline path\n", encoding="utf-8")
    lock = _acquire(root, 1, "adaptation")
    _run_json(
        "request-approval", "--project", str(root), "--gate", "technical_adaptation",
        "--artifact", "docs/technical-adaptation.md", "--lock-id", lock, "--expected-revision", "1",
    )
    _release(root, lock)
    lock = _acquire(root, 2, "approve-adaptation")
    approved = _run_json(
        "approve", "--project", str(root), "--actor", "owner", "--lock-id", lock,
        "--expected-revision", "2", input=APPROVAL_STATEMENT + "\n",
    )
    assert approved["details"]["state"]["workflow_state"] == "stage_development"
    _release(root, lock)
    lock = _acquire(root, 3, "verification")
    _run_json(
        "transition", "--project", str(root), "--to", "system_verification", "--lock-id", lock,
        "--expected-revision", "3",
    )
    _release(root, lock)

    manifest = root / "evidence-authoring.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0", "evidence_id": "evidence-01", "stage_id": "forged",
        "state_revision": 999, "factory_version": "forged", "prd_sha256": "a" * 64,
        "source_digest": "b" * 64, "checks": [{
            "name": "pytest", "command": "pytest -q", "started_at": "2026-08-20T00:00:00Z",
            "ended_at": "2026-08-20T00:00:01Z", "exit_status": 0, "summary": "ok", "mode": "mock",
        }], "known_issues": [], "ready_for_human_acceptance": True,
    }), encoding="utf-8")
    lock = _acquire(root, 4, "evidence")
    _run_json(
        "record-evidence", "--project", str(root), "--manifest", str(manifest), "--lock-id", lock,
        "--expected-revision", "4",
    )
    _run_json(
        "verify-stage", "--project", str(root), "--evidence-id", "evidence-01", "--lock-id", lock,
        "--expected-revision", "4",
    )
    _release(root, lock)
    lock = _acquire(root, 5, "stage-acceptance")
    _run_json(
        "request-approval", "--project", str(root), "--gate", "stage_acceptance", "--lock-id", lock,
        "--expected-revision", "5",
    )
    _release(root, lock)
    lock = _acquire(root, 6, "approve-stage")
    _run_json(
        "approve", "--project", str(root), "--actor", "owner", "--lock-id", lock,
        "--expected-revision", "6", input=APPROVAL_STATEMENT + "\n",
    )
    _release(root, lock)

    status = _run_json("status", "--project", str(root))
    assert status["details"]["state"]["revision"] == 7
    assert _run_json("validate", "--project", str(root))["details"]["valid"] is True
    assert _run_json("resume", "--project", str(root))["details"]["summary"]["revision"] == 7


def test_cli_repair_audit_lock_takeover_and_rejects_invalid_stage_without_secret(tmp_path: Path) -> None:
    bad = run_cli("--json", "init", "--project", str(tmp_path / "new"), "--project-id", "demo-web",
                  "--name", "Demo", "--prd", str(tmp_path / "missing"), "--intake", str(tmp_path / "missing"),
                  "--stage", "stage-01:Core:bad")
    payload = json.loads(bad.stdout)
    assert bad.returncode == 2
    assert payload["code"] == "stage_invalid"
    assert "TEST_SECRET_DO_NOT_PRINT" not in bad.stdout + bad.stderr

    prd, intake, root = tmp_path / "prd.md", tmp_path / "intake.yaml", tmp_path / "product"
    prd.write_text("# PRD\n", encoding="utf-8")
    _intake(intake)
    _run_json(
        "init", "--project", str(root), "--project-id", "demo-web", "--name", "Demo",
        "--prd", str(prd), "--intake", str(intake), "--stage", "stage-01:Core",
    )
    repo = ProjectRepository(root)
    initial = repo.load_state()
    repo.save_state(initial.model_copy(update={"revision": 1, "last_event_id": "lost-event"}), 0)
    lock = _acquire(root, 1, "repair")
    repaired = _run_json(
        "repair-audit", "--project", str(root), "--lock-id", lock, "--expected-revision", "1",
    )
    assert repaired["details"]["event"]["event_id"] == "lost-event"
    _release(root, lock)

    lock = _acquire(root, 1, "stale")
    lock_path = root / ".product-factory/execution-lock.json"
    stale = json.loads(lock_path.read_text(encoding="utf-8"))
    stale["lease_expires_at"] = "2000-01-01T00:00:00Z"
    lock_path.write_text(json.dumps(stale), encoding="utf-8")
    takeover = _run_json(
        "lock", "takeover", "--project", str(root), "--old-lock-id", lock, "--tool", "pytest-cli",
        "--session-id", "replacement", "--reason", "test recovery", "--lease-seconds", "120",
    )
    assert takeover["details"]["takeover"]["old_lock_id"] == lock
    _release(root, takeover["details"]["lock"]["lock_id"])
