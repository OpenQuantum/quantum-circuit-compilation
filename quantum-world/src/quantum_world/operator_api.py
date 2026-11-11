"""Trusted BFF API over a live World; never advertised as Agent tools.

The service token authenticates the BFF. X-Quantum-User is its authenticated
user, not a browser-selected identity. This API must remain on a trusted link.
"""

import asyncio
import base64
from collections import Counter
import copy
import hashlib
import hmac
import json
import os
from pathlib import PurePosixPath
import re
import secrets

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import approval
from .store import WorldError, canonical, now


LOG_NAMES = frozenset(
    prefix + suffix
    for prefix in ("generator", "preflight", "verify")
    for suffix in (".stdout", ".stderr")
)
ARTIFACT_NAMES = {
    "candidate": frozenset(),
    "experiment": LOG_NAMES,
    "package": frozenset({"submission.zip", "review.json", "receipt.json"}),
}
ENTITY_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")


class HashConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def create_operator_app(
    world, *, token=None, operator_users=None, gateway=None, submit_timeout=3720
):
    """Build routes without constructing, recovering, or closing a World."""
    token = token if token is not None else os.environ.get("QUANTUM_OPERATOR_TOKEN", "")
    if not isinstance(token, str) or len(token) < 32 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("QUANTUM_OPERATOR_TOKEN must contain at least 32 printable non-space ASCII characters")
    if submit_timeout <= 0:
        raise ValueError("submit_timeout must be positive")
    operators = frozenset(
        operator_users
        if operator_users is not None
        else (value.strip() for value in os.environ.get("QUANTUM_OPERATOR_USERS", "").split(",") if value.strip())
    )
    key = token.encode()
    in_flight = set()
    app = FastAPI(title="Quantum World operator API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.world = world

    @app.exception_handler(WorldError)
    async def world_error(request: Request, exc: WorldError):
        status = {
            "UNAUTHENTICATED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404,
            "INVALID_ARGUMENTS": 422, "SUBMISSION_TIMEOUT": 504,
            "SUBMISSION_FAILED": 502, "KAGGLE_ERROR": 502,
        }.get(exc.code, 409)
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=status)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def owners(user):
        return {
            canonical([sender, user])
            for sender, users in world.settings.allowed_agents.items()
            if user in users
        }

    async def identity(
        authorization: str | None = Header(default=None),
        x_quantum_user: str | None = Header(default=None),
    ):
        supplied = (authorization or "").removeprefix("Bearer ")
        if not (authorization or "").startswith("Bearer ") or not secrets.compare_digest(supplied.encode(), key):
            raise WorldError("UNAUTHENTICATED", "A valid operator service credential is required")
        if not x_quantum_user or len(x_quantum_user) > 256 or not owners(x_quantum_user):
            raise WorldError("FORBIDDEN", "User is not assigned to this World")
        return x_quantum_user

    async def operator(user=Depends(identity)):
        if user not in operators:
            raise WorldError("FORBIDDEN", "This user is not an authorized submission operator")
        return user

    def records(kind, user):
        permitted = owners(user)
        return [(owner, row) for owner, row in world.store.list(kind) if owner in permitted]

    def find(kind, entity_id, user):
        for owner, row in records(kind, user):
            if row["id"] == entity_id:
                return owner, row
        raise WorldError("NOT_FOUND", "No matching object is available to this user")

    def artifact_names(kind, row):
        names = set(ARTIFACT_NAMES.get(kind, ()))
        if kind == "candidate":
            names.update(row.get("files", {}))
        elif kind == "experiment":
            names.update("input/" + name for name in (row.get("result") or {}).get("files", {}))
        return names

    def artifact_path(kind, row, name):
        entity_id = row["id"]
        if not ENTITY_ID.fullmatch(entity_id) or name not in artifact_names(kind, row):
            raise WorldError("NOT_FOUND", "Artifact not found")
        relative = PurePosixPath(name)
        if (not name or name != relative.as_posix() or relative.is_absolute()
                or ".." in relative.parts or "\\" in name or any(ord(c) < 32 for c in name)):
            raise WorldError("NOT_FOUND", "Artifact not found")
        root = world.settings.data_root.resolve()
        container = root / {"experiment": "experiments", "candidate": "candidates", "package": "packages"}[kind]
        folder = container / entity_id
        path = folder / name
        # A manifest authorizes a relative name, never a caller-provided host
        # path. Reject symlinks at every level, including nested source folders.
        segments = [container, folder] + [folder.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)]
        if any(part.is_symlink() for part in segments):
            raise WorldError("NOT_FOUND", "Artifact not found")
        if not path.is_file() or not path.resolve().is_relative_to(folder):
            raise WorldError("NOT_FOUND", "Artifact not found")
        return path

    def artifact_id(kind, entity_id, name):
        payload = base64.urlsafe_b64encode(canonical([kind, entity_id, name]).encode()).decode().rstrip("=")
        signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def decode_artifact(value):
        try:
            if len(value) > 1024:
                raise ValueError
            payload, supplied = value.rsplit(".", 1)
            signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
            if not secrets.compare_digest(supplied.encode(), signature.encode()):
                raise ValueError
            parts = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(parts, list) or len(parts) != 3 or not all(isinstance(p, str) for p in parts):
                raise ValueError
            if parts[0] not in ARTIFACT_NAMES:
                raise ValueError
            return parts
        except (ValueError, UnicodeError):
            raise WorldError("NOT_FOUND", "Artifact not found") from None

    def public(kind, owner, row):
        value = copy.deepcopy(row)
        value.pop("salt", None)
        if isinstance(value.get("result"), dict):
            value["result"].pop("files", None)
        value["sender_id"] = json.loads(owner)[0]
        value["artifacts"] = []
        for name in sorted(artifact_names(kind, row)):
            try:
                path = artifact_path(kind, row, name)
            except WorldError:
                continue
            ident = artifact_id(kind, row["id"], name)
            value["artifacts"].append({
                "id": ident, "name": name, "size_bytes": path.stat().st_size,
                "download_url": "/operator/v1/artifacts/" + ident,
            })
        return value

    def listing(kind, user):
        rows = sorted(records(kind, user), key=lambda item: (item[1].get("created_at", ""), item[1]["id"]), reverse=True)
        return {"items": [public(kind, owner, row) for owner, row in rows]}

    prefix = "/operator/v1"

    @app.get(prefix + "/summary")
    async def summary(user=Depends(identity)):
        experiments = records("experiment", user)
        packages = records("package", user)
        return {
            "world_id": world.settings.world_id, "user_id": user,
            "can_approve": user in operators, "configured_best": world.settings.current_best,
            "counts": {"candidates": len(records("candidate", user)), "experiments": len(experiments), "packages": len(packages),
                       "active_experiments": sum(row["status"] in {"queued", "running"} for _, row in experiments),
                       "awaiting_approval": sum(row["status"] == "awaiting_approval" for _, row in packages)},
            "experiments_by_status": dict(Counter(row["status"] for _, row in experiments)),
            "packages_by_status": dict(Counter(row["status"] for _, row in packages)),
            "best_local_score": max((row["local_score"] for _, row in packages), default=None),
            "approval_required": True,
        }

    @app.get(prefix + "/candidates")
    async def candidates(user=Depends(identity)):
        return listing("candidate", user)

    @app.get(prefix + "/experiments")
    async def experiments(user=Depends(identity)):
        return listing("experiment", user)

    @app.get(prefix + "/experiments/{experiment_id}")
    async def experiment(experiment_id: str, user=Depends(identity)):
        return public("experiment", *find("experiment", experiment_id, user))

    @app.get(prefix + "/experiments/{experiment_id}/logs")
    async def logs(
        experiment_id: str, name: str = "verify.stdout",
        offset: int = Query(default=0, ge=0), limit: int = Query(default=16384, ge=1, le=262144),
        user=Depends(identity),
    ):
        _, row = find("experiment", experiment_id, user)
        if name not in LOG_NAMES:
            raise WorldError("NOT_FOUND", "Log not found")
        path = artifact_path("experiment", row, name)
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(min(offset, size))
            start = stream.tell()
            content = stream.read(limit)
            end = stream.tell()
        return {"experiment_id": experiment_id, "name": name, "offset": start, "next_offset": end,
                "size_bytes": size, "eof": end >= size, "text": content.decode("utf-8", "replace")}

    @app.post(prefix + "/experiments/{experiment_id}/cancel")
    async def cancel(experiment_id: str, user=Depends(identity)):
        owner, row = find("experiment", experiment_id, user)
        if row["status"] in {"queued", "running"}:
            with world.store.transaction():
                row = world.store.get("experiment", owner, experiment_id)
                row["cancel_requested"] = True
                if row["status"] == "queued":
                    row.update(status="cancelled", finished_at=now())
                world.store.put("experiment", owner, row)
            worker = world.workers.get(experiment_id)
            if worker:
                worker.cancel()
        return public("experiment", owner, world.store.get("experiment", owner, experiment_id))

    @app.get(prefix + "/packages")
    async def packages(user=Depends(identity)):
        return listing("package", user)

    @app.get(prefix + "/packages/{package_id}")
    async def package(package_id: str, user=Depends(identity)):
        return public("package", *find("package", package_id, user))

    @app.head(prefix + "/artifacts/{artifact}")
    @app.get(prefix + "/artifacts/{artifact}")
    async def download(artifact: str, user=Depends(identity)):
        kind, entity_id, name = decode_artifact(artifact)
        _, row = find(kind, entity_id, user)
        path = artifact_path(kind, row, name)
        return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.post(prefix + "/packages/{package_id}/approve")
    async def approve_package(package_id: str, body: HashConfirmation, user=Depends(operator)):
        owner, _ = find("package", package_id, user)
        result = approval.approve(world, package_id, user, body.sha256)
        return public("package", owner, result)

    @app.post(prefix + "/packages/{package_id}/submit")
    async def submit_package(package_id: str, body: HashConfirmation, user=Depends(operator)):
        owner, row = find("package", package_id, user)
        if not secrets.compare_digest(body.sha256, row["sha256"]):
            raise WorldError("HASH_CHANGED", "Submission must match the reviewed archive SHA-256")
        if package_id in in_flight:
            raise WorldError("SUBMISSION_IN_PROGRESS", "Submission is already in progress; query its status")
        in_flight.add(package_id)
        try:
            selected_gateway = gateway
            if selected_gateway is None:
                from .gateway import KaggleGateway
                selected_gateway = KaggleGateway(world.settings)
            async with asyncio.timeout(submit_timeout):
                result = await approval.submit(world, package_id, selected_gateway)
            return public("package", owner, result)
        except TimeoutError:
            raise WorldError("SUBMISSION_TIMEOUT", "Submission timed out; inspect package status before any further action") from None
        except WorldError:
            raise
        except Exception:
            raise WorldError("SUBMISSION_FAILED", "Submission failed; inspect package status and reconcile Kaggle before retrying") from None
        finally:
            in_flight.discard(package_id)

    return app
