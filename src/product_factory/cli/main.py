"""Installed ``product-factory`` entry point and service-only dispatch."""

from __future__ import annotations

import os
import socket
import sys
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from product_factory.cli.output import failure, internal_failure, render, success
from product_factory.cli.parser import build_parser
from product_factory.contracts.models import GateType, LockOwner
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.services.evidence import record_evidence, verify_stage
from product_factory.services.initialize import check_inputs, initialize_project
from product_factory.services.recovery import repair_audit, resume_project, validate_project
from product_factory.services.workflow import WorkflowService
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command, exposing only protocol-safe outcome data."""
    parser = build_parser()
    namespace = parser.parse_args(argv)
    try:
        envelope = _dispatch(namespace)
    except FactoryError as error:
        envelope = failure(error)
        _write(envelope, namespace.json_mode)
        return error.exit_code
    except Exception:
        # Unexpected exceptions may contain source paths, environment values, or
        # library diagnostics.  They are intentionally not terminal output.
        envelope = internal_failure()
        _write(envelope, namespace.json_mode)
        return 1
    _write(envelope, namespace.json_mode)
    return 0


def _dispatch(args: Any):
    root = Path(args.project)
    if args.command == "init":
        state = initialize_project(
            target=root,
            project_id=args.project_id,
            name=args.name,
            # Resolve CLI input paths from the caller's current directory;
            # initialization still reads the bundled handbook manifest below.
            prd_source=Path(args.prd).resolve(),
            intake_source=Path(args.intake).resolve(),
            stage_specs=[_parse_stage(value) for value in args.stage],
            factory_root=_factory_root(),
        )
        return success("initialized", "项目已初始化", "运行 check-inputs", {"state": state})

    if args.command == "check-inputs":
        state = check_inputs(root, args.lock_id, args.expected_revision)
        return success("inputs_checked", "项目输入已通过检查", "请求技术适配审批", {"state": state})

    if args.command == "status":
        repo = _repository_or_error(root, "status")
        return success(
            "status", "已读取项目状态", "根据当前状态继续操作",
            {"project": repo.load_project(), "state": repo.load_state(), "lock": LockManager(root).status()},
        )

    if args.command == "request-approval":
        state = WorkflowService(root).request_approval(
            GateType(args.gate), Path(args.artifact) if args.artifact else None,
            args.lock_id, args.expected_revision,
        )
        return success("approval_requested", "已请求人工审批", "运行 approve 并输入协议批准语句", {"state": state})

    if args.command == "approve":
        statement = _read_approval_statement(args.json_mode)
        state = WorkflowService(root).approve(statement, args.actor, args.lock_id, args.expected_revision)
        return success("approval_consumed", "审批已记录并消费", "继续下一项工作流操作", {"state": state})

    if args.command == "record-evidence":
        manifest = record_evidence(root, Path(args.manifest), args.lock_id, args.expected_revision)
        return success("evidence_recorded", "阶段证据已登记", "运行 verify-stage", {"evidence": manifest})

    if args.command == "verify-stage":
        state = verify_stage(root, args.evidence_id, args.lock_id, args.expected_revision)
        return success("stage_verified", "阶段证据已验证", "请求阶段验收或继续工作流", {"state": state})

    if args.command == "transition":
        # The parser only exposes this one V1 transition.  The service retains
        # the actual transition eligibility and lock/revision enforcement.
        state = WorkflowService(root).start_verification(args.lock_id, args.expected_revision)
        return success("transitioned", "已进入系统验证", "登记阶段证据", {"state": state})

    if args.command == "resume":
        return success("recovery_summary", "已生成恢复摘要", "按 next_command 操作", {"summary": resume_project(root)})

    if args.command == "validate":
        report = validate_project(root)
        code = "validation_passed" if report.valid else "validation_failed"
        message = "项目协议记录有效" if report.valid else "项目协议记录存在问题"
        action = "继续当前工作流" if report.valid else "根据 findings 修复后重新验证"
        return success(code, message, action, {"valid": report.valid, "findings": report.findings})

    if args.command == "repair-audit":
        event = repair_audit(root, args.lock_id, args.expected_revision)
        return success("audit_repaired", "缺失审计事件已补写", "运行 validate 确认项目一致性", {"event": event})

    if args.command == "lock":
        return _dispatch_lock(args, root)
    raise RuntimeError("unreachable command")


def _dispatch_lock(args: Any, root: Path):
    manager = LockManager(root)
    if args.lock_command == "acquire":
        record = manager.acquire(_owner(args.tool, args.session_id), _state_revision(root), _lease(args.lease_seconds))
        return success("lock_acquired", "已获取执行锁", "在租约到期前完成写操作或续约", {"lock": record})
    if args.lock_command == "status":
        return success("lock_status", "已读取执行锁", "根据锁状态继续操作", {"lock": manager.status()})
    if args.lock_command == "heartbeat":
        record = manager.heartbeat(args.lock_id, _lease(args.lease_seconds))
        return success("lock_renewed", "执行锁已续约", "继续操作或完成后释放锁", {"lock": record})
    if args.lock_command == "release":
        manager.release(args.lock_id)
        return success("lock_released", "执行锁已释放", "其他会话现在可以获取锁", {})
    if args.lock_command == "takeover":
        result = manager.takeover(
            args.old_lock_id, _owner(args.tool, args.session_id), _state_revision(root), args.reason,
            _lease(args.lease_seconds),
        )
        return success("lock_taken_over", "已接管过期执行锁", "使用新锁 ID 继续操作", {"lock": result.lock, "takeover": result.details})
    raise RuntimeError("unreachable lock command")


def _parse_stage(value: str) -> tuple[str, str, bool]:
    parts = value.split(":")
    if len(parts) not in {2, 3} or not parts[0] or not parts[1]:
        raise _invalid_stage()
    if len(parts) == 3 and parts[2] != "requires_real_model":
        raise _invalid_stage()
    return parts[0], parts[1], len(parts) == 3


def _invalid_stage() -> FactoryError:
    return FactoryError(
        "stage_invalid", ErrorCategory.INPUT_REQUIRED, "阶段格式必须为 ID:NAME[:requires_real_model]",
        "init", False, "修正 --stage 参数",
    )


def _owner(tool: str, session_id: str) -> LockOwner:
    try:
        return LockOwner(tool=tool, session_id=session_id, pid=os.getpid(), host=socket.gethostname())
    except ValidationError as exc:
        raise FactoryError(
            "lock_owner_invalid", ErrorCategory.INPUT_REQUIRED, "执行锁所有者信息无效",
            "lock", False, "提供有效的 tool 和 session-id",
        ) from exc


def _lease(seconds: int) -> timedelta:
    if seconds <= 0:
        raise FactoryError(
            "lease_invalid", ErrorCategory.INPUT_REQUIRED, "执行锁租约必须为正整数秒",
            "lock", False, "提供大于 0 的 --lease-seconds",
        )
    return timedelta(seconds=seconds)


def _state_revision(root: Path) -> int:
    return _repository_or_error(root, "lock").load_state().revision


def _repository_or_error(root: Path, step: str) -> ProjectRepository:
    try:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(resolved)
        repo = ProjectRepository(resolved)
        if not repo.paths.project.is_file() or not repo.paths.state.is_file():
            raise FileNotFoundError(repo.paths.metadata)
        return repo
    except (OSError, RuntimeError) as exc:
        raise FactoryError(
            "project_not_initialized", ErrorCategory.INPUT_REQUIRED, "项目尚未初始化或不可读取",
            step, False, "先运行 init 或检查 --project 路径",
        ) from exc


def _read_approval_statement(json_mode: bool) -> str:
    try:
        # JSON stdout must remain a single parseable envelope.  The exact
        # interactive prompt is instead shown on stderr in that mode.
        with redirect_stdout(sys.stderr):
            return input("请输入批准语句：")
    except EOFError as exc:
        raise FactoryError(
            "approval_statement_required", ErrorCategory.APPROVAL_REQUIRED, "必须交互式输入批准语句",
            "approve", True, "重新运行 approve 并输入完整批准语句",
        ) from exc


def _factory_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _write(envelope: Any, json_mode: bool) -> None:
    destination = sys.stdout if json_mode or envelope.ok else sys.stderr
    print(render(envelope, json_mode), end="", file=destination)


if __name__ == "__main__":
    raise SystemExit(main())
