from __future__ import annotations

import time
import shutil
from pathlib import Path

import pytest

from product_factory.errors import FactoryError
from product_factory.domain.approvals import APPROVAL_STATEMENT
from product_factory.web.service import AgentRunManager, ConsoleService


FACTORY_ROOT = Path(__file__).resolve().parents[2]
PRD = FACTORY_ROOT / "examples" / "minimal-project" / "PRD.md"


def _create(service: ConsoleService, name: str = "demo") -> dict:
    return service.create_project(
        {
            "directory": name,
            "project_id": name,
            "name": "演示项目",
            "prd_path": str(PRD),
            "confirmed_by": "product-owner",
            "prd_confirmed": True,
            "requirements_confirmed": True,
            "stage_id": "stage-01",
            "stage_name": "Web MVP",
        }
    )


def test_create_list_snapshot_and_check_inputs(tmp_path: Path) -> None:
    service = ConsoleService(tmp_path, factory_root=FACTORY_ROOT)

    created = _create(service)
    assert created["project"]["project_id"] == "demo"
    assert created["state"]["workflow_state"] == "initialized"
    assert created["documents"][0]["id"] == "prd"
    assert created["documents"][0]["exists"] is True
    assert "离线" in created["documents"][0]["content"]
    assert created["stats"] == {"events": 0, "approvals": 0, "evidence": 0}
    assert [item["project_id"] for item in service.list_projects()] == ["demo"]

    checked = service.perform_action(tmp_path / "demo", "check_inputs", {})
    assert checked["state"]["workflow_state"] == "inputs_checked"
    assert checked["state"]["revision"] == 1
    assert checked["lock"] is None
    assert checked["validation"]["valid"] is True
    assert checked["activity"][0]["type"] == "inputs_checked"
    assert not (tmp_path / "demo" / ".product-factory" / "execution-lock.json").exists()


def test_create_requires_explicit_prd_confirmation(tmp_path: Path) -> None:
    service = ConsoleService(tmp_path, factory_root=FACTORY_ROOT)
    payload = {
        "directory": "demo",
        "project_id": "demo",
        "name": "演示项目",
        "prd_path": str(PRD),
        "confirmed_by": "owner",
        "prd_confirmed": False,
        "requirements_confirmed": True,
        "stage_id": "stage-01",
        "stage_name": "Web MVP",
    }

    with pytest.raises(FactoryError, match="prd_confirmation_required"):
        service.create_project(payload)
    assert not (tmp_path / "demo").exists()


@pytest.mark.parametrize("directory", ["../escape", "/tmp/escape", ".", "nested/../../escape"])
def test_project_directory_cannot_escape_workspace(tmp_path: Path, directory: str) -> None:
    service = ConsoleService(tmp_path, factory_root=FACTORY_ROOT)
    payload = {
        "directory": directory,
        "project_id": "demo",
        "name": "演示项目",
        "prd_path": str(PRD),
        "confirmed_by": "owner",
        "prd_confirmed": True,
        "requirements_confirmed": True,
        "stage_id": "stage-01",
        "stage_name": "Web MVP",
    }
    with pytest.raises(FactoryError, match="project_path_invalid"):
        service.create_project(payload)


def test_agent_run_uses_argument_vector_and_captures_output(tmp_path: Path) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text("#!/bin/sh\nprintf 'agent complete\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    manager = AgentRunManager(executable=str(executable))

    run = manager.start(project, "完成当前阶段")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = manager.get(run["run_id"])
        if current["status"] != "running":
            break
        time.sleep(0.01)

    assert current["status"] == "completed"
    assert current["exit_code"] == 0
    assert "agent complete" in current["output"]
    assert current["command"][0] == str(executable)
    assert str(project.resolve()) in current["command"]
    assert current["command"][-1] == "-"


def test_console_actions_cover_the_complete_core_milestone(tmp_path: Path) -> None:
    service = ConsoleService(tmp_path, factory_root=FACTORY_ROOT)
    _create(service)
    root = tmp_path / "demo"

    service.perform_action(root, "check_inputs", {})
    shutil.copy(
        FACTORY_ROOT / "examples/minimal-project/technical-adaptation.md",
        root / "docs/technical-adaptation.md",
    )
    service.perform_action(root, "request_adaptation", {})
    service.perform_action(
        root, "approve", {"actor": "product-owner", "statement": APPROVAL_STATEMENT}
    )
    service.perform_action(root, "start_verification", {})
    shutil.copy(
        FACTORY_ROOT / "examples/minimal-project/evidence-manifest.yaml",
        root / "evidence-authoring.yaml",
    )
    service.perform_action(root, "record_evidence", {"manifest": "evidence-authoring.yaml"})
    service.perform_action(root, "verify_stage", {"evidence_id": "evidence-01"})
    service.perform_action(root, "request_acceptance", {})
    final = service.perform_action(
        root, "approve", {"actor": "product-owner", "statement": APPROVAL_STATEMENT}
    )

    assert final["state"]["workflow_state"] == "next_stage_or_frontend"
    assert final["state"]["current_stage"]["completion_level"] == "human_accepted"
    assert final["state"]["revision"] == 7
    assert final["validation"]["valid"] is True


def test_console_does_not_launch_agent_while_human_action_is_pending(tmp_path: Path) -> None:
    service = ConsoleService(tmp_path, factory_root=FACTORY_ROOT)
    _create(service)
    with pytest.raises(FactoryError, match="agent_stage_blocked"):
        service.start_agent(tmp_path / "demo", "开始开发")
