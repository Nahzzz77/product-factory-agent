# Product Factory Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline, installable `product-factory` CLI that initializes a new Web product project, enforces approval and evidence gates through `next_stage_or_frontend`, prevents concurrent writes, and restores state safely after interruption.

**Architecture:** JSON Schema files are generated from strict Pydantic contract models and committed as the language-neutral protocol. Pure domain functions decide transitions; repository and lock classes own filesystem safety; service classes orchestrate mutations; the CLI only parses input and renders stable human or JSON output.

**Tech Stack:** Python 3.11+, Pydantic 2, PyYAML 6, pytest 8, standard-library `argparse`, `pathlib`, `hashlib`, `json`, `os`, and `subprocess`.

**Spec:** `docs/superpowers/specs/2026-08-20-product-factory-core-design.md`

## Global Constraints

- V1 only initializes new Web product projects; it does not adopt legacy projects or create native App, mini-program, desktop, or game projects.
- All milestone-one tests run offline. Do not call models, cloud APIs, paid services, browsers, or external data services.
- Do not add FastAPI, Next.js, a database, a task queue, or a cloud SDK to the core package.
- Secrets must not enter CLI arguments, chat transcripts, repository files, logs, evidence, generated frontends, or test fixtures.
- Public contract models reject unknown fields. Generated JSON Schemas are committed and must match the models byte-for-byte after deterministic export.
- Implement workflow behavior only from `initialized` through `next_stage_or_frontend`; later protocol states validate but transitions into them return `unsupported_transition`.
- `status`, `resume`, and `validate` are read-only. `repair-audit` is the only command that repairs a referenced missing event and it cannot alter business state.
- All mutation commands require a valid lease lock and an expected state revision.
- Use atomic replacement for mutable snapshots and append-only writes for approvals and events.
- Source digests exclude `.git/`, `.product-factory/`, secret files, caches, and build outputs.
- Verify the completed milestone on the current macOS environment. Describe Windows and Linux as designed-compatible, not verified.
- Each task follows red-green-refactor and ends with a focused local Git commit. Do not configure a remote or push.

## Planned File Map

```text
pyproject.toml                              package metadata and test configuration
src/product_factory/__init__.py            public version export
src/product_factory/version.py             single version constant
src/product_factory/errors.py              stable error categories and exit codes
src/product_factory/contracts/models.py    strict protocol models
src/product_factory/contracts/export.py    deterministic JSON Schema export
src/product_factory/domain/states.py        workflow enums and transition rules
src/product_factory/domain/approvals.py     exact approval matching
src/product_factory/domain/evidence.py      evidence validity decisions
src/product_factory/storage/paths.py        project-contained paths
src/product_factory/storage/files.py        atomic JSON/YAML and JSONL primitives
src/product_factory/storage/repository.py   typed project persistence
src/product_factory/storage/locks.py        lease lock lifecycle
src/product_factory/services/mutations.py   state-first audited mutation helper
src/product_factory/services/initialize.py  project creation and input checks
src/product_factory/services/workflow.py    transitions and approval orchestration
src/product_factory/services/evidence.py    digest, record, and verify operations
src/product_factory/services/recovery.py    validation, resume, and audit repair
src/product_factory/cli/parser.py           argparse command tree
src/product_factory/cli/output.py           human and JSON rendering
src/product_factory/cli/main.py             command dispatch and exit handling
schemas/*.schema.json                       generated public schemas
templates/intake.yaml                       seven-category intake declaration
templates/technical-adaptation.md           first approval artifact
templates/stage-development.md              stage implementation record
templates/acceptance.md                     human acceptance checklist
templates/evidence-manifest.yaml            evidence authoring template
examples/minimal-project/                   deterministic demonstration input
tests/unit/                                 pure domain tests
tests/storage/                              filesystem and lease tests
tests/integration/                          services and CLI workflow tests
README.md                                   installation and operator guide
CHANGELOG.md                                milestone scope and limitations
```

## Spec Coverage Map

| Design requirement | Implemented by |
| --- | --- |
| Strict language-neutral contracts | Tasks 1–2 |
| Atomic storage, append-only audit, revision checks | Task 3 |
| Single-writer lease, heartbeat, release, takeover | Task 4 |
| Pure state, approval, and evidence rules | Task 5 |
| New-project initialization and seven-category intake | Task 6 |
| Adaptation and stage acceptance gates | Task 7 |
| Immutable evidence, source digest, staleness | Task 8 |
| Read-only validation/resume and explicit audit repair | Task 9 |
| Human and machine CLI contracts, stable exit codes | Task 10 |
| Templates, golden example, full offline proof, docs | Task 11 |
| Secret prohibition, no cloud or GitHub actions | Global constraints and Task 11 quality gate |

---

### Task 1: Package Skeleton, Errors, and Strict Contract Models

**Files:**
- Create: `pyproject.toml`
- Create: `src/product_factory/__init__.py`
- Create: `src/product_factory/version.py`
- Create: `src/product_factory/errors.py`
- Create: `src/product_factory/contracts/__init__.py`
- Create: `src/product_factory/contracts/models.py`
- Test: `tests/unit/test_contract_models.py`

**Interfaces:**
- Consumes: no earlier implementation.
- Produces: `__version__: str`; `ErrorCategory`, `FactoryError`; all Pydantic records used by every later task.

- [ ] **Step 1: Create the failing contract tests**

```python
# tests/unit/test_contract_models.py
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from product_factory.contracts.models import (
    CompletionLevel,
    CurrentStage,
    IntakeRecord,
    RequirementDeclaration,
    RequirementStatus,
    StateRecord,
    WorkflowState,
)


def test_state_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StateRecord.model_validate(
            {
                "schema_version": "1.0",
                "project_id": "demo",
                "revision": 0,
                "workflow_state": "initialized",
                "current_stage": {"id": "stage-01", "sequence": 1, "completion_level": "none"},
                "waiting_on": None,
                "last_valid_evidence_id": None,
                "last_event_id": None,
                "updated_at": "2026-08-20T00:00:00Z",
                "unexpected": True,
            }
        )


def test_intake_requires_all_seven_categories() -> None:
    declaration = RequirementDeclaration(status=RequirementStatus.PRESENT, source="PRD §1")
    with pytest.raises(ValidationError):
        IntakeRecord(
            schema_version="1.0",
            project_id="demo",
            prd_confirmed=True,
            confirmed_by="owner",
            confirmed_at=datetime.now(timezone.utc),
            requirements={"target_user_and_core_task": declaration},
        )


def test_state_enum_values_match_protocol() -> None:
    state = StateRecord(
        schema_version="1.0",
        project_id="demo",
        revision=0,
        workflow_state=WorkflowState.INITIALIZED,
        current_stage=CurrentStage(id="stage-01", sequence=1, completion_level=CompletionLevel.NONE),
        updated_at=datetime.now(timezone.utc),
    )
    assert state.workflow_state.value == "initialized"
```

- [ ] **Step 2: Run the tests and confirm the package is absent**

Run: `python -m pytest tests/unit/test_contract_models.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'product_factory'`.

- [ ] **Step 3: Add package metadata and the executable entry point**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "product-factory-agent"
version = "0.1.0"
description = "File-backed delivery protocol for local coding agents"
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.10,<3",
  "PyYAML>=6.0,<7",
]

[project.optional-dependencies]
dev = ["pytest>=8.3,<9"]

[project.scripts]
product-factory = "product_factory.cli.main:main"

[tool.hatch.build.targets.wheel]
packages = ["src/product_factory"]

[tool.pytest.ini_options]
addopts = "-ra"
testpaths = ["tests"]
```

```python
# src/product_factory/version.py
__version__ = "0.1.0"
```

```python
# src/product_factory/__init__.py
from product_factory.version import __version__

