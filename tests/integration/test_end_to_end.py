"""Golden offline workflow through the installed public CLI only."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from product_factory.domain.approvals import APPROVAL_STATEMENT


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY_ROOT / "examples" / "minimal-project"


def _installed_cli() -> Path:
    executable = Path(sys.executable).parent / "product-factory"
    assert executable.is_file(), "install the editable package before the end-to-end test"
    return executable


def _run(*arguments: str, input_text: str | None = None) -> dict:
    completed = subprocess.run(
        [str(_installed_cli()), "--json", *arguments],
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    return payload


def _acquire(project: Path, session_id: str, revision: int) -> str:
    payload = _run(
        "lock", "acquire", "--project", str(project), "--tool", "pytest-e2e",
        "--session-id", session_id, "--lease-seconds", "120",
    )
    lock = payload["details"]["lock"]
    assert lock["state_revision"] == revision
    return lock["lock_id"]


def _release(project: Path, lock_id: str) -> None:
    _run("lock", "release", "--project", str(project), "--lock-id", lock_id)


def test_minimal_project_completes_the_offline_protocol_via_installed_cli(tmp_path: Path) -> None:
    """Exercise the documented release/reacquire workflow without service calls."""
    project = tmp_path / "minimal-web"
    _run(
        "init", "--project", str(project), "--project-id", "minimal-web", "--name", "离线任务清单",
        "--prd", str(EXAMPLE / "PRD.md"), "--intake", str(EXAMPLE / "intake.yaml"),
        "--stage", "stage-01:离线核心",
    )

    # These are authoring inputs.  They deliberately exist before source-digest
    # calculation, so recording evidence does not make its own digest stale.
    shutil.copyfile(EXAMPLE / "technical-adaptation.md", project / "docs" / "technical-adaptation.md")
    shutil.copyfile(EXAMPLE / "evidence-manifest.yaml", project / "evidence-authoring.yaml")

    lock = _acquire(project, "check-inputs", 0)
    _run("check-inputs", "--project", str(project), "--lock-id", lock, "--expected-revision", "0")
    _release(project, lock)

    lock = _acquire(project, "request-adaptation", 1)
    _run(
        "request-approval", "--project", str(project), "--gate", "technical_adaptation",
        "--artifact", "docs/technical-adaptation.md", "--lock-id", lock, "--expected-revision", "1",
    )
    _release(project, lock)

    lock = _acquire(project, "approve-adaptation", 2)
    _run(
        "approve", "--project", str(project), "--actor", "product-owner", "--lock-id", lock,
        "--expected-revision", "2", input_text=APPROVAL_STATEMENT + "\n",
    )
    _release(project, lock)

    lock = _acquire(project, "system-verification", 3)
    _run(
        "transition", "--project", str(project), "--to", "system_verification", "--lock-id", lock,
        "--expected-revision", "3",
    )
    _release(project, lock)

    lock = _acquire(project, "record-evidence", 4)
    recorded = _run(
        "record-evidence", "--project", str(project), "--manifest", "evidence-authoring.yaml",
        "--lock-id", lock, "--expected-revision", "4",
    )
    assert recorded["details"]["evidence"]["evidence_id"] == "evidence-01"
    _run(
        "verify-stage", "--project", str(project), "--evidence-id", "evidence-01",
        "--lock-id", lock, "--expected-revision", "4",
    )
    _release(project, lock)

    lock = _acquire(project, "request-stage-acceptance", 5)
    _run(
        "request-approval", "--project", str(project), "--gate", "stage_acceptance",
        "--lock-id", lock, "--expected-revision", "5",
    )
    _release(project, lock)

    lock = _acquire(project, "approve-stage-acceptance", 6)
    _run(
        "approve", "--project", str(project), "--actor", "product-owner", "--lock-id", lock,
        "--expected-revision", "6", input_text=APPROVAL_STATEMENT + "\n",
    )
    _release(project, lock)

    state = json.loads((project / ".product-factory" / "state.json").read_text(encoding="utf-8"))
    assert state["workflow_state"] == "next_stage_or_frontend"
    assert state["current_stage"]["completion_level"] == "human_accepted"

    approvals = [
        json.loads(line)
        for line in (project / ".product-factory" / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(approvals) == 2
    assert {item["gate_type"] for item in approvals} == {"technical_adaptation", "stage_acceptance"}

    immutable = project / ".product-factory" / "evidence" / "stage-01" / "evidence-01" / "manifest.json"
    assert immutable.is_file()
    assert json.loads(immutable.read_text(encoding="utf-8"))["stage_id"] == "stage-01"
    events = [
        json.loads(line)
        for line in (project / ".product-factory" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events
    assert all(event["project_id"] == "minimal-web" for event in events)

    resume = _run("resume", "--project", str(project))
    assert resume["details"]["summary"]["next_command"] == "等待下一里程碑设计"
