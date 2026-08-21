"""Application service used by the local browser console."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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
from product_factory.storage.files import atomic_write_json, read_contained_regular_bytes


_MAX_UPLOAD_BYTES = 1_000_000
_MAX_AGENT_OUTPUT_BYTES = 100_000


class AgentRunManager:
    """Launch Codex safely, stream its output, and retain project-local history."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("codex")
        self._runs: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_requested: set[str] = set()
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
        run_directory = self._run_directory(project, run_id)
        run_directory.mkdir(parents=True, exist_ok=False)
        (run_directory / "output.log").touch(exist_ok=False)
        self._persist(record)
        with self._mutex:
            self._runs[run_id] = record
        thread = threading.Thread(
            target=self._execute, args=(run_id, command, prompt, project), daemon=True,
            name=f"product-factory-agent-{run_id[:8]}",
        )
        thread.start()
        return self.get(run_id, project)

    def get(self, run_id: str, project: Path | None = None) -> dict[str, Any]:
        if not _valid_run_id(run_id):
            raise _web_error("agent_run_not_found", "找不到这次 Codex 任务", "刷新项目后重试")
        with self._mutex:
            record = self._runs.get(run_id)
        if record is None and project is not None:
            record = self._load(project.resolve(), run_id)
            if record is not None:
                with self._mutex:
                    self._runs[run_id] = record
        if record is None:
            raise _web_error("agent_run_not_found", "找不到这次 Codex 任务", "刷新项目后重试")
        return dict(record)

    def latest_for(self, project: Path) -> dict[str, Any] | None:
        history = self.history(project)
        return history[0] if history else None

    def history(self, project: Path) -> list[dict[str, Any]]:
        project = project.resolve()
        records: dict[str, dict[str, Any]] = {}
        root = project / ".product-factory" / "agent-runs"
        try:
            entries = list(os.scandir(root))
        except FileNotFoundError:
            entries = []
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or not _valid_run_id(entry.name):
                continue
            record = self._load(project, entry.name)
            if record is not None:
                records[record["run_id"]] = record
        with self._mutex:
            for record in self._runs.values():
                if record.get("project_path") == str(project):
                    records[record["run_id"]] = dict(record)
        return sorted(records.values(), key=lambda item: item.get("started_at", ""), reverse=True)

    def cancel(self, run_id: str, project: Path) -> dict[str, Any]:
        current = self.get(run_id, project)
        if current["status"] not in {"running", "cancelling"}:
            return current
        with self._mutex:
            self._cancel_requested.add(run_id)
            process = self._processes.get(run_id)
            record = self._runs[run_id]
            record["status"] = "cancelling"
            snapshot = dict(record)
        self._persist(snapshot)
        if process is not None and process.poll() is None:
            process.terminate()
        return snapshot

    def _execute(self, run_id: str, command: list[str], prompt: str, project: Path) -> None:
        process: subprocess.Popen[str] | None = None
        log_path = self._run_directory(project, run_id) / "output.log"
        failure_output = ""
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=project,
                bufsize=1,
            )
            with self._mutex:
                self._processes[run_id] = process
                cancel_now = run_id in self._cancel_requested
            if cancel_now:
                process.terminate()
            if process.stdin is not None:
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            with log_path.open("a", encoding="utf-8", newline="") as log:
                if process.stdout is not None:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        with self._mutex:
                            record = self._runs[run_id]
                            record["output"] = (record["output"] + line)[-_MAX_AGENT_OUTPUT_BYTES:]
                log.flush()
                os.fsync(log.fileno())
            exit_code = process.wait()
            with self._mutex:
                cancelled = run_id in self._cancel_requested
            status = "cancelled" if cancelled else "completed" if exit_code == 0 else "failed"
        except (OSError, subprocess.SubprocessError) as exc:
            failure_output = f"Codex 启动失败：{type(exc).__name__}"
            status = "failed"
            exit_code = None
            try:
                log_path.write_text(failure_output, encoding="utf-8")
            except OSError:
                pass
        with self._mutex:
            record = self._runs[run_id]
            snapshot = dict(record)
            if status == "failed" and not snapshot["output"]:
                snapshot["output"] = failure_output
            snapshot.update(
                status=status,
                exit_code=exit_code,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            self._processes.pop(run_id, None)
            self._cancel_requested.discard(run_id)
        self._persist(snapshot)
        with self._mutex:
            self._runs[run_id].update(snapshot)

    @staticmethod
    def _run_directory(project: Path, run_id: str) -> Path:
        return project / ".product-factory" / "agent-runs" / run_id

    def _persist(self, record: dict[str, Any]) -> None:
        payload = {key: value for key, value in record.items() if key != "output"}
        atomic_write_json(
            self._run_directory(Path(record["project_path"]), record["run_id"]) / "run.json",
            payload,
        )

    def _load(self, project: Path, run_id: str) -> dict[str, Any] | None:
        try:
            metadata = json.loads(
                read_contained_regular_bytes(
                    project, (".product-factory", "agent-runs", run_id, "run.json")
                )
            )
            output = read_contained_regular_bytes(
                project, (".product-factory", "agent-runs", run_id, "output.log")
            ).decode("utf-8", errors="replace")[-_MAX_AGENT_OUTPUT_BYTES:]
        except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict) or metadata.get("run_id") != run_id:
            return None
        if metadata.get("project_path") != str(project):
            return None
        metadata["output"] = output
        if metadata.get("status") in {"running", "cancelling"}:
            metadata["status"] = "interrupted"
            metadata["finished_at"] = metadata.get("finished_at") or datetime.now(timezone.utc).isoformat()
        return metadata


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
                    repo = ProjectRepository(candidate)
                    project = repo.load_project()
                    state = repo.load_state()
                    validation = validate_project(candidate)
                except (FactoryError, OSError, ValueError):
                    continue
                projects.append(_normalise(
                    {
                        "path": str(candidate.resolve()),
                        "project_id": project.project_id,
                        "name": project.name,
                        "workflow_state": state.workflow_state,
                        "revision": state.revision,
                        "current_stage": state.current_stage,
                        "waiting_on": state.waiting_on,
                        "updated_at": state.updated_at,
                        "valid": validation.valid,
                        "agent_run": self.agent_runs.latest_for(candidate),
                    }
                ))
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
        with tempfile.TemporaryDirectory(prefix="product-factory-web-") as staging_raw:
            staging = Path(staging_raw)
            if "prd_content" in payload:
                prd_source = _write_uploaded_document(payload, "prd_content", staging / "PRD.md")
            else:
                prd_source = Path(_required_text(payload, "prd_path")).expanduser().resolve()
            if "constraints_content" in payload:
                constraints_source = _write_uploaded_document(
                    payload, "constraints_content", staging / "constraints.md"
                )
            else:
                constraints_raw = str(payload.get("constraints_path", "")).strip()
                constraints_source = (
                    Path(constraints_raw).expanduser().resolve() if constraints_raw else None
                )
            intake_source = staging / "intake.yaml"
            intake_source.write_text(
                yaml.safe_dump(
                    intake.model_dump(mode="json"), allow_unicode=True, sort_keys=False
                ),
                encoding="utf-8",
            )
            if self.factory_root is not None:
                initialize_project(
                    target, project_id, name, prd_source, intake_source,
                    [(stage_id, stage_name, bool(payload.get("requires_real_model", False)))],
                    self.factory_root, constraints_source,
                )
            else:
                with factory_resource_root() as root:
                    initialize_project(
                        target, project_id, name, prd_source, intake_source,
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
        events = repo.read_events()
        approvals = repo.read_approvals()
        activity = [
            {
                "kind": "event",
                "type": event.event_type,
                "created_at": event.created_at,
                "revision": event.after_revision,
                "details": event.details,
            }
            for event in events
        ] + [
            {
                "kind": "approval",
                "type": approval.gate_type.value,
                "created_at": approval.created_at,
                "revision": approval.consumed_by_revision,
                "details": {"actor": approval.actor, "scope": approval.scope},
            }
            for approval in approvals
        ]
        activity.sort(key=lambda item: item["created_at"], reverse=True)
        documents = [
            self._document(root, "prd", "产品需求文档", ("inputs", "PRD.md")),
            self._document(
                root, "technical-adaptation", "技术适配方案", ("docs", "technical-adaptation.md")
            ),
            self._document(root, "constraints", "项目约束", ("inputs", "constraints.md")),
        ]
        return _normalise(
            {
                "path": root,
                "project": project,
                "state": state,
                "validation": validation,
                "recovery": recovery,
                "lock": lock,
                "agent_run": self.agent_runs.latest_for(root),
                "documents": documents,
                "activity": activity[:30],
                "stats": {
                    "events": len(events),
                    "approvals": len(approvals),
                    "evidence": self._evidence_count(root),
                },
            }
        )

    @staticmethod
    def _document(root: Path, identifier: str, title: str, parts: tuple[str, ...]) -> dict[str, Any]:
        try:
            content = read_contained_regular_bytes(root, parts)
            decoded = content.decode("utf-8")
            return {
                "id": identifier,
                "title": title,
                "path": "/".join(parts),
                "exists": True,
                "content": decoded[:100_000],
                "truncated": len(decoded) > 100_000,
            }
        except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
            return {
                "id": identifier,
                "title": title,
                "path": "/".join(parts),
                "exists": False,
                "content": "",
                "truncated": False,
            }

    @staticmethod
    def _evidence_count(root: Path) -> int:
        evidence_root = root / ".product-factory" / "evidence"
        count = 0
        try:
            for current, directories, files in os.walk(evidence_root, followlinks=False):
                directories[:] = [name for name in directories if not name.startswith(".")]
                if "manifest.json" in files and (Path(current) / "manifest.json").is_file():
                    count += 1
        except OSError:
            return 0
        return count

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
        if active and active["status"] in {"running", "cancelling"}:
            raise _web_error("agent_already_running", "这个项目已有 Codex 任务在运行", "等待任务完成")
        return self.agent_runs.start(root, objective)

    def agent_history(self, project_path: Path | str) -> list[dict[str, Any]]:
        root = self.resolve_project_path(str(project_path), require_managed=True)
        return self.agent_runs.history(root)

    def get_agent_run(self, project_path: Path | str, run_id: str) -> dict[str, Any]:
        root = self.resolve_project_path(str(project_path), require_managed=True)
        return self.agent_runs.get(run_id, root)

    def cancel_agent(self, project_path: Path | str, run_id: str) -> dict[str, Any]:
        root = self.resolve_project_path(str(project_path), require_managed=True)
        return self.agent_runs.cancel(run_id, root)

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


def _write_uploaded_document(payload: dict[str, Any], key: str, destination: Path) -> Path:
    value = payload.get(key)
    if not isinstance(value, str):
        raise _web_error("prd_upload_invalid", "上传的文档无法读取", "重新选择 Markdown 或文本文件")
    encoded = value.encode("utf-8")
    if not value.strip() or len(encoded) > _MAX_UPLOAD_BYTES:
        raise _web_error(
            "prd_upload_invalid", "上传的文档为空或超过 1 MB", "选择 1 MB 以内的非空文本文件"
        )
    destination.write_bytes(encoded)
    return destination


def _web_error(code: str, message: str, action: str) -> FactoryError:
    return FactoryError(code, ErrorCategory.INPUT_REQUIRED, message, "web", False, action)


def _valid_run_id(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False
