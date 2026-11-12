import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from quantum_world.config import Settings
from quantum_world.research import ResearchClient
from quantum_world.service import World
from quantum_world.store import WorldError


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_observation_is_durable_and_identity_is_required(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            world = World(
                Settings(
                    root,
                    root / "state",
                    Path("/unused"),
                    root,
                    allowed_agents={"agent": ["user"]},
                    research_url="http://127.0.0.1:8787",
                    research_token="test-token",
                )
            )
            try:
                world.research.query = Mock(
                    return_value={
                        "task": "invsqrt",
                        "nodes": [{"id": "method-1"}],
                        "read_only": True,
                    }
                )
                params = {"userId": "user", "task": "invsqrt", "limit": 2}
                first = await world.call(
                    "agent", "same-id", "SearchResearchMethods", params
                )
                self.assertEqual(
                    first,
                    await world.call(
                        "agent", "same-id", "SearchResearchMethods", params
                    ),
                )
                self.assertEqual(world.research.query.call_count, 1)
                with self.assertRaises(WorldError) as error:
                    await world.call(
                        "agent",
                        "other-id",
                        "GetResearchNode",
                        {"userId": "other", "task": "invsqrt", "node_id": "method-1"},
                    )
                self.assertEqual(error.exception.code, "FORBIDDEN")
                self.assertEqual(world.research.query.call_count, 1)
            finally:
                await world.close()

    async def test_adapter_only_uses_fixed_service_origin_and_checks_response_scope(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                root,
                root,
                Path("/unused"),
                root,
                research_url="http://127.0.0.1:8787",
                research_token="test-token",
            )
            client = ResearchClient(settings)
            params = {"task": "dfthcsos", "node_id": "method-1"}
            for scope in ("public_and_official_only", "all_members"):
                payload = json.dumps(
                    {"task": "dfthcsos", "nodes": [], "scope": scope, "read_only": True}
                ).encode()
                with patch("quantum_world.research.build_opener") as factory:
                    factory.return_value.open.return_value = io.BytesIO(payload)
                    if scope == "all_members":
                        with self.assertRaises(WorldError):
                            client.query("GetResearchNode", params)
                    else:
                        self.assertEqual(
                            client.query("GetResearchNode", params)["task"], "dfthcsos"
                        )
                    request = factory.return_value.open.call_args.args[0]
                    self.assertTrue(
                        request.full_url.startswith(
                            "http://127.0.0.1:8787/api/agent/research?"
                        )
                    )
                    self.assertEqual(request.get_method(), "GET")
                    self.assertEqual(
                        request.get_header("Authorization"), "Bearer test-token"
                    )
            with self.assertRaises(ValueError):
                Settings(
                    root, root, Path("/unused"), root, research_url="http://example.com"
                )
