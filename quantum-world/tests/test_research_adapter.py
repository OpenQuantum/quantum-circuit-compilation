import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

from quantum_world.research import ResearchClient
from quantum_world.store import WorldError


class ResearchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = ResearchClient(SimpleNamespace(
            research_url="http://127.0.0.1:8787/",
            research_token="read-token",
            research_results_token="results-token",
            world_id="world-1",
        ))

    def test_opt_in_fallback_on_empty_results_uses_short_keywords(self):
        def search(request, **kwargs):
            query = parse_qs(urlsplit(request.full_url).query, keep_blank_values=True)
            expected = query["query"][0] == "precision"
            self.assertEqual(query["limit"], ["2"])
            return io.BytesIO(json.dumps({
                "scope": "public_and_official_only", "task": "invsqrt", "read_only": True,
                "nodes": [{"id": "retune"}] if expected else [],
            }).encode())

        params = {"task": "invsqrt", "query": "an impossible search term", "limit": 2}
        with patch("quantum_world.research.build_opener") as factory:
            client = factory.return_value.open
            client.side_effect = search
            self.assertEqual(self.client.query("SearchResearchMethods", params)["nodes"], [])
            self.assertEqual(client.call_count, 1)
            client.reset_mock()
            result = self.client.query("SearchResearchMethods", {**params, "fallback": True})
            self.assertEqual(result["nodes"], [{"id": "retune"}])
            self.assertEqual(result["matched_query"], "precision")
            queries = [
                parse_qs(urlsplit(call.args[0].full_url).query)["query"][0]
                for call in client.call_args_list
            ]
            self.assertEqual(queries, ["an impossible search term", "impossible", "precision"])

    def test_auth_error_does_not_look_like_an_empty_search(self):
        with patch("quantum_world.research.build_opener") as factory:
            client = factory.return_value.open
            client.side_effect = HTTPError("http://127.0.0.1:8787", 401, "", {}, None)
            with self.assertRaises(WorldError) as error:
                self.client.query("SearchResearchMethods", {"task": "invsqrt", "query": "no-hit", "fallback": True})
            self.assertEqual(error.exception.code, "RESEARCH_HTTP_ERROR")
            client.assert_called_once()

    def test_result_writes_and_history_use_a_dedicated_token(self):
        record = {"world_id": "world-1", "user_id": "user +1", "task": "invsqrt", "experiment_id": "exp-1"}
        with patch("quantum_world.research.build_opener") as factory:
            transport = factory.return_value.open
            transport.return_value = io.BytesIO(json.dumps({"record": record, "created": True}).encode())
            self.client.write_result(record)
            request = transport.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8787/api/world/optimization/v1/experiments")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Authorization"), "Bearer results-token")
            self.assertEqual(json.loads(request.data), record)

            transport.return_value = io.BytesIO(json.dumps({
                "world_id": "world-1", "user_id": "user +1", "task": "invsqrt", "records": [record],
            }).encode())
            self.assertEqual(self.client.history("user +1"), [record])
            request = transport.call_args.args[0]
            self.assertEqual(urlsplit(request.full_url).path, "/api/world/optimization/v1/history")
            self.assertEqual(parse_qs(urlsplit(request.full_url).query)["user_id"], ["user +1"])
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer results-token")

    def test_history_refuses_another_users_records(self):
        with patch("quantum_world.research.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(json.dumps({
                "world_id": "world-1", "user_id": "user-1", "task": "invsqrt",
                "records": [{"world_id": "world-1", "user_id": "somebody-else", "task": "invsqrt"}],
            }).encode())
            with self.assertRaises(WorldError):
                self.client.history("user-1")
