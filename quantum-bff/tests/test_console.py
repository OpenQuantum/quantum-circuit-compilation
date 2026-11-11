"""Browser session -> competition BFF -> real World API; Kaggle is always a fake."""

import asyncio
import hashlib
import sys

import httpx
import pytest
from pydantic import ValidationError
from star_bff.config import Settings as StarSettings

from quantum_bff.config import Settings
from quantum_bff.main import create_app
from quantum_world.config import Settings as WorldSettings
from quantum_world.operator_api import create_operator_app
from quantum_world.service import World
from quantum_world.store import canonical

TOKEN = "test-only-service-token-never-a-real-credential"
API = "/api/quantum/v1"
HUMAN = "/api/human/v1"


class Gateway:
    def __init__(self):
        self.uploads = 0

    async def best(self):
        return 1.0

    async def upload(self, archive, folder):
        self.uploads += 1
        return {"submission_ref": "fake-test-submission"}


@pytest.fixture
async def console(tmp_path):
    world = World(WorldSettings(tmp_path, tmp_path / "world", sys.executable, tmp_path,
                               allowed_agents={"real-agent": ["alice", "bob"]}))
    owner = canonical(["real-agent", "alice"])
    world.store.put("experiment", owner, {
        "id": "exp-alice", "task": "dqi", "mode": "full", "status": "succeeded",
        "created_at": "2026-11-10T00:00:00Z", "result": {"full": True, "task_cost": 12},
    })
    exp = world.settings.data_root / "experiments" / "exp-alice"
    exp.mkdir(parents=True)
    (exp / "verify.stdout").write_text("Verifier passed\n")
    package_folder = world.settings.data_root / "packages" / "pkg-alice"
    package_folder.mkdir(parents=True)
    archive = package_folder / "submission.zip"
    archive.write_bytes(b"test-artifact-not-real-circuits")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    world.store.put("package", owner, {
        "id": "pkg-alice", "status": "awaiting_approval", "sha256": sha,
        "local_score": 2.0, "created_at": "2026-11-10T00:00:00Z", "experiments": ["exp-alice"],
    })
    gateway = Gateway()
    operator = create_operator_app(world, token=TOKEN, operator_users={"alice"}, gateway=gateway)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>Quantum console</h1>")
    settings = StarSettings(auth_mode="development", transport_mode="demo", data_dir=tmp_path / "bff",
                            public_base_url="http://127.0.0.1:8320", ui_origin="http://127.0.0.1:8320")
    app = create_app(settings, Settings(operator_token=TOKEN, frontend_dir=frontend),
                     operator_transport=httpx.ASGITransport(app=operator))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=settings.public_base_url,
                                         headers={"Origin": settings.ui_origin}) as client:
                yield client, world, gateway, sha
    finally:
        await world.close()


async def login(client, name="alice"):
    response = await client.post(HUMAN + "/auth/development", json={"userId": name})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrfToken"]
    client.headers["X-CSRF-Token"] = csrf


async def test_static_console_and_session_scope(console):
    client, world, gateway, sha = console
    assert (await client.get("/")).status_code == 200
    assert (await client.get(API + "/summary")).status_code == 401
    await login(client)
    summary = await client.get(API + "/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["counts"]["experiments"] == 1
    assert summary.json()["can_approve"] is True
    assert summary.json()["transport_mode"] == "demo"
    assert summary.headers["cache-control"] == "private, no-store"
    record = (await client.get(API + "/experiments/exp-alice")).json()
    assert record["sender_id"] == "real-agent"  # Human never impersonates the Agent.
    assert record["artifacts"][0]["download_url"].startswith(API + "/artifacts/")
    await login(client, "bob")
    assert (await client.get(API + "/experiments")).json()["items"] == []
    assert (await client.get(API + "/experiments/exp-alice",
                             headers={"X-Quantum-User": "alice"})).status_code == 404
    assert (await client.get(record["artifacts"][0]["download_url"])).status_code == 404
    assert gateway.uploads == 0


async def test_csrf_hash_review_and_separate_submit(console):
    client, world, gateway, sha = console
    await login(client)
    path = API + "/packages/pkg-alice"
    assert (await client.post(path + "/submit", json={"sha256": sha})).status_code == 409
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.post(path + "/approve", json={"sha256": sha})).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert (await client.post(path + "/approve", json={"sha256": "0" * 64})).status_code == 409
    assert (await client.post(path + "/approve", json={"sha256": sha, "userId": "alice"})).status_code == 422
    assert (await client.post(path + "/approve", json={"sha256": sha},
                             headers={"Origin": "https://unrelated.invalid"})).status_code == 403
    approved = await client.post(path + "/approve", json={"sha256": sha})
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["operator"] == "alice"
    assert gateway.uploads == 0  # Approval itself never uploads.
    submitted = await client.post(path + "/submit", json={"sha256": sha})
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"
    assert gateway.uploads == 1
    assert (await client.post(path + "/submit", json={"sha256": sha})).status_code == 409
    assert gateway.uploads == 1


