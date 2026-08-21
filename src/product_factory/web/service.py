"""Application service used by the local browser console."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from product_factory.cli.output import _normalise
from product_factory.contracts.models import (
    GateType,
    IntakeRecord,
    LockOwner,
    REQUIREMENT_KEYS,
    RequirementDeclaration,
    RequirementStatus,
    WorkflowState,
)
from product_factory.domain.approvals import APPROVAL_STATEMENT
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.resources import factory_resource_root
from product_factory.services.evidence import record_evidence, verify_stage
from product_factory.services.initialize import check_inputs, initialize_project
from product_factory.services.recovery import repair_audit, resume_project, validate_project
from product_factory.services.workflow import WorkflowService
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


class AgentRunManager:
    """Launch explicitly requested Codex runs without shell interpolation."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("codex")
        self._runs: dict[str, dict[str, Any]] = {}
        self._mutex = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def start(self, project: Path, objective: str) -> dict[str, Any]:
        objective = objective.strip()
        if not objective:
            raise _web_error(
                "agent_objective_required", "请说明希望 Codex 在当前阶段完成什么", "填写开发任务后重试"
            )
        if not self.executable:
            raise FactoryError(
                "codex_unavailable", ErrorCategory.ENVIRONMENT_BLOCKED, "本机没有找到 Codex 命令",
                "web agent", True, "安装或登录 Codex 后重试",
            )
        project = project.resolve()
        run_id = str(uuid4())
        command = [
            self.executable,
            "-s", "workspace-write",
            "-a", "never",
            "exec",
            "-C", str(project),
            "--skip-git-repo-check",
            "--color", "never",
            "-",
        ]
        prompt = _agent_prompt(objective)
        record: dict[str, Any] = {
            "run_id": run_id,
            "project_path": str(project),
            "objective": objective,
            "status": "running",
            "exit_code": None,
            "output": "",
            "command": command,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
        with self._mutex:
            self._runs[run_id] = record
        thread = threading.Thread(
            target=self._execute, args=(run_id, command, prompt, project), daemon=True,
            name=f"product-factory-agent-{run_id[:8]}",
        )
        thread.start()
        return self.get(run_id)

    def get(self, run_id: str) -> dict[str, Any]:
        with self._mutex:
            record = self._runs.get(run_id)
            if record is None:
                raise _web_error("agent_run_not_found", "找不到这次 Codex 任务", "刷新项目后重试")
            return dict(record)

    def latest_for(self, project: Path) -> dict[str, Any] | None:
        resolved = str(project.resolve())
        with self._mutex:
            candidates = [item for item in self._runs.values() if item["project_path"] == resolved]
            return dict(candidates[-1]) if candidates else None

    def _execute(self, run_id: str, command: list[str], prompt: str, project: Path) -> None:
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=project,
                check=False,
                timeout=None,
            )
            output = completed.stdout[-100_000:]
            status = "completed" if completed.returncode == 0 else "failed"
            exit_code: int | None = completed.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            output = f"Codex 启动失败：{type(exc).__name__}"
            status = "failed"
            exit_code = None
        with self._mutex:
            record = self._runs[run_id]
            record.update(
                status=status,
                exit_code=exit_code,
                output=output,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )


