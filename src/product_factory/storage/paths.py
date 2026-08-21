from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    root: Path

    @property
    def metadata(self) -> Path:
        return self.root / ".product-factory"

    @property
    def project(self) -> Path:
        return self.metadata / "project.yaml"

    @property
    def intake(self) -> Path:
        return self.metadata / "intake.yaml"

    @property
    def state(self) -> Path:
        return self.metadata / "state.json"

    @property
    def approvals(self) -> Path:
        return self.metadata / "approvals.jsonl"

    @property
    def events(self) -> Path:
        return self.metadata / "events.jsonl"

    @property
    def lock(self) -> Path:
        return self.metadata / "execution-lock.json"

    @property
    def evidence(self) -> Path:
        return self.metadata / "evidence"
