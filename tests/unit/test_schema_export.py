from pathlib import Path

from product_factory.contracts.export import SCHEMA_MODELS, export_schemas


def test_export_is_deterministic_and_complete(tmp_path: Path) -> None:
    first = export_schemas(tmp_path / "first")
    second = export_schemas(tmp_path / "second")
    assert [p.name for p in first] == [f"{name}.schema.json" for name in sorted(SCHEMA_MODELS)]
    assert [p.read_bytes() for p in first] == [p.read_bytes() for p in second]
