"""The intentionally small, explicit Product Factory command grammar."""

from __future__ import annotations

import argparse
import errno
import os
import sys
from pathlib import Path


class ArgumentParseError(Exception):
    """A deliberately detail-free parse failure for the public CLI boundary."""


class ProtocolArgumentParser(argparse.ArgumentParser):
    """Do not let argparse echo untrusted arguments or terminate the process."""

    def error(self, message: str) -> None:
        del message
        raise ArgumentParseError

    def _print_message(self, message: str | None, file=None) -> None:
        """Write argparse's built-in help without leaking a closed pipe at exit."""
        if not message:
            return
        destination = sys.stdout if file is None else file
        try:
            destination.write(message)
            destination.flush()
        except (BrokenPipeError, OSError) as error:
            if error.errno != errno.EPIPE:
                raise
            self._stdout_pipe_closed = True
            _silence_output_stream(destination)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            self._print_message(message, sys.stderr)
        if getattr(self, "_stdout_pipe_closed", False):
            status = 0
        raise SystemExit(status)


def _silence_output_stream(stream) -> None:
    """Replace a broken output descriptor so shutdown cannot reflush EPIPE."""
    try:
        stream_fd = stream.fileno()
    except (AttributeError, OSError):
        # Test capture streams and StringIO do not necessarily expose a file
        # descriptor.  They also have no OS-level descriptor to reflush.
        return

    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull_fd, stream_fd)
    except OSError:
        return
    finally:
        os.close(devnull_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = ProtocolArgumentParser(prog="product-factory", description="产品工厂交付协议命令行")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="输出稳定的 JSON 结果")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=ProtocolArgumentParser
    )

    web = commands.add_parser("web", help="启动本地浏览器工作台")
    web.add_argument(
        "--workspace",
        default=str(Path.home() / "ProductFactoryProjects"),
        help="产品项目所在的本地工作区",
    )
    web.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    web.add_argument("--port", type=int, default=8765, help="监听端口")
    web.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    web.add_argument(
        "--allow-network", action="store_true", help="显式允许监听非本机地址（不推荐）"
    )

    init = commands.add_parser("init", help="初始化一个新的受管项目")
    _project(init, required=True)
    init.add_argument("--project-id", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--prd", required=True)
    init.add_argument("--intake", required=True)
    init.add_argument("--stage", action="append", required=True, metavar="ID:NAME[:requires_real_model]")

    inputs = commands.add_parser("check-inputs", help="检查并确认项目输入")
    _mutation(inputs)

    status = commands.add_parser("status", help="显示项目状态")
    _project(status, required=True)

    request = commands.add_parser("request-approval", help="请求人工审批")
    _mutation(request)
    request.add_argument("--gate", choices=("technical_adaptation", "stage_acceptance"), required=True)
    request.add_argument("--artifact")

    approve = commands.add_parser("approve", help="交互式提交批准语句")
    _mutation(approve)
    approve.add_argument("--actor", required=True)

    evidence = commands.add_parser("record-evidence", help="登记阶段证据")
    _mutation(evidence)
    evidence.add_argument("--manifest", required=True)

    verify = commands.add_parser("verify-stage", help="验证已登记的阶段证据")
    _mutation(verify)
    verify.add_argument("--evidence-id", required=True)

    transition = commands.add_parser("transition", help="推进受允许的工作流状态")
    _mutation(transition)
    transition.add_argument("--to", choices=("system_verification",), required=True)

    lock = commands.add_parser("lock", help="管理执行锁")
    lock_commands = lock.add_subparsers(dest="lock_command", required=True)
    acquire = lock_commands.add_parser("acquire", help="获取执行锁")
    _project(acquire, required=True)
    acquire.add_argument("--tool", required=True)
    acquire.add_argument("--session-id", required=True)
    acquire.add_argument("--lease-seconds", type=int, required=True)

    lock_status = lock_commands.add_parser("status", help="显示执行锁")
    _project(lock_status, required=True)

    heartbeat = lock_commands.add_parser("heartbeat", help="续约执行锁")
    _project(heartbeat, required=True)
    heartbeat.add_argument("--lock-id", required=True)
    heartbeat.add_argument("--lease-seconds", type=int, required=True)

    release = lock_commands.add_parser("release", help="释放执行锁")
    _project(release, required=True)
    release.add_argument("--lock-id", required=True)

    takeover = lock_commands.add_parser("takeover", help="接管过期执行锁")
    _project(takeover, required=True)
    takeover.add_argument("--old-lock-id", required=True)
    takeover.add_argument("--tool", required=True)
    takeover.add_argument("--session-id", required=True)
    takeover.add_argument("--reason", required=True)
    takeover.add_argument("--lease-seconds", type=int, required=True)

    resume = commands.add_parser("resume", help="生成只读恢复摘要")
    _project(resume, required=True)
    validate = commands.add_parser("validate", help="校验项目协议记录")
    _project(validate, required=True)
    repair = commands.add_parser("repair-audit", help="显式补写缺失的审计事件")
    _mutation(repair)
    return parser


def _project(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--project", required=required, metavar="PATH")


def _mutation(parser: argparse.ArgumentParser) -> None:
    _project(parser, required=True)
    parser.add_argument("--lock-id", required=True)
    parser.add_argument("--expected-revision", type=int, required=True, metavar="N")
