from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from star_bff.api.dependencies import csrf_user, current_user
from star_bff.errors import BFFError
from starlette.background import BackgroundTask

from .client import API

router = APIRouter(prefix=API)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def client(request):
    return request.app.state.quantum


def writable_user(request: Request, user=Depends(csrf_user)):
    if request.app.state.quantum_settings.read_only:
        raise BFFError("CONSOLE_READ_ONLY", "当前控制台为只读模式，不能取消实验、审批或提交。", 403)
    return user


@router.get("/summary")
async def summary(request: Request, user=Depends(current_user)):
    data = await client(request).request(user, "GET", "/summary")
    return {**data, "transport_mode": request.app.state.settings.transport_mode,
            "read_only": request.app.state.quantum_settings.read_only}


@router.get("/candidates")
async def candidates(request: Request, user=Depends(current_user)):
    return await client(request).request(user, "GET", "/candidates")


@router.get("/experiments")
async def experiments(request: Request, user=Depends(current_user)):
    return await client(request).request(user, "GET", "/experiments")


@router.get("/experiments/{experiment_id}")
async def experiment(request: Request, experiment_id: str, user=Depends(current_user)):
    return await client(request).request(user, "GET", "/experiments/" + client(request).identifier(experiment_id))


@router.get("/experiments/{experiment_id}/logs")
async def logs(request: Request, experiment_id: str, name: str = "verify.stdout",
               offset: int = Query(0, ge=0), limit: int = Query(16384, ge=1, le=262144),
               user=Depends(current_user)):
    return await client(request).request(
        user, "GET", "/experiments/" + client(request).identifier(experiment_id) + "/logs",
        params={"name": name, "offset": offset, "limit": limit},
    )


@router.post("/experiments/{experiment_id}/cancel")
async def cancel(request: Request, experiment_id: str, body: EmptyBody, user=Depends(writable_user)):
    return await client(request).request(
        user, "POST", "/experiments/" + client(request).identifier(experiment_id) + "/cancel", body={},
    )


@router.get("/packages")
async def packages(request: Request, user=Depends(current_user)):
    return await client(request).request(user, "GET", "/packages")


@router.get("/packages/{package_id}")
async def package(request: Request, package_id: str, user=Depends(current_user)):
    return await client(request).request(user, "GET", "/packages/" + client(request).identifier(package_id))


@router.post("/packages/{package_id}/approve")
async def approve(request: Request, package_id: str, body: Approval, user=Depends(writable_user)):
    return await client(request).request(
        user, "POST", "/packages/" + client(request).identifier(package_id) + "/approve", body=body.model_dump(),
    )


@router.post("/packages/{package_id}/submit")
async def submit(request: Request, package_id: str, body: Approval, user=Depends(writable_user)):
    return await client(request).request(
        user, "POST", "/packages/" + client(request).identifier(package_id) + "/submit",
        body=body.model_dump(), submission=True,
    )


@router.api_route("/artifacts/{artifact_id}", methods=["GET", "HEAD"])
async def artifact(request: Request, artifact_id: str, user=Depends(current_user)):
    upstream = await client(request).download(user, artifact_id, request.headers, request.method)
    headers = {name: upstream.headers[name] for name in (
        "content-length", "content-type", "content-disposition", "content-range", "accept-ranges", "etag", "last-modified"
    ) if name in upstream.headers}
    headers.update({"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    async def chunks():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(chunks(), status_code=upstream.status_code, headers=headers,
                             background=BackgroundTask(upstream.aclose))