async def test_log_paging_and_streamed_download_range(console):
    client, _, _, _ = console
    await login(client)
    page = await client.get(API + "/experiments/exp-alice/logs", params={"limit": 8})
    assert page.status_code == 200, page.text
    assert page.json()["text"] == "Verifier"
    assert page.json()["next_offset"] == 8
    assert (await client.get(API + "/experiments/exp-alice/logs",
                             params={"name": "../../outside"})).status_code == 404
    package = (await client.get(API + "/packages/pkg-alice")).json()
    link = package["artifacts"][0]["download_url"]
    response = await client.get(link, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206, response.text
    assert response.content == b"test"
    assert response.headers["content-range"].startswith("bytes 0-3/")
    assert response.headers["content-disposition"].startswith("attachment;")
    head = await client.head(link)
    assert head.status_code == 200 and not head.content
    assert int(head.headers["content-length"]) > 4


async def test_existing_chat_http_contract_still_works(console):
    client, _, _, _ = console
    await login(client)
    agents = (await client.get(HUMAN + "/agents")).json()["items"]
    conversation = await client.post(HUMAN + "/interactions", json={
        "agentId": agents[0]["agentId"], "mode": "conversation",
    })
    assert conversation.status_code == 201, conversation.text
    interaction = conversation.json()["interactionSessionId"]
    response = await client.post(HUMAN + f"/interactions/{interaction}/runs", json={
        "inputId": "test-chat-input", "parts": [{"type": "text", "text": "Review the experiment"}],
    })
    assert response.status_code in {200, 201, 202}, response.text
    run_id = response.json()["runId"]
    for _ in range(100):
        snapshot = await client.get(HUMAN + "/runs/" + run_id)
        assert snapshot.status_code == 200
        if snapshot.json()["status"] in {"succeeded", "failed", "canceled"}:
            break
        await asyncio.sleep(.02)
    assert snapshot.json()["status"] == "succeeded"


@pytest.mark.parametrize("endpoint", ["http://remote.example", "http://u:p@localhost", "http://localhost/a", "https://world.example?x=1"])
def test_service_origin_is_fixed_and_tokens_hidden(endpoint):
    with pytest.raises(ValidationError):
        Settings(operator_url=endpoint, operator_token=TOKEN)
    assert TOKEN not in repr(Settings(operator_token=TOKEN))


async def test_mutation_timeout_is_not_retried(tmp_path):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("simulated", request=request)

    app = create_app(StarSettings(auth_mode="development", data_dir=tmp_path / "bff"),
                     Settings(operator_token=TOKEN, frontend_dir=tmp_path),
                     operator_transport=httpx.MockTransport(timeout))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8100") as client:
            await login(client)
            response = await client.post(API + "/packages/pkg/submit", json={"sha256": "0" * 64})
            assert response.status_code == 502
            assert response.json()["data"]["code"] == "WORLD_ACTION_UNCERTAIN"
            assert response.json()["data"]["retryable"] is False
    assert len(calls) == 1
    assert calls[0].headers["X-Quantum-User"] == "alice"


async def test_read_only_console_rejects_all_mutations_before_forwarding(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(500)

    app = create_app(StarSettings(auth_mode="development", data_dir=tmp_path / "bff"),
                     Settings(operator_token=TOKEN, frontend_dir=tmp_path, read_only=True),
                     operator_transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8100") as client:
            await login(client)
            for path, body in (("/experiments/exp/cancel", {}),
                               ("/packages/pkg/approve", {"sha256": "0" * 64}),
                               ("/packages/pkg/submit", {"sha256": "0" * 64})):
                response = await client.post(API + path, json=body)
                assert response.status_code == 403
                assert response.json()["data"]["code"] == "CONSOLE_READ_ONLY"
    assert calls == []
