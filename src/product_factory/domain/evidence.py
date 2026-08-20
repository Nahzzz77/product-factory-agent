"""Pure, ordered evidence invalidation rules."""

from product_factory.contracts.models import EvidenceManifest, ProjectRecord, StateRecord


def evaluate_evidence(
    manifest: EvidenceManifest,
    project: ProjectRecord,
    state: StateRecord,
    current_source_digest: str,
) -> list[str]:
    """Return the stable list of reasons this evidence cannot be reused."""
    reasons: list[str] = []
    stage = next(item for item in project.stage_plan if item.id == state.current_stage.id)
    if manifest.stage_id != stage.id:
        reasons.append("stage_mismatch")
    if manifest.state_revision > state.revision:
        reasons.append("future_revision")
    if manifest.factory_version != project.factory_version:
        reasons.append("factory_version_mismatch")
    if manifest.prd_sha256 != project.prd.sha256:
        reasons.append("prd_changed")
    if manifest.source_digest != current_source_digest:
        reasons.append("source_changed")
    if any(check.exit_status != 0 for check in manifest.checks):
        reasons.append("check_failed")
    if stage.requires_real_model and not any(
        check.mode == "real" and check.exit_status == 0 for check in manifest.checks
    ):
        reasons.append("real_model_missing")
    if any(issue.blocking for issue in manifest.known_issues):
        reasons.append("blocking_issue")
    if not manifest.ready_for_human_acceptance:
        reasons.append("not_ready")
    return reasons
