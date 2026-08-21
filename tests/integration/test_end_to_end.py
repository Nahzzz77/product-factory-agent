"""Golden offline workflow through the installed public CLI only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY_ROOT / "examples" / "minimal-project"
PUBLIC_APPROVAL_STATEMENT = "验收通过，批准进入下一阶段。"
_SOURCE_INJECTION_ENVIRONMENT = {
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "__PYVENV_LAUNCHER__",
}


def _installed_cli() -> Path:
    venv = (REPOSITORY_ROOT / ".venv").resolve()
    executable = (venv / "bin" / "product-factory").resolve()
    assert executable.is_file(), "install the editable package before the end-to-end test"
    assert executable.is_relative_to(venv), "end-to-end entry point must come from the repository venv"
    return executable


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in _SOURCE_INJECTION_ENVIRONMENT:
        environment.pop(variable, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    return environment


def _run(
    cwd: Path,
    *arguments: str,
    input_text: str | None = None,
    expected_returncode: int = 0,
    expected_ok: bool = True,
) -> dict:
    completed = subprocess.run(
        [str(_installed_cli()), "--json", *arguments],
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
        cwd=cwd,
        env=_subprocess_environment(),
    )
    assert completed.returncode == expected_returncode, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is expected_ok
    return payload


def _acquire(cwd: Path, project: Path, session_id: str, revision: int) -> str:
    payload = _run(
        cwd,
        "lock", "acquire", "--project", str(project), "--tool", "pytest-e2e",
        "--session-id", session_id, "--lease-seconds", "120",
    )
    lock = payload["details"]["lock"]
    assert lock["state_revision"] == revision
    return lock["lock_id"]


def _release(cwd: Path, project: Path, lock_id: str) -> None:
    _run(cwd, "lock", "release", "--project", str(project), "--lock-id", lock_id)


def _assert_help_works_outside_repository(cwd: Path) -> None:
    completed = subprocess.run(
        [str(_installed_cli()), "--help"],
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        env=_subprocess_environment(),
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.startswith("usage: product-factory")


def test_minimal_project_completes_the_offline_protocol_via_installed_cli(tmp_path: Path) -> None:
    """Exercise the documented release/reacquire workflow without service calls."""
    external_cwd = tmp_path / "cli-cwd"
    external_cwd.mkdir()
    _assert_help_works_outside_repository(external_cwd)

    project = tmp_path / "minimal-web"
    _run(
        external_cwd,
        "init", "--project", str(project), "--project-id", "minimal-web", "--name", "离线任务清单",
        "--prd", str(EXAMPLE / "PRD.md"), "--intake", str(EXAMPLE / "intake.yaml"),
        "--stage", "stage-01:离线核心",
    )

    # These are authoring inputs.  They deliberately exist before source-digest
    # calculation, so recording evidence does not make its own digest stale.
    shutil.copyfile(EXAMPLE / "technical-adaptation.md", project / "docs" / "technical-adaptation.md")
    shutil.copyfile(EXAMPLE / "evidence-manifest.yaml", project / "evidence-authoring.yaml")

    lock = _acquire(external_cwd, project, "check-inputs", 0)
    _run(external_cwd, "check-inputs", "--project", str(project), "--lock-id", lock, "--expected-revision", "0")
    _release(external_cwd, project, lock)

    lock = _acquire(external_cwd, project, "request-adaptation", 1)
    _run(
        external_cwd,
        "request-approval", "--project", str(project), "--gate", "technical_adaptation",
        "--artifact", "docs/technical-adaptation.md", "--lock-id", lock, "--expected-revision", "1",
    )
    _release(external_cwd, project, lock)

    lock = _acquire(external_cwd, project, "approve-adaptation", 2)
    _run(
        external_cwd,
        "approve", "--project", str(project), "--actor", "product-owner", "--lock-id", lock,
        "--expected-revision", "2", input_text=PUBLIC_APPROVAL_STATEMENT + "\n",
    )
    _release(external_cwd, project, lock)

    lock = _acquire(external_cwd, project, "system-verification", 3)
    _run(
        external_cwd,
        "transition", "--project", str(project), "--to", "system_verification", "--lock-id", lock,
        "--expected-revision", "3",
    )
    _release(external_cwd, project, lock)

    lock = _acquire(external_cwd, project, "record-evidence", 4)
    recorded = _run(
        external_cwd,
        "record-evidence", "--project", str(project), "--manifest", "evidence-authoring.yaml",
        "--lock-id", lock, "--expected-revision", "4",
    )
    assert recorded["details"]["evidence"]["evidence_id"] == "evidence-01"
    immutable = project / ".product-factory" / "evidence" / "stage-01" / "evidence-01" / "manifest.json"
    original_manifest = immutable.read_bytes()
    duplicate = _run(
        external_cwd,
        "record-evidence", "--project", str(project), "--manifest", "evidence-authoring.yaml",
        "--lock-id", lock, "--expected-revision", "4",
        expected_returncode=6,
        expected_ok=False,
    )
    assert duplicate["code"] == "evidence_exists"
    assert immutable.read_bytes() == original_manifest
    _run(
        external_cwd,
        "verify-stage", "--project", str(project), "--evidence-id", "evidence-01",
        "--lock-id", lock, "--expected-revision", "4",
    )
    _release(external_cwd, project, lock)

    lock = _acquire(external_cwd, project, "request-stage-acceptance", 5)
    _run(
        external_cwd,
        "request-approval", "--project", str(project), "--gate", "stage_acceptance",
        "--lock-id", lock, "--expected-revision", "5",
    )
    _release(external_cwd, project, lock)

    lock = _acquire(external_cwd, project, "approve-stage-acceptance", 6)
    _run(
        external_cwd,
        "approve", "--project", str(project), "--actor", "product-owner", "--lock-id", lock,
        "--expected-revision", "6", input_text=PUBLIC_APPROVAL_STATEMENT + "\n",
    )
    _release(external_cwd, project, lock)

    state = json.loads((project / ".product-factory" / "state.json").read_text(encoding="utf-8"))
    assert state["workflow_state"] == "next_stage_or_frontend"
    assert state["current_stage"]["completion_level"] == "human_accepted"

    approvals = [
        json.loads(line)
        for line in (project / ".product-factory" / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(approvals) == 2
    assert [item["gate_type"] for item in approvals] == ["technical_adaptation", "stage_acceptance"]
    assert [item["statement"] for item in approvals] == [
        PUBLIC_APPROVAL_STATEMENT,
        PUBLIC_APPROVAL_STATEMENT,
    ]

    assert immutable.is_file()
    assert json.loads(immutable.read_text(encoding="utf-8"))["stage_id"] == "stage-01"
    events = [
        json.loads(line)
        for line in (project / ".product-factory" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events
    assert all(event["project_id"] == "minimal-web" for event in events)

    resume = _run(external_cwd, "resume", "--project", str(project))
    assert resume["details"]["summary"]["next_command"] == "等待下一里程碑设计"