__all__ = ["__version__"]
```

- [ ] **Step 4: Add stable error types and exit codes**

```python
# src/product_factory/errors.py
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    INPUT_REQUIRED = "input_required"
    APPROVAL_REQUIRED = "approval_required"
    IMPLEMENTATION_FAILED = "implementation_failed"
    EXTERNAL_SERVICE_FAILED = "external_service_failed"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    POLICY_BLOCKED = "policy_blocked"
    INTERRUPTED = "interrupted"


EXIT_BY_CATEGORY = {
    ErrorCategory.INPUT_REQUIRED: 2,
    ErrorCategory.APPROVAL_REQUIRED: 3,
    ErrorCategory.ENVIRONMENT_BLOCKED: 4,
    ErrorCategory.IMPLEMENTATION_FAILED: 5,
    ErrorCategory.POLICY_BLOCKED: 6,
    ErrorCategory.EXTERNAL_SERVICE_FAILED: 10,
    ErrorCategory.INTERRUPTED: 10,
}


@dataclass(slots=True)
class FactoryError(Exception):
    code: str
    category: ErrorCategory
    message: str
    step: str
    retryable: bool
    action: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CATEGORY[self.category]
```

- [ ] **Step 5: Implement strict protocol models**

Create `src/product_factory/contracts/models.py` with `ConfigDict(extra="forbid")` on a shared base model and these exact public types:

```python
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowState(StrEnum):
    INITIALIZED = "initialized"
    INPUTS_CHECKED = "inputs_checked"
    ADAPTATION_PENDING_APPROVAL = "adaptation_pending_approval"
    STAGE_DEVELOPMENT = "stage_development"
    SYSTEM_VERIFICATION = "system_verification"
    HUMAN_ACCEPTANCE_PENDING = "human_acceptance_pending"
    NEXT_STAGE_OR_FRONTEND = "next_stage_or_frontend"
    RELEASE_READY = "release_ready"
    DEPLOYMENT_PENDING_APPROVAL = "deployment_pending_approval"
    DEPLOYED_PENDING_ACCEPTANCE = "deployed_pending_acceptance"
    PRODUCTION_ACCEPTED = "production_accepted"
    OBSERVING = "observing"


class CompletionLevel(StrEnum):
    NONE = "none"
    IMPLEMENTED = "implemented"
    SYSTEM_VERIFIED = "system_verified"
    HUMAN_ACCEPTED = "human_accepted"


class GateType(StrEnum):
    TECHNICAL_ADAPTATION = "technical_adaptation"
    STAGE_ACCEPTANCE = "stage_acceptance"


class RequirementStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class RequirementDeclaration(StrictModel):
    status: RequirementStatus
    source: str = Field(min_length=1)
    reason: str | None = None

    @model_validator(mode="after")
    def require_reason_for_not_applicable(self):
        if self.status is RequirementStatus.NOT_APPLICABLE and not self.reason:
            raise ValueError("not_applicable requires reason")
        return self


REQUIREMENT_KEYS = frozenset(
    {
        "target_user_and_core_task",
        "input_process_output",
        "user_flow_and_confirmations",
        "scope_and_priority",
        "acceptance_criteria",
        "model_cost_platform",
        "data_privacy_performance_deployment",
    }
)


class IntakeRecord(StrictModel):
    schema_version: Literal["1.0"]
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    prd_confirmed: bool
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime
    requirements: dict[str, RequirementDeclaration]

    @model_validator(mode="after")
    def require_exact_categories(self):
        if set(self.requirements) != REQUIREMENT_KEYS:
            raise ValueError("requirements must contain the seven protocol categories")
        return self


