"""Operator API integration tests use local fixtures and fake Kaggle only."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

from quantum_world.config import Settings
from quantum_world.operator_api import create_operator_app
from quantum_world.runner import digest, manifest
from quantum_world.service import REFERENCE, World
from quantum_world.store import canonical, identifier


TOKEN = "operator-api-unit-test-service-token-only"


class Runner:
    async def run(self, job, candidate, folder):
        inputs = folder / "input"
        inputs.mkdir()
        names = ("angle_k_0.kmb", "submission.npz") if job["task"] == "dfthcsos" else (job["task"] + ".kmb",)
        for name in names:
            (inputs / name).write_bytes(b"unit-fixture-not-a-real-circuit")
        (folder / "verify.stdout").write_bytes(b"0123456789\n")
        full = job["mode"] == "full"
        cost = REFERENCE[job["task"]] / 2
        return {"status": "PASS", "full": full, "task_cost": cost if full else None,
                "diagnostic_cost": None if full else cost, "files": manifest(inputs)}


class Gateway:
    def __init__(self):
        self.uploads = 0
        self.lookups = 0
        self.best_started = asyncio.Event()
        self.release_best = None
        self.release_upload = None
        self.score = 1

    async def best(self):
        self.lookups += 1
        self.best_started.set()
        if self.release_best:
            await self.release_best.wait()
        return self.score

    async def upload(self, archive, folder):
        self.uploads += 1
        if self.release_upload:
            await self.release_upload.wait()
        return {"submission_ref": "fake-only-never-real", "archive_sha256": digest(archive)}


class OperatorAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root, self.root / "state", Path(sys.executable), self.root,
                                 allowed_agents={"agent-a": ["user", "other"], "agent-b": ["user"]})
        self.world = World(self.settings, Runner())
        self.gateway = Gateway()
        self.app = create_operator_app(self.world, token=TOKEN, operator_users={"user"}, gateway=self.gateway)
        self.client = self.make_client(self.app)

    def make_client(self, app):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://world.test",
                                 headers={"Authorization": "Bearer " + TOKEN, "X-Quantum-User": "user"})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.world.close()
        self.temp.cleanup()

    async def call(self, tool, params, sender="agent-a", user="user"):
        result = await self.world.call(sender, identifier("action"), tool, {"userId": user, **params})
        return json.loads(result["data"]["text"])

    async def experiment(self, task="invsqrt", sender="agent-a", user="user", *, mode="full"):
        result = await self.call("RunExperiment", {"task": task, "mode": mode}, sender, user)
        await self.world.workers[result["experiment_id"]]
        return result["experiment_id"]

    async def package(self):
        ids = [await self.experiment(task) for task in REFERENCE]
        return await self.call("BuildSubmission", {"experiments": ids})

    async def approve(self, package):
        return await self.client.post("/operator/v1/packages/" + package["id"] + "/approve", json={"sha256": package["sha256"]})

    async def test_authentication_and_configuration_fail_closed(self):
        for headers, status in [({"Authorization": "Bearer invalid"}, 401), ({"X-Quantum-User": "unassigned"}, 403)]:
            response = await self.client.get("/operator/v1/summary", headers=headers)
            self.assertEqual(response.status_code, status)
        for invalid in ("short", " " * 32, "x" * 32 + "\n", "密" * 32):
            with self.assertRaises(ValueError):
                create_operator_app(self.world, token=invalid)
        response = await self.client.get("/operator/v1/summary")
        self.assertTrue(response.json()["can_approve"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertIs(self.app.state.world, self.world)

    async def test_scope_uses_real_sender_and_user_assignments(self):
        first = await self.experiment()
        second = await self.experiment(sender="agent-b")
        hidden = await self.experiment(user="other")
        self.world.store.put("experiment", canonical(["revoked-agent", "user"]),
                             {"id": "exp-revoked", "task": "dqi", "status": "failed"})
        response = await self.client.get("/operator/v1/experiments")
        self.assertEqual({row["id"] for row in response.json()["items"]}, {first, second})
        self.assertEqual((await self.client.get("/operator/v1/experiments/" + hidden)).status_code, 404)
        self.assertEqual((await self.client.get("/operator/v1/experiments/exp-revoked")).status_code, 404)
        summary = (await self.client.get("/operator/v1/summary")).json()
        self.assertEqual(summary["counts"]["experiments"], 2)
        self.assertEqual(summary["experiments_by_status"], {"succeeded": 2})
        self.assertEqual(summary["counts"]["active_experiments"], 0)

    async def test_user_without_submission_permission_can_read_quick_result_and_log(self):
        ident = await self.experiment(mode="quick")
        app = create_operator_app(self.world, token=TOKEN, operator_users=(), gateway=self.gateway)
        async with self.make_client(app) as client:
            summary = await client.get("/operator/v1/summary")
            self.assertEqual(summary.status_code, 200)
            self.assertFalse(summary.json()["can_approve"])

            url = f"/operator/v1/experiments/{ident}"
            response = await client.get(url)
            self.assertEqual(response.status_code, 200)
            report = response.json()
            self.assertEqual(report["sender_id"], "agent-a")
            self.assertEqual(report["status"], "succeeded")
            self.assertEqual(report["mode"], "quick")
            self.assertIsNone(report["result"]["task_cost"])
            self.assertEqual(report["result"]["diagnostic_cost"], REFERENCE["invsqrt"] / 2)

            log = await client.get(url + "/logs", params={"name": "verify.stdout"})
            self.assertEqual(log.status_code, 200)
            self.assertEqual(log.json()["text"], "0123456789\n")
            self.assertTrue(log.json()["eof"])

            for operation in ("approve", "submit"):
                response = await client.post(f"/operator/v1/packages/pkg-example/{operation}", json={"sha256": "0" * 64})
                self.assertEqual(response.status_code, 403)
            self.assertEqual(self.gateway.uploads, 0)

    async def test_candidate_and_package_records_are_available(self):
        candidate = await self.call("CreateCandidate", {"task": "invsqrt", "entrypoint": "main.py", "files": {"main.py": "pass"}})
        rows = (await self.client.get("/operator/v1/candidates")).json()["items"]
        self.assertEqual(rows[0]["id"], candidate["candidate_id"])
        package = await self.package()
        rows = (await self.client.get("/operator/v1/packages")).json()["items"]
        self.assertEqual(rows[0]["id"], package["id"])
        self.assertEqual({a["name"] for a in rows[0]["artifacts"]}, {"review.json", "submission.zip"})
        self.assertEqual(self.gateway.lookups, 0)
        self.assertEqual(self.gateway.uploads, 0)

    async def test_log_offsets_and_range_download_are_scoped_and_confined(self):
        ident = await self.experiment()
        detail = (await self.client.get("/operator/v1/experiments/" + ident)).json()
        self.assertNotIn("salt", detail)
        self.assertNotIn("files", detail["result"])
        response = await self.client.get(f"/operator/v1/experiments/{ident}/logs", params={"name": "verify.stdout", "offset": 2, "limit": 4})
        self.assertEqual(response.json()["text"], "2345")
        self.assertEqual(response.json()["next_offset"], 6)
        self.assertFalse(response.json()["eof"])
        url = next(item["download_url"] for item in detail["artifacts"] if item["name"] == "verify.stdout")
        response = await self.client.get(url, headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        response = await self.client.head(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "11")
        self.assertEqual((await self.client.get(url, headers={"X-Quantum-User": "other"})).status_code, 404)
        self.assertEqual((await self.client.get(url + "tampered")).status_code, 404)
        self.assertEqual((await self.client.get(f"/operator/v1/experiments/{ident}/logs", params={"name": "../world.sqlite3"})).status_code, 404)
        path = self.settings.data_root / "experiments" / ident / "verify.stdout"
        path.unlink()
        path.symlink_to(self.settings.data_root / "world.sqlite3")
        self.assertEqual((await self.client.get(url)).status_code, 404)

    async def test_manifest_artifacts_stream_large_circuits_and_nested_sources(self):
        ident = await self.experiment()
        path = self.settings.data_root / "experiments" / ident / "input" / "invsqrt.kmb"
        size = 32 * 1024 * 1024
        with path.open("wb") as stream:
            stream.seek(size - 4)
            stream.write(b"TAIL")
        owner = canonical(["agent-a", "user"])
        row = self.world.store.get("experiment", owner, ident)
        row["result"]["files"] = {"invsqrt.kmb": digest(path)}
        self.world.store.put("experiment", owner, row)
        detail = (await self.client.get("/operator/v1/experiments/" + ident)).json()
        artifact = next(item for item in detail["artifacts"] if item["name"] == "input/invsqrt.kmb")
        self.assertEqual(artifact["size_bytes"], size)
        response = await self.client.get(artifact["download_url"], headers={"Range": f"bytes={size - 4}-{size - 1}"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"TAIL")
        self.assertEqual(response.headers["content-range"], f"bytes {size - 4}-{size - 1}/{size}")
        self.assertEqual((await self.client.get(f"/operator/v1/experiments/{ident}/logs", params={"name": "input/invsqrt.kmb"})).status_code, 404)

        candidate = await self.call("CreateCandidate", {"task": "dqi", "entrypoint": "lib/main.py", "files": {"lib/main.py": "print('fixture')"}})
        rows = (await self.client.get("/operator/v1/candidates")).json()["items"]
        source = next(row for row in rows if row["id"] == candidate["candidate_id"])["artifacts"][0]
        self.assertEqual((await self.client.get(source["download_url"])).text, "print('fixture')")
        self.assertEqual((await self.client.get(source["download_url"], headers={"X-Quantum-User": "other"})).status_code, 404)
        folder = self.settings.data_root / "candidates" / candidate["candidate_id"] / "lib"
        (folder / "main.py").unlink()
        folder.rmdir()
        target = self.root / "outside"
        target.mkdir()
        (target / "main.py").write_text("must not be served")
        folder.symlink_to(target, target_is_directory=True)
        self.assertEqual((await self.client.get(source["download_url"])).status_code, 404)

    async def test_cancellation_targets_existing_live_worker(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked(job, candidate, folder):
            started.set()
            await release.wait()
        self.world.runner.run = blocked
        result = await self.call("RunExperiment", {"task": "dqi", "mode": "full"})
        ident = result["experiment_id"]
        worker = self.world.workers[ident]
        await started.wait()
        response = await self.client.post(f"/operator/v1/experiments/{ident}/cancel")
        self.assertEqual(response.status_code, 200)
        await worker
        self.assertEqual((await self.client.get(f"/operator/v1/experiments/{ident}")).json()["status"], "cancelled")

    async def test_approval_role_hash_and_explicit_submission(self):
        package = await self.package()
        base = "/operator/v1/packages/" + package["id"]
        response = await self.client.post(base + "/approve", json={"sha256": package["sha256"]}, headers={"X-Quantum-User": "other"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await self.client.post(base + "/approve", json={"sha256": "0" * 64})).status_code, 409)
        self.assertEqual((await self.client.post(base + "/submit", json={"sha256": package["sha256"]})).status_code, 409)
        self.assertEqual(self.gateway.uploads, 0)
        response = await self.approve(package)
        self.assertEqual(response.json()["approval"]["operator"], "user")
        self.assertEqual(self.gateway.uploads, 0)
        response = await self.client.post(base + "/submit", json={"sha256": "0" * 64})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.gateway.lookups, 0)
        self.gateway.score = 3
        response = await self.client.post(base + "/submit", json={"sha256": package["sha256"]})
        self.assertEqual(response.json()["error"]["code"], "NO_IMPROVEMENT")
        self.assertEqual(self.gateway.uploads, 0)
        self.gateway.score = 1
        response = await self.client.post(base + "/submit", json={"sha256": package["sha256"]})
        self.assertEqual(response.json()["status"], "submitted")
        self.assertEqual(self.gateway.uploads, 1)
        self.assertEqual((await self.client.post(base + "/submit", json={"sha256": package["sha256"]})).status_code, 409)
        self.assertEqual(self.gateway.uploads, 1)

    async def test_double_submission_has_only_one_upload(self):
        package = await self.package()
        await self.approve(package)
        self.gateway.release_best = asyncio.Event()
        url = "/operator/v1/packages/" + package["id"] + "/submit"
        first = asyncio.create_task(self.client.post(url, json={"sha256": package["sha256"]}))
        await self.gateway.best_started.wait()
        second = await self.client.post(url, json={"sha256": package["sha256"]})
        self.assertEqual(second.json()["error"]["code"], "SUBMISSION_IN_PROGRESS")
        self.gateway.release_best.set()
        self.assertEqual((await first).json()["status"], "submitted")
        self.assertEqual(self.gateway.uploads, 1)

    async def test_upload_timeout_stays_unknown_and_does_not_retry(self):
        package = await self.package()
        await self.approve(package)
        self.gateway.release_upload = asyncio.Event()
        app = create_operator_app(self.world, token=TOKEN, operator_users={"user"}, gateway=self.gateway, submit_timeout=0.02)
        async with self.make_client(app) as client:
            base = "/operator/v1/packages/" + package["id"]
            response = await client.post(base + "/submit", json={"sha256": package["sha256"]})
            self.assertEqual(response.status_code, 504)
            self.assertEqual((await client.get(base)).json()["status"], "unknown")
            self.assertEqual((await client.post(base + "/submit", json={"sha256": package["sha256"]})).status_code, 409)
            self.assertEqual((await client.post(base + "/approve", json={"sha256": package["sha256"]})).status_code, 409)
        self.assertEqual(self.gateway.uploads, 1)


if __name__ == "__main__":
    unittest.main()
