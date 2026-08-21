import json
import shutil
import subprocess
from pathlib import Path

import pytest

from product_factory.contracts.export import SCHEMA_MODELS, export_schemas
from product_factory.contracts.models import EvidenceManifest, IntakeRecord, StateRecord


def test_export_is_deterministic_and_complete(tmp_path: Path) -> None:
    first = export_schemas(tmp_path / "first")
    second = export_schemas(tmp_path / "second")
    assert [p.name for p in first] == [f"{name}.schema.json" for name in sorted(SCHEMA_MODELS)]
    assert [p.read_bytes() for p in first] == [p.read_bytes() for p in second]


def test_intake_schema_requires_a_nonblank_reason_only_when_not_applicable() -> None:
    declaration = IntakeRecord.model_json_schema(mode="validation")["$defs"]["RequirementDeclaration"]

    assert declaration["allOf"] == [
        {
            "if": {
                "properties": {"status": {"const": "not_applicable"}},
                "required": ["status"],
            },
            "then": {
                "properties": {"reason": {"pattern": "\\S", "type": "string"}},
                "required": ["reason"],
            },
        }
    ]

    exported = json.loads((Path("schemas") / "intake.schema.json").read_text(encoding="utf-8"))
    assert exported["$defs"]["RequirementDeclaration"]["allOf"] == declaration["allOf"]


def test_standard_draft_202012_enforces_portable_evidence_id_rules(tmp_path: Path) -> None:
    """The published schemas, not only Pydantic validators, reject bad IDs."""
    interpreter = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else shutil.which("python3")
    if interpreter is None:
        pytest.skip("no system Python with jsonschema available")
    paths = export_schemas(tmp_path / "schemas")
    evidence_schema = next(path for path in paths if path.name == "evidence-manifest.schema.json")
    state_schema = next(path for path in paths if path.name == "state.schema.json")
    evidence = EvidenceManifest.model_validate({
        "schema_version": "1.0", "evidence_id": "证据-01", "stage_id": "stage-01",
        "state_revision": 4, "factory_version": "test", "prd_sha256": "a" * 64,
        "source_digest": "b" * 64,
        "checks": [{"name": "check", "command": "true", "started_at": "2026-08-20T00:00:00Z",
                    "ended_at": "2026-08-20T00:00:01Z", "exit_status": 0, "summary": "ok", "mode": "mock"}],
        "ready_for_human_acceptance": True,
    }).model_dump(mode="json")
    state = StateRecord.model_validate({
        "schema_version": "1.0", "project_id": "demo", "revision": 0, "workflow_state": "initialized",
        "current_stage": {"id": "stage-01", "sequence": 1, "completion_level": "none"},
        "updated_at": "2026-08-20T00:00:00Z",
    }).model_dump(mode="json")
    invalid = ["COM¹.log", "LPT³", "e\u0301", "é" * 128, "😀" * 64]
    payload = {"schemas": [str(evidence_schema), str(state_schema)], "evidence": evidence, "state": state, "invalid": invalid}
    script = '''
import json, sys
from jsonschema import Draft202012Validator
data = json.load(sys.stdin)
schemas = [json.load(open(path, encoding="utf-8")) for path in data["schemas"]]
for schema in schemas:
    Draft202012Validator.check_schema(schema)
validators = [Draft202012Validator(schema) for schema in schemas]
assert not list(validators[0].iter_errors(data["evidence"]))
assert not list(validators[1].iter_errors(data["state"]))
for value in data["invalid"]:
    evidence = dict(data["evidence"], evidence_id=value)
    state = dict(data["state"], last_valid_evidence_id=value)
    assert list(validators[0].iter_errors(evidence)), value
    assert list(validators[1].iter_errors(state)), value
'''
    result = subprocess.run(
        [interpreter, "-c", script], input=json.dumps(payload, ensure_ascii=False), text=True,
        capture_output=True, check=False,
    )
    if result.returncode and "No module named 'jsonschema'" in result.stderr:
        pytest.skip("system Python lacks jsonschema")
    assert result.returncode == 0, result.stderr