class PrdReference(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HandbookReference(StrictModel):
    title: str
    version: str
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StagePlanItem(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    name: str
    sequence: int = Field(ge=1)
    kind: Literal["development", "frontend"] = "development"
    requires_real_model: bool = False


class ProjectRecord(StrictModel):
    schema_version: Literal["1.0"]
    project_id: str
    name: str
    created_at: datetime
    factory_version: str
    prd: PrdReference
    constraints_path: str
    handbooks: list[HandbookReference]
    stage_plan: list[StagePlanItem] = Field(min_length=1)
    source_excludes: list[str]

    @model_validator(mode="after")
    def require_unique_ordered_stages(self):
        ids = [item.id for item in self.stage_plan]
        sequences = [item.sequence for item in self.stage_plan]
        if len(ids) != len(set(ids)) or sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("stage_plan requires unique ids and contiguous sequence values")
        return self


class CurrentStage(StrictModel):
    id: str
    sequence: int = Field(ge=1)
    completion_level: CompletionLevel


class WaitingOn(StrictModel):
    type: Literal["approval"]
    request_id: str
    gate_type: GateType
    scope: dict[str, Any]


class StateRecord(StrictModel):
    schema_version: Literal["1.0"]
    project_id: str
    revision: int = Field(ge=0)
    workflow_state: WorkflowState
    current_stage: CurrentStage
    waiting_on: WaitingOn | None = None
    last_valid_evidence_id: str | None = None
    last_event_id: str | None = None
    updated_at: datetime


class ApprovalRecord(StrictModel):
    schema_version: Literal["1.0"]
    approval_id: str
    request_id: str
    gate_type: GateType
    scope: dict[str, Any]
    state_revision: int
    statement: str
    actor: str
    source: Literal["interactive_cli"]
    created_at: datetime
    consumed_by_revision: int


class EventRecord(StrictModel):
    schema_version: Literal["1.0"]
    event_id: str
    event_type: str
    project_id: str
    before_revision: int
    after_revision: int
    created_at: datetime
    details: dict[str, Any]


class LockOwner(StrictModel):
    tool: str
    session_id: str
    pid: int
    host: str


class LockRecord(StrictModel):
    schema_version: Literal["1.0"]
    lock_id: str
    owner: LockOwner
    acquired_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime
    state_revision: int


class EvidenceCheck(StrictModel):
    name: str
    command: str
    started_at: datetime
    ended_at: datetime
    exit_status: int
    summary: str
    mode: Literal["not_applicable", "mock", "real"]
    artifact_paths: list[str] = Field(default_factory=list)


class KnownIssue(StrictModel):
    summary: str
    severity: Literal["low", "medium", "high", "critical"]
    blocking: bool


class EvidenceManifest(StrictModel):
    schema_version: Literal["1.0"]
    evidence_id: str
    stage_id: str
    state_revision: int
    factory_version: str
    prd_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_entry: str | None = None
    checks: list[EvidenceCheck] = Field(min_length=1)
    known_issues: list[KnownIssue] = Field(default_factory=list)
    ready_for_human_acceptance: bool


class ResultEnvelope(StrictModel):
    ok: bool
    code: str
    category: str | None
    message: str
    step: str
    retryable: bool
    action: str
    details: dict[str, Any]
```

- [ ] **Step 6: Run the contract tests**

Run: `python -m pip install -e '.[dev]' && python -m pytest tests/unit/test_contract_models.py -q`

Expected: `3 passed`.

- [ ] **Step 7: Commit the package and contract foundation**

```bash
git add pyproject.toml src/product_factory tests/unit/test_contract_models.py
git commit -m "feat: define core protocol models"
```

---

### Task 2: Deterministic JSON Schema Export

**Files:**
- Create: `src/product_factory/contracts/export.py`
- Create: `schemas/`
- Create: `tests/unit/test_schema_export.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: strict models from Task 1.
- Produces: `export_schemas(output_dir: Path) -> list[Path]` and committed `schemas/*.schema.json`.

- [ ] **Step 1: Write the failing deterministic export test**

```python
# tests/unit/test_schema_export.py
from pathlib import Path

from product_factory.contracts.export import SCHEMA_MODELS, export_schemas


def test_export_is_deterministic_and_complete(tmp_path: Path) -> None:
    first = export_schemas(tmp_path / "first")
    second = export_schemas(tmp_path / "second")
    assert [p.name for p in first] == [f"{name}.schema.json" for name in sorted(SCHEMA_MODELS)]
    assert [p.read_bytes() for p in first] == [p.read_bytes() for p in second]
```

- [ ] **Step 2: Run the test and verify the exporter is missing**

Run: `python -m pytest tests/unit/test_schema_export.py -q`

Expected: collection fails because `product_factory.contracts.export` does not exist.

- [ ] **Step 3: Implement sorted, newline-terminated exports**

```python
# src/product_factory/contracts/export.py
import json
from pathlib import Path

from product_factory.contracts.models import (
    ApprovalRecord,
    EventRecord,
    EvidenceManifest,
    IntakeRecord,
    LockRecord,
    ProjectRecord,
    ResultEnvelope,
    StateRecord,
)

SCHEMA_MODELS = {
    "approval": ApprovalRecord,
    "event": EventRecord,
    "evidence-manifest": EvidenceManifest,
    "execution-lock": LockRecord,
    "intake": IntakeRecord,
    "project": ProjectRecord,
    "result": ResultEnvelope,
    "state": StateRecord,
}


def export_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in sorted(SCHEMA_MODELS):
        path = output_dir / f"{name}.schema.json"
        payload = SCHEMA_MODELS[name].model_json_schema(mode="validation")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    export_schemas(Path("schemas"))
```

- [ ] **Step 4: Export the public schemas and add a check command**

Run: `python -m product_factory.contracts.export`

Add this line under the existing `[project.scripts]` table in `pyproject.toml`:

```toml
product-factory-export-schemas = "product_factory.contracts.export:main"
```

Refactor `export.py` so the script points to:

```python
def main() -> None:
    export_schemas(Path("schemas"))
```

- [ ] **Step 5: Verify schemas are reproducible**

Run: `python -m pytest tests/unit/test_schema_export.py -q && git diff --exit-code -- schemas`

Expected: test passes and a second export produces no diff.

- [ ] **Step 6: Commit generated schemas**

```bash
git add pyproject.toml src/product_factory/contracts/export.py schemas tests/unit/test_schema_export.py
git commit -m "feat: publish deterministic protocol schemas"
```

---

### Task 3: Safe Filesystem Primitives and Project Repository

**Files:**
- Create: `src/product_factory/storage/__init__.py`
- Create: `src/product_factory/storage/paths.py`
- Create: `src/product_factory/storage/files.py`
- Create: `src/product_factory/storage/repository.py`
- Test: `tests/storage/test_files.py`
- Test: `tests/storage/test_repository.py`

**Interfaces:**
- Consumes: `ProjectRecord`, `IntakeRecord`, `StateRecord`, `ApprovalRecord`, `EventRecord`, `EvidenceManifest`.
- Produces: `ProjectPaths`; `atomic_write_json`, `atomic_write_yaml`, `append_jsonl`; `ProjectRepository` typed load/save methods.

- [ ] **Step 1: Write failing tests for boundary checks and atomic state revisions**

```python
# tests/storage/test_files.py
import json
from pathlib import Path

import pytest

from product_factory.storage.files import atomic_write_json, contained_path


def test_atomic_json_leaves_complete_document(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"revision": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"revision": 1}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_contained_path_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside project root"):
        contained_path(tmp_path, "../secret.txt")
```

```python
# tests/storage/test_repository.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

from product_factory.contracts.models import CompletionLevel, CurrentStage, StateRecord, WorkflowState
from product_factory.errors import FactoryError
from product_factory.storage.repository import ProjectRepository


def make_state(revision: int) -> StateRecord:
    return StateRecord(
        schema_version="1.0",
        project_id="demo",
        revision=revision,
        workflow_state=WorkflowState.INITIALIZED,
        current_stage=CurrentStage(id="stage-01", sequence=1, completion_level=CompletionLevel.NONE),
        updated_at=datetime.now(timezone.utc),
    )


def test_save_state_rejects_stale_expected_revision(tmp_path: Path) -> None:
    repo = ProjectRepository(tmp_path)
    repo.paths.metadata.mkdir(parents=True)
    repo.write_initial_state(make_state(0))
    with pytest.raises(FactoryError) as caught:
        repo.save_state(make_state(2), expected_revision=1)
    assert caught.value.code == "revision_conflict"
```

- [ ] **Step 2: Run the storage tests and confirm imports fail**

Run: `python -m pytest tests/storage/test_files.py tests/storage/test_repository.py -q`

Expected: collection fails because storage modules are absent.

- [ ] **Step 3: Implement contained paths and atomic writes**

```python
# src/product_factory/storage/files.py
import json
import os
import uuid
from pathlib import Path
from typing import Any

import yaml


def contained_path(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError("path is outside project root")
    return candidate


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected mapping in {path}")
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
    return records
```

- [ ] **Step 4: Implement typed project paths**

```python
# src/product_factory/storage/paths.py
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    root: Path

    @property
    def metadata(self) -> Path: return self.root / ".product-factory"
    @property
    def project(self) -> Path: return self.metadata / "project.yaml"
    @property
    def intake(self) -> Path: return self.metadata / "intake.yaml"
    @property
    def state(self) -> Path: return self.metadata / "state.json"
    @property
    def approvals(self) -> Path: return self.metadata / "approvals.jsonl"
    @property
    def events(self) -> Path: return self.metadata / "events.jsonl"
    @property
    def lock(self) -> Path: return self.metadata / "execution-lock.json"
    @property
    def evidence(self) -> Path: return self.metadata / "evidence"
```

- [ ] **Step 5: Implement repository revision enforcement**

`ProjectRepository` must parse JSON/YAML through the Task 1 models, create empty JSONL files during initialization, and use this exact state mutation contract:

```python
class ProjectRepository:
    def __init__(self, root: Path):
        self.paths = ProjectPaths(root.resolve())

    def load_state(self) -> StateRecord:
        return StateRecord.model_validate_json(self.paths.state.read_text(encoding="utf-8"))

    def write_initial_state(self, state: StateRecord) -> None:
        if self.paths.state.exists():
            raise FactoryError("project_exists", ErrorCategory.POLICY_BLOCKED, "项目已经初始化", "init", False, "使用新目录")
        atomic_write_json(self.paths.state, state.model_dump(mode="json"))

    def save_state(self, next_state: StateRecord, expected_revision: int) -> StateRecord:
        current = self.load_state()
        if current.revision != expected_revision or next_state.revision != expected_revision + 1:
            raise FactoryError(
                "revision_conflict", ErrorCategory.ENVIRONMENT_BLOCKED, "状态已被其他会话修改",
                "save_state", True, "重新运行 status 或 resume",
                {"expected": expected_revision, "actual": current.revision},
            )
        atomic_write_json(self.paths.state, next_state.model_dump(mode="json"))
        return next_state
```

Add these exact typed methods to `ProjectRepository`:

```python
def load_project(self) -> ProjectRecord:
    return ProjectRecord.model_validate(load_yaml(self.paths.project))

def load_intake(self) -> IntakeRecord:
    return IntakeRecord.model_validate(load_yaml(self.paths.intake))

def save_project(self, record: ProjectRecord) -> None:
    atomic_write_yaml(self.paths.project, record.model_dump(mode="json"))

def save_intake(self, record: IntakeRecord) -> None:
    atomic_write_yaml(self.paths.intake, record.model_dump(mode="json"))

def append_approval(self, record: ApprovalRecord) -> None:
    append_jsonl(self.paths.approvals, record.model_dump(mode="json"))

def read_approvals(self) -> list[ApprovalRecord]:
    return [ApprovalRecord.model_validate(item) for item in read_jsonl(self.paths.approvals)]

def append_event(self, record: EventRecord) -> None:
    append_jsonl(self.paths.events, record.model_dump(mode="json"))

def read_events(self) -> list[EventRecord]:
    return [EventRecord.model_validate(item) for item in read_jsonl(self.paths.events)]

def evidence_path(self, stage_id: str, evidence_id: str) -> Path:
    return contained_path(self.paths.evidence, f"{stage_id}/{evidence_id}/manifest.json")

def save_evidence(self, record: EvidenceManifest) -> Path:
    path = self.evidence_path(record.stage_id, record.evidence_id)
    if path.exists():
        raise FactoryError("evidence_exists", ErrorCategory.POLICY_BLOCKED, "证据 ID 已存在", "record_evidence", False, "使用新的 evidence_id")
    atomic_write_json(path, record.model_dump(mode="json"))
    return path

def load_evidence(self, stage_id: str, evidence_id: str) -> EvidenceManifest:
    return EvidenceManifest.model_validate(load_json(self.evidence_path(stage_id, evidence_id)))
```

- [ ] **Step 6: Run storage tests**

Run: `python -m pytest tests/storage -q`

Expected: all storage tests pass.

- [ ] **Step 7: Commit storage primitives**

```bash
git add src/product_factory/storage tests/storage
git commit -m "feat: add atomic project storage"
```

---

### Task 4: Cross-Platform Lease Lock

**Files:**
- Create: `src/product_factory/storage/locks.py`
- Test: `tests/storage/test_locks.py`

**Interfaces:**
- Consumes: `LockRecord`, `LockOwner`, `ProjectPaths`, `FactoryError`.
- Produces: `LockManager.acquire`, `status`, `heartbeat`, `release`, and `takeover`.

- [ ] **Step 1: Write failing lease lifecycle tests with an injected clock**

```python
# tests/storage/test_locks.py
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from product_factory.contracts.models import LockOwner
from product_factory.errors import FactoryError
from product_factory.storage.locks import LockManager


def test_second_writer_is_blocked_until_lease_expires(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    clock = lambda: now
    manager = LockManager(tmp_path, now_fn=clock)
    first = manager.acquire(LockOwner(tool="codex", session_id="a", pid=1, host="mac"), 0, timedelta(minutes=5))
    with pytest.raises(FactoryError) as caught:
        manager.acquire(LockOwner(tool="pi", session_id="b", pid=2, host="mac"), 0, timedelta(minutes=5))
    assert caught.value.code == "lock_held"
    assert manager.status().lock_id == first.lock_id


def test_takeover_requires_expired_lease_and_reason(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 20, tzinfo=timezone.utc)]
    manager = LockManager(tmp_path, now_fn=lambda: current[0])
    old = manager.acquire(LockOwner(tool="codex", session_id="a", pid=1, host="mac"), 0, timedelta(seconds=1))
    current[0] += timedelta(seconds=2)
    new = manager.takeover(old.lock_id, LockOwner(tool="pi", session_id="b", pid=2, host="mac"), 0, "previous session ended", timedelta(minutes=5))
    assert new.lock_id != old.lock_id
```

- [ ] **Step 2: Run the tests and verify `LockManager` is missing**

Run: `python -m pytest tests/storage/test_locks.py -q`

Expected: collection fails on the missing module.

- [ ] **Step 3: Implement exclusive creation and lease checks**

Use `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)` for acquisition. Serialize the complete `LockRecord`, call `os.fsync`, and delete a partial file if serialization fails. Implement:

```python
class LockManager:
    def __init__(self, root: Path, now_fn: Callable[[], datetime] | None = None):
        self.path = ProjectPaths(root.resolve()).lock
        self.now = now_fn or (lambda: datetime.now(timezone.utc))

    def require(self, lock_id: str, expected_revision: int) -> LockRecord:
        record = self.status()
        if record is None or record.lock_id != lock_id:
            raise FactoryError("lock_required", ErrorCategory.ENVIRONMENT_BLOCKED, "需要有效执行锁", "lock", True, "先运行 lock acquire")
        if record.lease_expires_at <= self.now():
            raise FactoryError("lock_expired", ErrorCategory.ENVIRONMENT_BLOCKED, "执行锁已过期", "lock", True, "重新获取或显式接管")
        if record.state_revision != expected_revision:
            raise FactoryError("lock_revision_mismatch", ErrorCategory.ENVIRONMENT_BLOCKED, "执行锁绑定了旧状态", "lock", True, "释放后重新获取")
        return record
```

`heartbeat` atomically replaces the lock only for its owner and extends the lease. `release` only unlinks a matching lock ID. `takeover` requires the old ID, an expired lease, a non-empty reason, and uses exclusive creation after removing the expired record; return both the new lock and takeover details so Task 6 can append an event.

- [ ] **Step 4: Add tests for heartbeat, wrong-owner release, and active takeover rejection**

Add assertions for these exact error codes: `lock_owner_mismatch`, `lock_active`, and `lock_expired`.

- [ ] **Step 5: Run all lock tests**

Run: `python -m pytest tests/storage/test_locks.py -q`

Expected: all tests pass without `fcntl`, `msvcrt`, or platform-specific locking modules.

- [ ] **Step 6: Commit lease locking**

```bash
git add src/product_factory/storage/locks.py tests/storage/test_locks.py
git commit -m "feat: enforce single-writer lease locks"
```

---

### Task 5: Pure Workflow, Approval, and Evidence Rules

**Files:**
- Create: `src/product_factory/domain/__init__.py`
- Create: `src/product_factory/domain/states.py`
- Create: `src/product_factory/domain/approvals.py`
- Create: `src/product_factory/domain/evidence.py`
- Test: `tests/unit/test_transitions.py`
- Test: `tests/unit/test_approval_rules.py`
- Test: `tests/unit/test_evidence_rules.py`

**Interfaces:**
- Consumes: contract enums and records.
- Produces: `require_transition`, `require_exact_approval`, and `evaluate_evidence` pure functions.

- [ ] **Step 1: Write failing transition tests**

```python
# tests/unit/test_transitions.py
import pytest

from product_factory.contracts.models import CompletionLevel, WorkflowState
from product_factory.errors import FactoryError
from product_factory.domain.states import require_transition


def test_cannot_skip_adaptation_approval() -> None:
    with pytest.raises(FactoryError) as caught:
        require_transition(
            WorkflowState.INPUTS_CHECKED,
            WorkflowState.STAGE_DEVELOPMENT,
            CompletionLevel.NONE,
            has_approval=False,
            has_valid_evidence=False,
        )
    assert caught.value.code == "transition_not_allowed"


def test_later_protocol_state_is_recognized_but_unsupported() -> None:
    with pytest.raises(FactoryError) as caught:
        require_transition(
            WorkflowState.NEXT_STAGE_OR_FRONTEND,
            WorkflowState.RELEASE_READY,
            CompletionLevel.HUMAN_ACCEPTED,
            has_approval=True,
            has_valid_evidence=True,
        )
    assert caught.value.code == "unsupported_transition"
```

- [ ] **Step 2: Implement the exact milestone-one transition table**

```python
# src/product_factory/domain/states.py
from dataclasses import dataclass

from product_factory.contracts.models import CompletionLevel, WorkflowState
from product_factory.errors import ErrorCategory, FactoryError


@dataclass(frozen=True, slots=True)
class TransitionRule:
    required_completion: CompletionLevel
    requires_approval: bool = False
    requires_evidence: bool = False


RULES = {
    (WorkflowState.INITIALIZED, WorkflowState.INPUTS_CHECKED): TransitionRule(CompletionLevel.NONE),
    (WorkflowState.INPUTS_CHECKED, WorkflowState.ADAPTATION_PENDING_APPROVAL): TransitionRule(CompletionLevel.NONE),
    (WorkflowState.ADAPTATION_PENDING_APPROVAL, WorkflowState.STAGE_DEVELOPMENT): TransitionRule(CompletionLevel.NONE, requires_approval=True),
    (WorkflowState.STAGE_DEVELOPMENT, WorkflowState.SYSTEM_VERIFICATION): TransitionRule(CompletionLevel.NONE),
    (WorkflowState.SYSTEM_VERIFICATION, WorkflowState.HUMAN_ACCEPTANCE_PENDING): TransitionRule(CompletionLevel.SYSTEM_VERIFIED, requires_evidence=True),
    (WorkflowState.HUMAN_ACCEPTANCE_PENDING, WorkflowState.NEXT_STAGE_OR_FRONTEND): TransitionRule(CompletionLevel.SYSTEM_VERIFIED, requires_approval=True, requires_evidence=True),
}


def require_transition(current, target, completion, *, has_approval, has_valid_evidence) -> TransitionRule:
    if current is WorkflowState.NEXT_STAGE_OR_FRONTEND:
        raise FactoryError("unsupported_transition", ErrorCategory.POLICY_BLOCKED, "后续流程尚未实现", "transition", False, "等待后续里程碑")
    rule = RULES.get((current, target))
    if rule is None:
        raise FactoryError("transition_not_allowed", ErrorCategory.POLICY_BLOCKED, "不允许该状态转换", "transition", False, "运行 status 查看允许动作")
    if completion.value != rule.required_completion.value:
        raise FactoryError("completion_mismatch", ErrorCategory.IMPLEMENTATION_FAILED, "阶段完成级别不满足门禁", "transition", True, "完成当前验证步骤")
    if rule.requires_approval and not has_approval:
        raise FactoryError("approval_missing", ErrorCategory.APPROVAL_REQUIRED, "缺少匹配审批", "transition", True, "请求并完成审批")
    if rule.requires_evidence and not has_valid_evidence:
        raise FactoryError("evidence_missing", ErrorCategory.IMPLEMENTATION_FAILED, "缺少当前有效证据", "transition", True, "登记并验证证据")
    return rule
```

- [ ] **Step 3: Write and implement exact approval matching**

Test that a wrong statement, request ID, revision, gate, or scope fails with `approval_mismatch`, and a matching record passes. Implement:

```python
APPROVAL_STATEMENT = "验收通过，批准进入下一阶段。"


def require_exact_approval(record: ApprovalRecord, waiting: WaitingOn, current_revision: int) -> None:
    matches = (
        record.statement == APPROVAL_STATEMENT
        and record.request_id == waiting.request_id
        and record.gate_type is waiting.gate_type
        and record.scope == waiting.scope
        and record.state_revision == current_revision
        and record.consumed_by_revision == current_revision + 1
    )
    if not matches:
        raise FactoryError("approval_mismatch", ErrorCategory.APPROVAL_REQUIRED, "审批与当前门禁不匹配", "approve", True, "重新请求当前门禁审批")
```

- [ ] **Step 4: Write and implement pure evidence evaluation**

`evaluate_evidence(manifest, project, state, current_source_digest)` returns a list of stable reason codes. It must add:

```python
def evaluate_evidence(manifest, project, state, current_source_digest: str) -> list[str]:
    reasons: list[str] = []
    stage = next(item for item in project.stage_plan if item.id == state.current_stage.id)
    if manifest.stage_id != stage.id: reasons.append("stage_mismatch")
    if manifest.state_revision > state.revision: reasons.append("future_revision")
    if manifest.factory_version != project.factory_version: reasons.append("factory_version_mismatch")
    if manifest.prd_sha256 != project.prd.sha256: reasons.append("prd_changed")
    if manifest.source_digest != current_source_digest: reasons.append("source_changed")
    if any(check.exit_status != 0 for check in manifest.checks): reasons.append("check_failed")
    if stage.requires_real_model and not any(check.mode == "real" and check.exit_status == 0 for check in manifest.checks): reasons.append("real_model_missing")
    if any(issue.blocking for issue in manifest.known_issues): reasons.append("blocking_issue")
    if not manifest.ready_for_human_acceptance: reasons.append("not_ready")
    return reasons
```

- [ ] **Step 5: Run all domain tests**

Run: `python -m pytest tests/unit/test_transitions.py tests/unit/test_approval_rules.py tests/unit/test_evidence_rules.py -q`

Expected: all domain tests pass without filesystem access.

- [ ] **Step 6: Commit pure rules**

```bash
git add src/product_factory/domain tests/unit/test_transitions.py tests/unit/test_approval_rules.py tests/unit/test_evidence_rules.py
git commit -m "feat: enforce workflow gate rules"
```

---

### Task 6: Project Initialization and Input Validation

**Files:**
- Create: `src/product_factory/services/__init__.py`
- Create: `src/product_factory/services/mutations.py`
- Create: `src/product_factory/services/initialize.py`
- Create: `templates/intake.yaml`
- Test: `tests/integration/test_initialize.py`

**Interfaces:**
- Consumes: `ProjectRepository`, contract records, handbook `manifest.yaml`.
- Produces: `commit_state_change(...) -> StateRecord`; `initialize_project(...) -> StateRecord`; `check_inputs(...) -> StateRecord`.

- [ ] **Step 1: Write a failing initialization test**

```python
# tests/integration/test_initialize.py
from pathlib import Path

from product_factory.contracts.models import WorkflowState
from product_factory.services.initialize import initialize_project


def test_initialize_copies_baseline_and_creates_protocol_files(tmp_path: Path) -> None:
    prd = tmp_path / "source-prd.md"
    prd.write_text("# Confirmed PRD\n", encoding="utf-8")
    target = tmp_path / "new-product"
    state = initialize_project(
        target=target,
        project_id="demo-web",
        name="Demo Web",
        prd_source=prd,
        intake_source=Path("examples/minimal-project/intake.yaml"),
        stage_specs=[("stage-01", "Core flow", False)],
        factory_root=Path.cwd(),
    )
    assert state.workflow_state is WorkflowState.INITIALIZED
    assert (target / "inputs/PRD.md").read_text(encoding="utf-8") == "# Confirmed PRD\n"
    assert (target / ".product-factory/project.yaml").is_file()
    assert (target / ".product-factory/approvals.jsonl").read_text(encoding="utf-8") == ""
```

- [ ] **Step 2: Run the test and verify the service is absent**

Run: `python -m pytest tests/integration/test_initialize.py -q`

Expected: collection fails on the missing initialize service.

- [ ] **Step 3: Implement initialization without overwriting existing targets**

`initialize_project` must reject a non-empty target with `project_exists`, create `inputs/assets`, `.product-factory/evidence`, `docs`, `backend`, and `frontend`, copy the PRD and intake declaration, create an empty `constraints.md` when none is supplied, load `references/handbooks/manifest.yaml`, calculate SHA-256 values, write `project.yaml`, `intake.yaml`, and revision-zero `state.json`, and create empty approvals/events files.

Use this helper for baseline copies:

```python
def copy_baseline(source: Path, destination: Path) -> str:
    content = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return hashlib.sha256(content).hexdigest()
```

Use UUIDv4 strings for event and request identities; use `datetime.now(timezone.utc)` and Pydantic JSON mode for timestamps.

- [ ] **Step 4: Write failing input-check tests**

Add tests proving these codes: `prd_not_confirmed`, `input_requirement_missing`, `prd_digest_mismatch`, and `inputs_valid`. A `NOT_APPLICABLE` declaration with a reason is valid.

- [ ] **Step 5: Implement the shared state-first audited mutation helper**

```python
# src/product_factory/services/mutations.py
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
```

Add a test that replaces `repo.append_event` with a function raising `OSError`, then asserts `state.json` contains the preallocated missing `last_event_id`. Task 9 will prove explicit repair.

- [ ] **Step 6: Implement deterministic input validation and state advance**

```python
def collect_input_errors(repo: ProjectRepository) -> list[str]:
    project = repo.load_project()
    intake = repo.load_intake()
    errors: list[str] = []
    if intake.project_id != project.project_id: errors.append("project_id_mismatch")
    if not intake.prd_confirmed: errors.append("prd_not_confirmed")
    if hashlib.sha256((repo.paths.root / project.prd.path).read_bytes()).hexdigest() != project.prd.sha256:
        errors.append("prd_digest_mismatch")
    for key, declaration in intake.requirements.items():
        if declaration.status is RequirementStatus.MISSING:
            errors.append(f"input_requirement_missing:{key}")
    return errors
```

`check_inputs` requires a valid lock and expected revision zero. If errors exist, raise `FactoryError(code=errors[0], category=INPUT_REQUIRED, details={"errors": errors})`. Otherwise build revision one with `inputs_checked` and call `commit_state_change(repo, state, next_state, "inputs_checked", {"errors": []})`.

- [ ] **Step 7: Run initialization integration tests**

Run: `python -m pytest tests/integration/test_initialize.py -q`

Expected: initialization and all input-check cases pass.

- [ ] **Step 8: Commit initialization**

```bash
git add src/product_factory/services/mutations.py src/product_factory/services/initialize.py templates/intake.yaml tests/integration/test_initialize.py
git commit -m "feat: initialize and validate project inputs"
```

---

### Task 7: Workflow Service and Auditable Approvals

**Files:**
- Create: `src/product_factory/services/workflow.py`
- Test: `tests/integration/test_workflow.py`

**Interfaces:**
- Consumes: repository, lock manager, domain transition and approval functions.
- Produces: `WorkflowService.request_approval`, `approve`, `start_verification`, and `mark_system_verified`.

- [ ] **Step 1: Write a failing adaptation-gate integration test**

The test must initialize and check inputs, create `docs/technical-adaptation.md`, acquire a lock bound to revision one, then assert:

```python
pending = service.request_approval(
    gate=GateType.TECHNICAL_ADAPTATION,
    artifact=Path("docs/technical-adaptation.md"),
    lock_id=lock.lock_id,
    expected_revision=1,
)
assert pending.workflow_state is WorkflowState.ADAPTATION_PENDING_APPROVAL
assert pending.waiting_on.gate_type is GateType.TECHNICAL_ADAPTATION

with pytest.raises(FactoryError) as caught:
    service.approve("wrong words", "owner", lock.lock_id, expected_revision=2)
assert caught.value.code == "approval_statement_mismatch"
```

- [ ] **Step 2: Run the test and verify workflow service is missing**

Run: `python -m pytest tests/integration/test_workflow.py -q`

Expected: collection fails on the missing service.

- [ ] **Step 3: Implement approval request creation**

`request_approval` must:

1. Require the lock and expected revision.
2. For technical adaptation, require `inputs_checked` and a project-contained artifact.
3. For stage acceptance, require `system_verification`, `system_verified`, and `last_valid_evidence_id`.
4. Store `scope` as `{"artifact_path": relative_path, "artifact_sha256": digest}` for adaptation or `{"stage_id": id, "evidence_id": id}` for stage acceptance.
5. Set a UUID request ID in `waiting_on`.
6. Advance to `adaptation_pending_approval` or `human_acceptance_pending` and append `approval_requested`.

- [ ] **Step 4: Implement exact interactive approval consumption**

`approve(statement, actor, lock_id, expected_revision)` must reject any statement other than `APPROVAL_STATEMENT`, append the complete `ApprovalRecord` before state mutation, preallocate the event ID, then advance:

```python
target = {
    GateType.TECHNICAL_ADAPTATION: WorkflowState.STAGE_DEVELOPMENT,
    GateType.STAGE_ACCEPTANCE: WorkflowState.NEXT_STAGE_OR_FRONTEND,
}[state.waiting_on.gate_type]
completion = (
    CompletionLevel.NONE
    if target is WorkflowState.STAGE_DEVELOPMENT
    else CompletionLevel.HUMAN_ACCEPTED
)
```

The approval uses `source="interactive_cli"`, `state_revision=expected_revision`, and `consumed_by_revision=expected_revision + 1`. Clear `waiting_on`; preserve the current evidence ID for stage acceptance.

- [ ] **Step 5: Implement the development-to-verification transition**

`start_verification` requires `stage_development`, no approval, a valid lock, and expected revision. It sets `workflow_state=system_verification`, `completion_level=implemented`, increments the revision, and appends `implementation_recorded`.

`mark_system_verified` is called only by Task 8 after evidence validation. It keeps `workflow_state=system_verification`, changes completion to `system_verified`, stores the evidence ID, increments the revision, and appends `system_verified`.

- [ ] **Step 6: Add failure tests for approval replay and scope mismatch**

After a successful approval, call `approve` again and assert `approval_not_pending`. Modify the adaptation artifact after requesting approval and assert `approval_scope_changed` before consuming the approval.

- [ ] **Step 7: Run workflow integration tests**

Run: `python -m pytest tests/integration/test_workflow.py -q`

Expected: all transition, replay, and scope-binding cases pass.

- [ ] **Step 8: Commit workflow orchestration**

```bash
git add src/product_factory/services/workflow.py tests/integration/test_workflow.py
git commit -m "feat: add auditable approval workflow"
```

---

### Task 8: Evidence Digests, Immutable Recording, and Verification

**Files:**
- Create: `src/product_factory/services/evidence.py`
- Test: `tests/integration/test_evidence_service.py`

**Interfaces:**
- Consumes: evidence domain rules, repository, lock manager, workflow `mark_system_verified`.
- Produces: `compute_source_digest`, `record_evidence`, `verify_stage`.

- [ ] **Step 1: Write a failing digest test**

```python
def test_digest_ignores_factory_state_but_detects_source_change(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/app.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".product-factory").mkdir()
    (tmp_path / ".product-factory/state.json").write_text("{}", encoding="utf-8")
    first = compute_source_digest(tmp_path, [])
    (tmp_path / ".product-factory/state.json").write_text('{"revision": 2}', encoding="utf-8")
    assert compute_source_digest(tmp_path, []) == first
    (tmp_path / "backend/app.py").write_text("value = 2\n", encoding="utf-8")
    assert compute_source_digest(tmp_path, []) != first
```

- [ ] **Step 2: Implement stable source hashing**

Walk files in sorted POSIX relative-path order. Exclude any path beginning with `.git/`, `.product-factory/`, `.venv/`, `build/`, `dist/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`; exclude `.env`, names beginning `.env.`, and configured glob patterns. Hash `relative_path.encode()`, a NUL byte, file bytes, and another NUL byte for each included file.

- [ ] **Step 3: Write a failing immutable evidence recording test**

Record evidence ID `evidence-01`, then record the same ID again and assert `evidence_exists`. Assert the stored path is `.product-factory/evidence/stage-01/evidence-01/manifest.json`.

- [ ] **Step 4: Implement recording with recomputed identity fields**

`record_evidence` accepts an authoring YAML/JSON file, validates it, but overwrites these fields from current facts before saving: `stage_id`, `state_revision`, `factory_version`, `prd_sha256`, and `source_digest`. It requires `system_verification`, completion `implemented`, a valid lock, and expected revision. It never overwrites an existing evidence directory.

- [ ] **Step 5: Write verification gate tests**

Cover `source_changed`, `check_failed`, `real_model_missing`, `blocking_issue`, `not_ready`, and a fully valid non-model stage. After a valid result, assert state completion is `system_verified` and `last_valid_evidence_id` is stored.

- [ ] **Step 6: Implement verification**

```python
def verify_stage(root: Path, evidence_id: str, lock_id: str, expected_revision: int) -> StateRecord:
    repo = ProjectRepository(root)
    state = repo.load_state()
    project = repo.load_project()
    LockManager(root).require(lock_id, expected_revision)
    manifest = repo.load_evidence(state.current_stage.id, evidence_id)
    reasons = evaluate_evidence(manifest, project, state, compute_source_digest(root, project.source_excludes))
    if reasons:
        raise FactoryError("evidence_invalid", ErrorCategory.IMPLEMENTATION_FAILED, "系统验证证据未通过", "verify_stage", True, "修复后重新登记证据", {"reasons": reasons})
    return WorkflowService(root).mark_system_verified(evidence_id, lock_id, expected_revision)
```

- [ ] **Step 7: Run evidence integration tests**

Run: `python -m pytest tests/integration/test_evidence_service.py -q`

Expected: all digest, immutable-history, real-model, and gate cases pass.

- [ ] **Step 8: Commit evidence services**

```bash
git add src/product_factory/services/evidence.py tests/integration/test_evidence_service.py
git commit -m "feat: validate immutable stage evidence"
```

---

### Task 9: Read-Only Validation, Recovery Summary, and Explicit Audit Repair

**Files:**
- Create: `src/product_factory/services/recovery.py`
- Test: `tests/integration/test_recovery.py`

**Interfaces:**
- Consumes: repository, lock manager, source digest, all contract models.
- Produces: `ValidationReport`; `RecoverySummary`; `validate_project(root: Path) -> ValidationReport`; `resume_project(root: Path) -> RecoverySummary`; `repair_audit(root: Path, lock_id: str, expected_revision: int) -> EventRecord`.

- [ ] **Step 1: Write failing read-only recovery tests**

```python
def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def test_validate_and_resume_do_not_write(initialized_project: Path) -> None:
    before = snapshot(initialized_project)
    report = validate_project(initialized_project)
    summary = resume_project(initialized_project)
    assert report.valid is True
    assert summary.workflow_state == "initialized"
    assert summary.revision == 0
    assert snapshot(initialized_project) == before
```

- [ ] **Step 2: Implement cross-record validation**

Implement the reports and cross-record checks with these exact shapes:

```python
@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    findings: list[str]


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    workflow_state: str
    revision: int
    lock_status: str
    evidence_status: str
    waiting_on: dict | None
    audit_status: str
    next_command: str


def validate_project(root: Path) -> ValidationReport:
    repo = ProjectRepository(root)
    project, intake, state = repo.load_project(), repo.load_intake(), repo.load_state()
    approvals, events = repo.read_approvals(), repo.read_events()
    findings: list[str] = []
    if {project.project_id, intake.project_id, state.project_id} != {project.project_id}:
        findings.append("project_id_mismatch")
    if state.current_stage.id not in {item.id for item in project.stage_plan}:
        findings.append("unknown_current_stage")
    consumed = [item.consumed_by_revision for item in approvals]
    if len(consumed) != len(set(consumed)):
        findings.append("duplicate_approval_consumption")
    event_ids = {item.event_id for item in events}
    if state.last_event_id is not None and state.last_event_id not in event_ids:
        findings.append("missing_referenced_event")
    if state.last_valid_evidence_id is not None:
        try:
            repo.load_evidence(state.current_stage.id, state.last_valid_evidence_id)
        except (FileNotFoundError, ValueError):
            findings.append("missing_referenced_evidence")
    return ValidationReport(valid=not findings, findings=findings)
```

- [ ] **Step 3: Implement safe next-command selection**

```python
NEXT_COMMAND = {
    WorkflowState.INITIALIZED: "product-factory check-inputs",
    WorkflowState.INPUTS_CHECKED: "product-factory request-approval --gate technical_adaptation --artifact docs/technical-adaptation.md",
    WorkflowState.ADAPTATION_PENDING_APPROVAL: "product-factory approve",
    WorkflowState.STAGE_DEVELOPMENT: "product-factory transition --to system_verification",
    WorkflowState.SYSTEM_VERIFICATION: "product-factory record-evidence --manifest evidence-authoring.yaml",
    WorkflowState.HUMAN_ACCEPTANCE_PENDING: "product-factory approve",
    WorkflowState.NEXT_STAGE_OR_FRONTEND: "等待下一里程碑设计",
}
```

If the referenced event is missing, override `next_command` with `product-factory repair-audit`. If an active lock belongs to another session, report read-only status and do not recommend a mutation command.

- [ ] **Step 4: Write failing audit-gap tests**

```python
def test_audit_gap_requires_explicit_repair(project_with_missing_event: Path) -> None:
    before = snapshot(project_with_missing_event)
    summary = resume_project(project_with_missing_event)
    assert summary.audit_status == "missing_referenced_event"
    assert summary.next_command == "product-factory repair-audit"
    assert snapshot(project_with_missing_event) == before
    with pytest.raises(FactoryError) as caught:
        repair_audit(project_with_missing_event, "missing-lock", expected_revision=1)
    assert caught.value.code == "lock_required"
```

- [ ] **Step 5: Implement audit repair without business-state mutation**

```python
def repair_audit(root: Path, lock_id: str, expected_revision: int) -> EventRecord:
    repo = ProjectRepository(root)
    before = repo.paths.state.read_bytes()
    state = repo.load_state()
    LockManager(root).require(lock_id, expected_revision)
    if state.last_event_id is None or state.last_event_id in {item.event_id for item in repo.read_events()}:
        raise FactoryError("audit_repair_not_needed", ErrorCategory.POLICY_BLOCKED, "没有可修复的审计缺口", "repair_audit", False, "运行 resume")
    event = EventRecord(
        schema_version="1.0",
        event_id=state.last_event_id,
        event_type="recovered_missing_event",
        project_id=state.project_id,
        before_revision=state.revision,
        after_revision=state.revision,
        created_at=datetime.now(timezone.utc),
        details={"workflow_state": state.workflow_state.value},
    )
    repo.append_event(event)
    if repo.paths.state.read_bytes() != before:
        raise RuntimeError("repair_audit changed state.json")
    return event
```

- [ ] **Step 6: Run recovery tests**

Run: `python -m pytest tests/integration/test_recovery.py -q`

Expected: validation and resume are byte-for-byte read-only; repair only appends one event.

- [ ] **Step 7: Commit recovery behavior**

```bash
git add src/product_factory/services/recovery.py tests/integration/test_recovery.py
git commit -m "feat: add read-only recovery and audit repair"
```

---

### Task 10: Complete CLI Surface and Stable Output

**Files:**
- Create: `src/product_factory/cli/__init__.py`
- Create: `src/product_factory/cli/parser.py`
- Create: `src/product_factory/cli/output.py`
- Create: `src/product_factory/cli/main.py`
- Test: `tests/integration/test_cli.py`

**Interfaces:**
- Consumes: all service APIs and `FactoryError`.
- Produces: installed `product-factory` command, stable JSON envelope, stable exit codes.

- [ ] **Step 1: Write failing CLI smoke tests**

```python
# tests/integration/test_cli.py
import json
import subprocess
import sys


def run_cli(*args: str):
    return subprocess.run([sys.executable, "-m", "product_factory.cli.main", *args], text=True, capture_output=True)


def test_status_missing_project_returns_stable_json(tmp_path) -> None:
    result = run_cli("--json", "status", "--project", str(tmp_path))
    payload = json.loads(result.stdout)
    assert result.returncode == 2
    assert payload.keys() == {"ok", "code", "category", "message", "step", "retryable", "action", "details"}
    assert payload["code"] == "project_not_initialized"
```

- [ ] **Step 2: Build the complete argparse tree**

`parser.py` must define these commands and arguments exactly:

```text
init --project PATH --project-id ID --name NAME --prd PATH --intake PATH --stage ID:NAME[:requires_real_model]
check-inputs --project PATH --lock-id ID --expected-revision N
status --project PATH
request-approval --project PATH --gate {technical_adaptation,stage_acceptance} [--artifact PATH] --lock-id ID --expected-revision N
approve --project PATH --actor NAME --lock-id ID --expected-revision N
record-evidence --project PATH --manifest PATH --lock-id ID --expected-revision N
verify-stage --project PATH --evidence-id ID --lock-id ID --expected-revision N
transition --project PATH --to system_verification --lock-id ID --expected-revision N
lock acquire --project PATH --tool NAME --session-id ID --lease-seconds N
lock status --project PATH
lock heartbeat --project PATH --lock-id ID --lease-seconds N
lock release --project PATH --lock-id ID
lock takeover --project PATH --old-lock-id ID --tool NAME --session-id ID --reason TEXT --lease-seconds N
resume --project PATH
validate --project PATH
repair-audit --project PATH --lock-id ID --expected-revision N
```

Global `--json` must work before the subcommand. Do not add secret-bearing flags.

- [ ] **Step 3: Implement output rendering**

```python
def success(code: str, message: str, action: str, details: dict) -> ResultEnvelope:
    return ResultEnvelope(ok=True, code=code, category=None, message=message, step="complete", retryable=False, action=action, details=details)


def failure(error: FactoryError) -> ResultEnvelope:
    return ResultEnvelope(ok=False, code=error.code, category=error.category.value, message=error.message, step=error.step, retryable=error.retryable, action=error.action, details=error.details)


def render(envelope: ResultEnvelope, json_mode: bool) -> str:
    if json_mode:
        return envelope.model_dump_json(indent=2)
    prefix = "完成" if envelope.ok else "未完成"
    return f"{prefix}：{envelope.message}\n下一步：{envelope.action}"
```

- [ ] **Step 4: Implement dispatch and interactive approval input**

`approve` must call `input("请输入批准语句：")`; it must not accept the statement as an argument or environment variable. `main(argv=None) -> int` catches only `FactoryError` for expected failures, emits a sanitized `internal_error` envelope for other exceptions, prints to stdout in JSON mode and stderr for human-mode failures, and returns the stable exit code.

- [ ] **Step 5: Add CLI tests for every command family**

Cover init, lock lifecycle, input check, adaptation approval, verification start, evidence recording, evidence verification, stage approval, status, resume, validate, and repair-audit. Use `subprocess.run(..., input=APPROVAL_STATEMENT + "\n")` only for approval tests. Assert no output contains `TEST_SECRET_DO_NOT_PRINT`.

- [ ] **Step 6: Run CLI tests through the installed entry point**

Run: `python -m pip install -e '.[dev]' && python -m pytest tests/integration/test_cli.py -q && product-factory --help`

Expected: all CLI tests pass and help lists every command without secret flags.

- [ ] **Step 7: Commit the CLI**

```bash
git add src/product_factory/cli tests/integration/test_cli.py
git commit -m "feat: expose the product factory CLI"
```

---

### Task 11: Templates, Minimal Example, End-to-End Proof, and Operator Docs

**Files:**
- Create: `templates/technical-adaptation.md`
- Create: `templates/stage-development.md`
- Create: `templates/acceptance.md`
- Create: `templates/evidence-manifest.yaml`
- Create: `examples/minimal-project/PRD.md`
- Create: `examples/minimal-project/constraints.md`
- Create: `examples/minimal-project/intake.yaml`
- Create: `examples/minimal-project/technical-adaptation.md`
- Create: `examples/minimal-project/evidence-manifest.yaml`
- Create: `tests/integration/test_end_to_end.py`
- Create: `README.md`
- Create: `CHANGELOG.md`

**Interfaces:**
- Consumes: installed CLI and all public contracts.
- Produces: a reproducible offline demonstration, user acceptance steps, and milestone documentation.

- [ ] **Step 1: Add templates with required, concrete fields**

The technical adaptation template must contain product shape, selected development path, adopted defaults, triggered modules, deviations, strong-baseline checks, cost/data boundaries, and decisions required. The stage template must contain goal, included/excluded scope, data/state, interfaces, test cases, acceptance steps, risks, and handoff. The acceptance template must contain address, exact input, expected result, failure/recovery check, subjective judgments, and feedback evidence.

The evidence authoring template must be valid against `EvidenceManifest` after the service fills identity fields. Use fixed safe sample commands:

```yaml
schema_version: "1.0"
evidence_id: "evidence-01"
stage_id: "stage-01"
state_revision: 0
factory_version: "0.1.0"
prd_sha256: "0000000000000000000000000000000000000000000000000000000000000000"
source_digest: "0000000000000000000000000000000000000000000000000000000000000000"
runtime_entry: null
checks:
  - name: "offline core check"
    command: "python -c 'print(\"ok\")'"
    started_at: "2026-08-20T00:00:00Z"
    ended_at: "2026-08-20T00:00:01Z"
    exit_status: 0
    summary: "offline check passed"
    mode: "not_applicable"
    artifact_paths: []
known_issues: []
ready_for_human_acceptance: true
```

- [ ] **Step 2: Create the minimal confirmed PRD and intake declaration**

The PRD must explicitly state one target user, one offline core task, one input/output pair, the approval points, V1 scope, acceptance criteria, zero external-call budget, and no sensitive data. The intake YAML must mark all seven categories `present` with exact PRD section references and use project ID `minimal-web`.

- [ ] **Step 3: Write the failing end-to-end test**

The test must run the installed CLI in a temporary directory through this exact sequence:

```text
init
lock acquire
check-inputs
lock release and reacquire at new revision
request-approval technical_adaptation
lock release and reacquire
approve with exact stdin statement
lock release and reacquire
transition to system_verification
lock release and reacquire
record-evidence
verify-stage
lock release and reacquire
request-approval stage_acceptance
lock release and reacquire
approve with exact stdin statement
resume
```

Assert final state `next_stage_or_frontend`, completion `human_accepted`, two approval lines, immutable evidence path, parseable events, and a recovery summary that recommends waiting for the next milestone.

- [ ] **Step 4: Run the end-to-end test and fix only contract-conforming defects**

Run: `python -m pytest tests/integration/test_end_to_end.py -q`

Expected: one full offline workflow passes. If a defect requires changing the approved public contract, stop and amend the spec before changing code.

- [ ] **Step 5: Write README installation and acceptance instructions**

README must include:

1. Product boundary and non-goals.
2. Python 3.11+ environment creation and `pip install -e '.[dev]'`.
3. The exact minimal-example command sequence.
4. Explanation of state, approval, evidence, lock, and recovery files.
5. Human versus `--json` output.
6. Secret-handling prohibition.
7. macOS verified scope and Windows/Linux qualification.
8. Explicit statement that no GitHub remote, cloud resource, deployment, model call, AI Web adapter, or image adapter exists in milestone one.

- [ ] **Step 6: Write CHANGELOG milestone entry**

Create `CHANGELOG.md` with version `0.1.0`, date `2026-08-20`, the implemented core features, offline verification statement, known limitation that later states are protocol-only, and platform verification scope.

- [ ] **Step 7: Run the full quality gate**

Run:

```bash
python -m product_factory.contracts.export
git diff --exit-code -- schemas
python -m pytest -q
python -m pip check
python -m pytest tests/integration/test_end_to_end.py -q
git diff --check
```

Expected: schemas unchanged, all tests pass, dependency check passes, the example completes end to end, and no whitespace errors appear.

- [ ] **Step 8: Scan for secrets and prohibited external configuration**

Run:

```bash
rg -n -i 'access[_-]?key|secret[_-]?key|api[_-]?key|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY' . \
  -g '!references/handbooks/**' \
  -g '!docs/product/**' \
  -g '!docs/superpowers/**'
git remote -v
```

Expected: only documentation/test assertions about prohibited key names appear; `git remote -v` prints nothing.

- [ ] **Step 9: Commit the verified milestone**

```bash
git add templates examples tests/integration/test_end_to_end.py README.md CHANGELOG.md
git commit -m "feat: complete the offline core milestone"
```

- [ ] **Step 10: Record final verification evidence for handoff**

Run:

```bash
git status --short --branch
git log --oneline --decorate -12
python -m pytest -q
```

Expected: clean `main` branch, focused task commits visible, and the full test suite passing. Report the exact test count and commands; do not claim Windows/Linux verification or any later adapter/deployment capability.
