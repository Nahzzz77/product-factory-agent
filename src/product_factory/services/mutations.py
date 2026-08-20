"""Shared, crash-recoverable state mutation primitives."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from product_factory.contracts.models import EventRecord, StateRecord
from product_factory.storage.repository import ProjectRepository


def commit_state_change(
    repo: ProjectRepository,
    current: StateRecord,
    next_state: StateRecord,
    event_type: str,
    details: dict[str, Any],
) -> StateRecord:
    """Commit state before its audit event, retaining repairable event identity on failure."""
    event_id = str(uuid4())
    committed = next_state.model_copy(update={"last_event_id": event_id})
    repo.save_state(committed, expected_revision=current.revision)
    repo.append_event(
        EventRecord(
            schema_version="1.0",
            event_id=event_id,
            event_type=event_type,
            project_id=current.project_id,
            before_revision=current.revision,
            after_revision=committed.revision,
            created_at=datetime.now(timezone.utc),
            details=details,
        )
    )
    return committed
