"""Exercise external Runtime Action -> loopback Hub -> real Quantum World.

Run using the external Runtime environment with uvicorn supplied via uv --with.
The ephemeral Hub is an SDK router fixture, not a production authentication service.
No model, stored credentials or Kaggle API is used.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quantum-world/src"))


async def run(args):
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from star_protocol import AgentClient
    from star_protocol.server import MessageRouter
    from agent_runtime.adapters.star_protocol import HubConfig
    from agent_runtime.bootstrap import build_runtime
    from agent_runtime.config import RuntimeSettings
    from agent_runtime.contracts.generation import ToolCall
    from agent_runtime.modules.action.external.world_tools import WorldToolBridge
    from quantum_world.config import Settings
    from quantum_world.protocol import QuantumEnvironment
    from quantum_world.service import World
    from quantum_world.tools import DEFINITIONS

    router = MessageRouter()
    token = secrets.token_urlsafe(32)

    async def websocket(websocket):
        if websocket.headers.get("authorization") != "Bearer " + token:
            await websocket.close(code=1008)
            return
        await router.handle_connection(
            websocket, websocket.path_params["role"], websocket.path_params["client_id"]
        )

    app = Starlette(routes=[WebSocketRoute("/ws/{role}/{client_id}", websocket)])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=3)
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    world = endpoint = client = runtime = None
    pg_name = "quantum-tools-smoke-" + secrets.token_hex(5)
    pg_started = False
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.02)
        with TemporaryDirectory(prefix="quantum-tools-smoke-") as temporary:
            temp = Path(temporary)
            # Runtime main now requires PostgreSQL. Always use an isolated
            # disposable database, never an existing DSN from the operator.
            password = secrets.token_urlsafe(24)
            pg_env = dict(os.environ, POSTGRES_PASSWORD=password)
            subprocess.check_output(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    pg_name,
                    "-p",
                    "127.0.0.1::5432",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    "POSTGRES_USER=smoke",
                    "-e",
                    "POSTGRES_DB=smoke",
                    "postgres:17-alpine",
                ],
                env=pg_env,
            )
            pg_started = True
            pg_port = (
                subprocess.check_output(
                    ["docker", "port", pg_name, "5432/tcp"], text=True
                )
                .strip()
                .rsplit(":", 1)[1]
            )
            os.environ["AGENT_RUNTIME_POSTGRES_DSN"] = (
                f"postgresql://smoke:{password}@127.0.0.1:{pg_port}/smoke"
            )
            os.environ["AGENT_RUNTIME_POSTGRES_ROOT"] = str(temp)
            os.environ["AGENT_RUNTIME_POSTGRES_NAMESPACE"] = "quantum_smoke"
            import psycopg

            async with asyncio.timeout(30):
                while True:
                    try:
                        connection = await psycopg.AsyncConnection.connect(
                            os.environ["AGENT_RUNTIME_POSTGRES_DSN"], connect_timeout=1
                        )
                        await connection.close()
                        break
                    except psycopg.OperationalError:
                        await asyncio.sleep(0.2)
            data_project = args.data_project.resolve()
            python = data_project / ".venv/bin/python"
            seed = data_project / "official/data/sample_submission"
            if not python.is_file() or not (seed / "invsqrt.kmb").is_file():
                raise ValueError(
                    "data-project must contain the existing verifier environment and official seed"
                )
            world = World(
                Settings(
                    data_project,
                    temp / "world",
                    python,
                    seed,
                    allowed_agents={"quantum-agent-local": ["local-user"]},
                )
            )
            endpoint = QuantumEnvironment(world)

            class RuntimeClient(AgentClient):
                async def on_outcome(self, sender, content):
                    await bridge.on_outcome(self, sender, content)

            client = RuntimeClient("quantum-agent-local")
            url = f"ws://127.0.0.1:{port}"
            hub = SimpleNamespace(
                ready=True,
                client=client,
                session_generation=1,
                config=SimpleNamespace(world_id=world.settings.world_id),
            )
            runtime = build_runtime(
                RuntimeSettings(
                    storage_path=temp / "runtime.sqlite3",
                    hub=HubConfig(
                        agent_id=client.client_id,
                        world_id=world.settings.world_id,
                        url=url,
                    ),
                ),
                offline=True,
            )
            module = runtime.action_module
            bridge = WorldToolBridge(
                lambda: hub,
                module.manager,
                world_id=world.settings.world_id,
                agent_id=client.client_id,
                human_client_id="smoke-human",
                allowed_user_id="local-user",
            )
            module.world_bridge = bridge
            await runtime.start()
            headers = {"Authorization": "Bearer " + token}
            await endpoint.connect(url, headers=headers)
            await endpoint.start()
            await client.connect(url, headers=headers)
            await client.start()
            await client.join_environment(world.settings.world_id)
            async with asyncio.timeout(15):
                while client.protocol_for(world.settings.world_id) is None:
                    await asyncio.sleep(0.05)
            context = {
                "agentRunId": "smoke-generation",
                "runId": "smoke-run",
                "inputId": "smoke-input",
                "agentId": client.client_id,
                "humanClientId": "smoke-human",
                "userId": "local-user",
                "interactionSessionId": "smoke-session",
                "conversationId": "smoke-conversation",
                "worldId": world.settings.world_id,
            }
            binding = await module.prepare(context)
            offered = {tool.name for tool in binding.tools}
            expected = {tool["name"] for tool in DEFINITIONS}
            assert expected <= offered, offered
            assert not {"SubmitSubmission", "ApproveSubmission"} & offered
            sequence = 0

            async def call(name, parameters):
                nonlocal sequence
                sequence += 1
                result = await binding.executor.execute(
                    ToolCall(f"smoke-{sequence}", name, parameters), context
                )
                if result.get("status") != "completed":
                    raise RuntimeError(f"{name} did not complete: {result}")
                return json.loads(result["data"]["text"])

            task = await call("GetTask", {"task": "invsqrt"})
            assert task["approval_required"]
            candidate = await call(
                "CreateCandidate",
                {
                    "task": "invsqrt",
                    "entrypoint": "main.py",
                    "files": {"main.py": 'print("smoke candidate")\n'},
                },
            )
            queued = await call("RunExperiment", {"task": "invsqrt", "mode": "quick"})
            async with asyncio.timeout(90):
                while True:
                    report = await call(
                        "GetExperiment", {"experiment_id": queued["experiment_id"]}
                    )
                    if report["status"] not in ("queued", "running"):
                        break
                    await asyncio.sleep(0.3)
            assert report["status"] == "succeeded", report
            assert report["result"]["status"] == "PASS"
            assert report["result"]["task_cost"] is None
            assert not report["result"]["full"]
            evidence = {
                "status": "PASS",
                "transport": "real loopback WebSocket / SDK MessageRouter fixture",
                "runtime_commit": subprocess.check_output(
                    ["git", "-C", str(args.runtime_repo), "rev-parse", "HEAD"],
                    text=True,
                ).strip(),
                "competition_commit": subprocess.check_output(
                    ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
                ).strip(),
                "discovered_tools": sorted(expected),
                "candidate_created": bool(candidate["candidate_id"]),
                "official_quick_result": report["result"],
                "runtime_action_calls": sequence,
                "model_called": False,
                "kaggle_called": False,
                "production_hub_tested": False,
            }
            evidence["runtime_storage"] = "isolated temporary PostgreSQL 17 container"
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
            # Stop before temporary databases and input artifacts are removed.
            await client.stop()
            client = None
            await endpoint.stop()
            endpoint = None
            await runtime.stop()
            runtime = None
            await world.close()
            world = None
    finally:
        if client:
            await client.stop()
        if endpoint:
            await endpoint.stop()
        if runtime:
            await runtime.stop()
        if world:
            await world.close()
        await router.stop()
        server.should_exit = True
        await server_task
        listener.close()
        if pg_started:
            subprocess.run(
                ["docker", "rm", "-f", pg_name], stdout=subprocess.DEVNULL, check=True
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-repo", type=Path, default=ROOT.parent / "star-agent-runtime"
    )
    parser.add_argument(
        "--data-project", type=Path, default=ROOT.parent / "quantum-search"
    )
    parser.add_argument(
        "--report", type=Path, default=ROOT / ".local-console/runtime-tools-smoke.json"
    )
    args = parser.parse_args()
    # Only source modules are shared with this test harness, not runtime credentials.
    sys.path.insert(0, str(args.runtime_repo.resolve() / "src"))
    asyncio.run(run(args))
