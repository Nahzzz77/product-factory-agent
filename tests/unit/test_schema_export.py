import json
from pathlib import Path

from product_factory.contracts.export import SCHEMA_MODELS, export_schemas
from product_factory.contracts.models import IntakeRecord


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
