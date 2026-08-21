"""Stable, safe rendering of command outcomes."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from product_factory.contracts.models import ResultEnvelope
from product_factory.errors import FactoryError


def success(code: str, message: str, action: str, details: dict[str, Any]) -> ResultEnvelope:
    return ResultEnvelope(
        ok=True, code=code, category=None, message=message, step="complete", retryable=False,
        action=action, details=_normalise(details),
    )


def failure(error: FactoryError) -> ResultEnvelope:
    return ResultEnvelope(
        ok=False, code=error.code, category=error.category.value, message=error.message,
        step=error.step, retryable=error.retryable, action=error.action,
        details=_normalise(error.details),
    )


def internal_failure() -> ResultEnvelope:
    return ResultEnvelope(
        ok=False, code="internal_error", category="implementation_failed", message="命令未能安全完成",
        step="internal", retryable=False, action="检查本地受控日志或联系维护者", details={},
    )


def render(envelope: ResultEnvelope, json_mode: bool) -> str:
    if json_mode:
        # Keep ResultEnvelope's declared field order stable while sorting nested
        # metadata, whose source may be a dataclass or a filesystem implementation.
        return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    prefix = "完成" if envelope.ok else "未完成"
    return f"{prefix}：{envelope.message}\n下一步：{envelope.action}\n"


def _normalise(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalise(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _normalise(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _normalise(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value