class ConsoleService:
    """Constrained façade over the protocol for a local human-operated UI."""

    def __init__(
        self,
        workspace: Path,
        *,
        factory_root: Path | None = None,
        agent_runs: AgentRunManager | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.factory_root = factory_root.resolve() if factory_root else None
        self.agent_runs = agent_runs or AgentRunManager()

    def config(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "codex_available": self.agent_runs.available,
            "approval_statement": APPROVAL_STATEMENT,
        }

    def list_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for root, directories, files in os.walk(self.workspace, followlinks=False):
            relative_depth = len(Path(root).relative_to(self.workspace).parts)
            is_managed = ".product-factory" in directories
            directories[:] = [
                name for name in directories
                if not name.startswith(".") and relative_depth < 4
            ]
            if relative_depth > 4:
                directories[:] = []
                continue
            if is_managed:
                candidate = Path(root)
                try:
                    snapshot = self.snapshot(candidate)
                except (FactoryError, OSError, ValueError):
                    continue
                projects.append(
                    {
                        "path": str(candidate.resolve()),
                        "project_id": snapshot["project"]["project_id"],
                        "name": snapshot["project"]["name"],
                        "workflow_state": snapshot["state"]["workflow_state"],
                        "revision": snapshot["state"]["revision"],
                        "valid": snapshot["validation"]["valid"],
                    }
                )
                directories[:] = []
        return sorted(projects, key=lambda item: (item["name"], item["path"]))

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("prd_confirmed") is not True:
            raise _web_error(
                "prd_confirmation_required", "必须由产品负责人确认 PRD 基线", "勾选确认后再创建项目"
            )
        if payload.get("requirements_confirmed") is not True:
            raise _web_error(
                "requirements_confirmation_required", "必须确认 PRD 已覆盖七类最低信息",
                "检查 PRD 的用户、流程、范围、验收、成本和数据要求",
            )
        target = self.resolve_project_path(str(payload.get("directory", "")), require_managed=False)
        project_id = _required_text(payload, "project_id")
        name = _required_text(payload, "name")
        confirmed_by = _required_text(payload, "confirmed_by")
        stage_id = _required_text(payload, "stage_id")
        stage_name = _required_text(payload, "stage_name")
        prd_source = Path(_required_text(payload, "prd_path")).expanduser().resolve()
        constraints_raw = str(payload.get("constraints_path", "")).strip()
        constraints_source = Path(constraints_raw).expanduser().resolve() if constraints_raw else None
        intake = IntakeRecord(
            schema_version="1.0",
            project_id=project_id,
            prd_confirmed=True,
            confirmed_by=confirmed_by,
            confirmed_at=datetime.now(timezone.utc),
            requirements={
                key: RequirementDeclaration(
                    status=RequirementStatus.PRESENT,
                    source="PRD.md（产品负责人通过本地 Web 控制台确认）",
                )
                for key in sorted(REQUIREMENT_KEYS)
            },
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8") as temporary:
            yaml.safe_dump(
                intake.model_dump(mode="json"), temporary, allow_unicode=True, sort_keys=False
            )
            temporary.flush()
            if self.factory_root is not None:
                initialize_project(
                    target, project_id, name, prd_source, Path(temporary.name),
                    [(stage_id, stage_name, bool(payload.get("requires_real_model", False)))],
                    self.factory_root, constraints_source,
                )
            else:
                with factory_resource_root() as root:
                    initialize_project(
                        target, project_id, name, prd_source, Path(temporary.name),
                        [(stage_id, stage_name, bool(payload.get("requires_real_model", False)))],
                        root, constraints_source,
                    )
        return self.snapshot(target)

    def snapshot(self, project_path: Path | str) -> dict[str, Any]:
        root = self.resolve_project_path(str(project_path), require_managed=True)
        repo = ProjectRepository(root)
        project = repo.load_project()
        state = repo.load_state()
        validation = validate_project(root)
        recovery = resume_project(root)
        lock = LockManager(root).status()
        return _normalise(
            {
                "path": root,
                "project": project,
                "state": state,
                "validation": validation,
                "recovery": recovery,
                "lock": lock,
                "agent_run": self.agent_runs.latest_for(root),
            }
        )

    def perform_action(self, project_path: Path | str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        root = self.resolve_project_path(str(project_path), require_managed=True)

        def operation(lock_id: str, revision: int) -> None:
            if action == "check_inputs":
                check_inputs(root, lock_id, revision)
            elif action == "request_adaptation":
                artifact = Path(str(payload.get("artifact") or "docs/technical-adaptation.md"))
                WorkflowService(root).request_approval(
                    GateType.TECHNICAL_ADAPTATION, artifact, lock_id, revision
                )
            elif action == "approve":
                statement = _required_text(payload, "statement")
                actor = _required_text(payload, "actor")
                WorkflowService(root).approve(statement, actor, lock_id, revision)
            elif action == "start_verification":
                WorkflowService(root).start_verification(lock_id, revision)
            elif action == "record_evidence":
                manifest = Path(_required_text(payload, "manifest"))
                record_evidence(root, manifest, lock_id, revision)
            elif action == "verify_stage":
                verify_stage(root, _required_text(payload, "evidence_id"), lock_id, revision)
            elif action == "request_acceptance":
                WorkflowService(root).request_approval(
                    GateType.STAGE_ACCEPTANCE, None, lock_id, revision
                )
            elif action == "repair_audit":
                repair_audit(root, lock_id, revision)
            else:
                raise _web_error("web_action_invalid", "未知的流程操作", "刷新页面后重试")

        self._with_lease(root, operation)
        return self.snapshot(root)

    def start_agent(self, project_path: Path | str, objective: str) -> dict[str, Any]:
        root = self.resolve_project_path(str(project_path), require_managed=True)
        state = ProjectRepository(root).load_state()
        if state.waiting_on is not None or state.workflow_state not in {
            WorkflowState.INPUTS_CHECKED,
            WorkflowState.STAGE_DEVELOPMENT,
            WorkflowState.SYSTEM_VERIFICATION,
        }:
            raise FactoryError(
                "agent_stage_blocked", ErrorCategory.POLICY_BLOCKED,
                "当前阶段不允许启动编码 Agent", "web agent", False,
                "先完成当前人工操作或进入可执行阶段",
            )
        active = self.agent_runs.latest_for(root)
        if active and active["status"] == "running":
            raise _web_error("agent_already_running", "这个项目已有 Codex 任务在运行", "等待任务完成")
        return self.agent_runs.start(root, objective)

    def resolve_project_path(self, value: str, *, require_managed: bool) -> Path:
        raw = Path(value).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (self.workspace / raw).resolve()
        try:
            relative = candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise _web_error("project_path_invalid", "项目必须位于工作区内", "选择工作区内的新目录") from exc
        if not relative.parts:
            raise _web_error("project_path_invalid", "工作区根目录不能直接作为项目", "填写项目子目录")
        if require_managed and not (
            candidate / ".product-factory" / "project.yaml"
        ).is_file():
            raise _web_error("project_not_initialized", "该目录不是受管项目", "先创建或初始化项目")
        return candidate

    @staticmethod
    def _with_lease(root: Path, operation) -> None:
        repo = ProjectRepository(root)
        revision = repo.load_state().revision
        manager = LockManager(root)
        lock = manager.acquire(
            LockOwner(
                tool="web-console", session_id=str(uuid4()), pid=os.getpid(), host=socket.gethostname()
            ),
            revision,
            timedelta(minutes=5),
        )
        try:
            operation(lock.lock_id, revision)
        finally:
            try:
                current = manager.status()
                if current is not None and current.lock_id == lock.lock_id:
                    manager.release(lock.lock_id)
            except FactoryError:
                pass


def _agent_prompt(objective: str) -> str:
    return f"""你是产品工厂当前阶段的执行 Agent。只在当前项目目录内工作。

开始前读取 inputs/PRD.md、.product-factory/project.yaml、.product-factory/state.json 和现有 docs。
遵守当前工作流阶段，只能分析、修改本地代码和文档、运行测试与浏览器检查。
不要修改 .product-factory 下的状态、审批、锁、事件或证据协议文件。
不要替产品负责人批准，不要跨阶段，不要部署，不要创建付费资源，不要读取或输出密钥。
完成后总结改动、验证结果、已知问题和下一步需要人工执行的操作。

本次目标：
{objective}
"""


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise _web_error("field_required", f"缺少必填字段：{key}", "补全表单后重试")
    return value


def _web_error(code: str, message: str, action: str) -> FactoryError:
    return FactoryError(code, ErrorCategory.INPUT_REQUIRED, message, "web", False, action)
