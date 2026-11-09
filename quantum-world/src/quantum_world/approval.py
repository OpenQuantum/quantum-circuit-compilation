"""Operator-only approval. No model-facing tool can call this module."""

from datetime import datetime, timedelta, timezone
import math
from .runner import digest
from .store import WorldError, identifier, now


def package(world, package_id):
    found = [
        (owner, p) for owner, p in world.store.list("package") if p["id"] == package_id
    ]
    if len(found) != 1:
        raise WorldError("NOT_FOUND", "Package not found")
    return found[0]


def approve(world, package_id, operator, confirmed_sha256):
    with world.store.transaction():
        owner, p = package(world, package_id)
        if p["status"] != "awaiting_approval":
            raise WorldError("APPROVAL_STATE", "Package is not awaiting approval")
        path = world.settings.data_root / "packages" / p["id"] / "submission.zip"
        if confirmed_sha256 != p["sha256"] or digest(path) != p["sha256"]:
            raise WorldError(
                "HASH_CHANGED", "Approval must match the exact archive SHA-256"
            )
        if not operator.strip():
            raise WorldError("OPERATOR_REQUIRED", "Human operator name required")
        p.update(
            status="approved",
            approval={
                "id": identifier("approval"),
                "operator": operator,
                "sha256": p["sha256"],
                "approved_at": now(),
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).isoformat(),
            },
        )
        world.store.put("package", owner, p)
        return p


async def submit(world, package_id, gateway):
    owner, p = package(world, package_id)
    a = p.get("approval")
    if p["status"] != "approved" or not a:
        raise WorldError(
            "APPROVAL_REQUIRED", "Human approval is required before any Kaggle upload"
        )
    archive = world.settings.data_root / "packages" / p["id"] / "submission.zip"
    if a["sha256"] != p["sha256"] or digest(archive) != p["sha256"]:
        raise WorldError("HASH_CHANGED", "Archive changed after approval")
    if datetime.fromisoformat(a["expires_at"]) <= datetime.now(timezone.utc):
        raise WorldError("APPROVAL_EXPIRED", "Human approval expired")
    live_best = await gateway.best()
    if (
        isinstance(live_best, bool)
        or not isinstance(live_best, (int, float))
        or not math.isfinite(live_best)
        or live_best < 0
    ):
        raise WorldError(
            "BEST_UNAVAILABLE", "A finite current Kaggle best score is required"
        )
    if p["local_score"] <= live_best:
        raise WorldError(
            "NO_IMPROVEMENT", "Local estimate no longer exceeds live account best"
        )
    # Consume BEFORE sending. A network failure may have created a submission;
    # UNKNOWN never retries automatically and needs manual Kaggle reconciliation.
    with world.store.transaction():
        latest = world.store.get("package", owner, p["id"])
        if latest["status"] != "approved":
            raise WorldError("APPROVAL_CONSUMED", "Approval already used")
        if digest(archive) != a["sha256"]:
            raise WorldError("HASH_CHANGED", "Archive changed during score lookup")
        if datetime.fromisoformat(a["expires_at"]) <= datetime.now(timezone.utc):
            raise WorldError("APPROVAL_EXPIRED", "Human approval expired")
        p.update(status="submitting", live_best=live_best)
        world.store.put("package", owner, p)
    try:
        receipt = await gateway.upload(
            archive, world.settings.data_root / "packages" / p["id"]
        )
        if not receipt.get("submission_ref"):
            raise WorldError("SUBMISSION_UNKNOWN", "No confirmed submission ref")
        p.update(status="submitted", receipt=receipt)
    except BaseException:
        p.update(
            status="unknown",
            error="Upload outcome unknown; reconcile with Kaggle before any new approval.",
        )
        world.store.put("package", owner, p)
        raise
    world.store.put("package", owner, p)
    return p
