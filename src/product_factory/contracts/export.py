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
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


def main() -> None:
    export_schemas(Path("schemas"))


if __name__ == "__main__":
    main()
