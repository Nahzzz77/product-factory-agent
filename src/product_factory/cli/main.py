"""Installed ``product-factory`` entry point and service-only dispatch."""

from __future__ import annotations

import os
import socket
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from pydantic import ValidationError

from product_factory.cli.output import failure, internal_failure, render, success
from product_factory.cli.parser import ArgumentParseError, build_parser
from product_factory.contracts.models import EventRecord, GateType, LockOwner, LockRecord
from product_factory.errors import ErrorCategory, FactoryError
from product_factory.resources import factory_resource_root
from product_factory.services.evidence import record_evidence, verify_stage
from product_factory.services.initialize import check_inputs, initialize_project
from product_factory.services.recovery import repair_audit, resume_project, validate_project
from product_factory.services.workflow import WorkflowService
from product_factory.storage.locks import LockManager
from product_factory.storage.repository import ProjectRepository


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command, exposing only protocol-safe outcome data."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    # This pre-parse check is intentionally only a mode selector.  It never
    # includes argv text in an error envelope, so malformed secret-bearing
    # arguments cannot be reflected by argparse diagnostics.
    json_mode = "--json" in arguments
    parser = build_parser()
    try:
        namespace = parser.parse_args(arguments)
        envelope = _dispatch(namespace)
    except ArgumentParseError:
        envelope = failure(_argument_error())
        return _emit(envelope, json_mode, 2)
    except KeyboardInterrupt:
        envelope = failure(_interrupted_error())
        return _emit(envelope, json_mode, _interrupted_error().exit_code)
    except FactoryError as error:
        envelope = failure(error)
        return _emit(envelope, json_mode, error.exit_code)
    except Exception:
        # Unexpected exceptions may contain source paths, environment values, or
        # library diagnostics.  They are intentionally not terminal output.
        envelope = internal_failure()
        return _emit(envelope, json_mode, 10)
    return _emit(envelope, json_mode, 0)


def _dispatch(args: Any):
    if args.command == "web":
        from product_factory.web.app import run_console

        run_console(
            Path(args.workspace), host=args.host, port=args.port,
            open_browser=not args.no_open, allow_network=args.allow_network,
        )
        return success("web_stopped", "本地工作台已停止", "需要时重新运行 product-factory web", {})

    root = Path(args.project)
    if args.command == "init":
        with factory_resource_root() as factory_root:
            state = initialize_project(
                target=root,
                project_id=args.project_id,
                name=args.name,
                # Resolve CLI input paths from the caller's current directory.
                prd_source=Path(args.prd).resolve(),
                intake_source=Path(args.intake).resolve(),
                stage_specs=[_parse_stage(value) for value in args.stage],
                factory_root=factory_root,
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
        _repository_or_error(root, "approve")
        service = WorkflowService(root)
        # Validate the project and lease before asking a human for a statement.
        # The service rechecks this whole boundary after input for TOCTOU safety.
        service.prepare_approval(args.lock_id, args.expected_revision)
        statement = _read_approval_statement(args.json_mode)
        state = service.approve(statement, args.actor, args.lock_id, args.expected_revision)
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
        if not report.valid:
            raise FactoryError(
                "validation_failed", ErrorCategory.INPUT_REQUIRED, "项目协议记录存在问题",
                "validate", False, "根据 findings 修复后重新验证", {"findings": report.findings},
            )
        return success("validation_passed", "项目协议记录有效", "继续当前工作流", {"valid": True, "findings": []})

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
        def prepare_takeover(old: LockRecord):
            # This provider is invoked only after LockManager owns the same
            # SQLite mutation mutex used by every state transition.  Never load
            # state before this point: its revision is the replacement fence.
            repo = ProjectRepository(root)
            try:
                project = repo.load_project()
                state = repo.load_state()
            except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
                raise FactoryError(
                    "project_protocol_invalid", ErrorCategory.ENVIRONMENT_BLOCKED,
                    "项目协议记录无效，不能审计接管执行锁", "lock takeover", False,
                    "先运行 validate 并修复项目协议记录",
                ) from exc
            if project.project_id != state.project_id:
                raise FactoryError(
                    "project_identity_mismatch", ErrorCategory.ENVIRONMENT_BLOCKED,
                    "项目状态与项目元数据的标识不一致", "lock takeover", False,
                    "修复项目元数据后重试",
                )

            def append_authorization(replacement: LockRecord) -> None:
                details = {
                    "old_lock_id": old.lock_id, "new_lock_id": replacement.lock_id,
                    "reason": args.reason, "old_owner": old.owner.model_dump(mode="json"),
                    "new_owner": replacement.owner.model_dump(mode="json"),
                    "old_acquired_at": old.acquired_at.isoformat(),
                    "old_lease_expires_at": old.lease_expires_at.isoformat(),
                    "new_acquired_at": replacement.acquired_at.isoformat(),
                    "new_lease_expires_at": replacement.lease_expires_at.isoformat(),
                }
                try:
                    repo.append_event(EventRecord(
                        schema_version="1.0", event_id=str(uuid4()), event_type="lock_takeover_authorized",
                        project_id=project.project_id, before_revision=state.revision,
                        after_revision=state.revision, created_at=datetime.now(timezone.utc), details=details,
                    ))
                except OSError as exc:
                    raise FactoryError(
                        "takeover_audit_failed", ErrorCategory.ENVIRONMENT_BLOCKED,
                        "执行锁接管审计未能写入，原锁未变更", "lock takeover", True,
                        "修复审计存储后重试接管",
                        details,
                    ) from exc
            return state.revision, append_authorization

        result = manager.takeover(
            args.old_lock_id, _owner(args.tool, args.session_id), 0, args.reason,
            _lease(args.lease_seconds), prepared=prepare_takeover,
        )
        return success("lock_taken_over", "已接管过期执行锁", "使用新锁 ID 继续操作", {"lock": result.lock, "takeover": result.details})
    raise RuntimeError("unreachable lock command")


def _takeover_repository(root: Path) -> ProjectRepository:
    repo = _repository_or_error(root, "lock takeover")
    try:
        repo.load_project()
        repo.load_state()
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise FactoryError(
            "project_protocol_invalid", ErrorCategory.ENVIRONMENT_BLOCKED,
            "项目协议记录无效，不能审计接管执行锁", "lock takeover", False,
            "先运行 validate 并修复项目协议记录",
        ) from exc
    return repo


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
    except KeyboardInterrupt as exc:
        raise _interrupted_error() from exc


def _argument_error() -> FactoryError:
    return FactoryError(
        "argument_invalid", ErrorCategory.INPUT_REQUIRED, "命令参数无效", "arguments", False,
        "运行 product-factory --help 查看命令用法",
    )


def _interrupted_error() -> FactoryError:
    return FactoryError(
        "interrupted", ErrorCategory.INTERRUPTED, "操作已中断", "interrupted", True,
        "确认当前状态后重新运行命令",
    )


def _emit(envelope: Any, json_mode: bool, exit_code: int) -> int:
    return exit_code if _write(envelope, json_mode) else 0


def _write(envelope: Any, json_mode: bool) -> bool:
    destination = sys.stdout if json_mode or envelope.ok else sys.stderr
    try:
        destination.write(render(envelope, json_mode))
        destination.flush()
        return True
    except BrokenPipeError:
        # Replacing stdout prevents the interpreter's final flush from emitting
        # ``Exception ignored`` after a consumer (for example `head`) closes.
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass
        return False


if __name__ == "__main__":
    raise SystemExit(main())
