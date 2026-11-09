"""Run with Star Runtime's interpreter; real Action/Bridge/World, in-memory I/O.

No model provider, credentials, Hub service or Kaggle request is used.
"""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_runtime.adapters.star_protocol import HubConfig
from agent_runtime.bootstrap import build_runtime
from agent_runtime.config import RuntimeSettings
from agent_runtime.contracts.generation import ToolCall
from agent_runtime.modules.action.external.world_tools import WorldToolBridge
from star_protocol import validate_compact_content

from quantum_world.config import Settings
from quantum_world.protocol import QuantumEnvironment
from quantum_world.service import World
from quantum_world.tools import DEFINITIONS


class InMemoryLink:
    client_id = "quantum-agent-local"
    current_env = "quantum-world-local"

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.outcomes = {}
        self.callback = None
        endpoint.send_outcome = self.receive

    def specification_for(self, peer):
        return {
            "protocol": "agent-env-compact/2",
            "tools": deepcopy(DEFINITIONS),
            "events": [],
        }

    def protocol_for(self, peer):
        return "agent-env-compact/2"

    async def send_action(self, recipient, name, params, *, action_id):
        content = {"id": action_id, "name": name, "params": params}
        validate_compact_content("action", content)
        await self.endpoint.on_action(self.client_id, content)

    async def receive(self, sender, outcome):
        validate_compact_content("outcome", outcome)
        self.outcomes[outcome["refs"][0]] = outcome
        await self.callback(self, self.current_env, outcome)

    async def await_action(self, action_id, *, timeout=30):
        return self.outcomes[action_id]

    def restore_action(self, recipient, content):
        pass

    def action_outcomes(self, action_id):
        return [self.outcomes[action_id]] if action_id in self.outcomes else []


class RuntimeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_identity_injection_and_real_world_roundtrip(self):
        with (
            TemporaryDirectory() as directory,
            patch("socket.socket.connect", side_effect=AssertionError("offline test")),
        ):
            root = Path(directory)
            settings = Settings(
                root,
                root / "world",
                Path("/unused"),
                root,
                allowed_agents={"quantum-agent-local": ["local-user"]},
            )
            world = World(settings)
            client = InMemoryLink(QuantumEnvironment(world))
            hub = SimpleNamespace(
                ready=True,
                client=client,
                session_generation=1,
                config=SimpleNamespace(world_id="quantum-world-local"),
            )
            runtime = build_runtime(
                RuntimeSettings(
                    storage_path=root / "runtime.sqlite3",
                    hub=HubConfig(
                        agent_id=client.client_id,
                        world_id=client.current_env,
                        url="ws://127.0.0.1:1",
                    ),
                ),
                offline=True,
            )
            module = runtime.action_module
            bridge = WorldToolBridge(
                lambda: hub,
                module.manager,
                world_id=client.current_env,
                agent_id=client.client_id,
                human_client_id="local-human",
                allowed_user_id="local-user",
            )
            module.world_bridge = bridge
            client.callback = bridge.on_outcome
            context = {
                "agentRunId": "gen-one",
                "runId": "run-one",
                "inputId": "input-one",
                "agentId": client.client_id,
                "humanClientId": "local-human",
                "userId": "local-user",
                "interactionSessionId": "session-one",
                "conversationId": "conversation-one",
                "worldId": client.current_env,
            }
            await runtime.start()
            try:
                binding = await module.prepare(context)
                offered = {t.name: t for t in binding.tools}
                self.assertTrue({d["name"] for d in DEFINITIONS} <= offered.keys())
                self.assertNotIn(
                    "userId", offered["GetTask"].input_schema["properties"]
                )
                result = await binding.executor.execute(
                    ToolCall("get-task-once", "GetTask", {"task": "invsqrt"}), context
                )
                self.assertEqual(result["status"], "completed")
                observation = json.loads(result["data"]["text"])
                self.assertTrue(observation["approval_required"])
                self.assertEqual(observation["task"], "invsqrt")
                self.assertEqual(len(world.store.list("experiment")), 0)
                self.assertNotIn("SubmitSubmission", offered)
            finally:
                await runtime.stop()
                await world.close()


if __name__ == "__main__":
    unittest.main()
