import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from types import SimpleNamespace

from star_protocol import validate_compact_content, ProtocolConflictError
from quantum_world.approval import approve, package, submit
from quantum_world.config import Settings
from quantum_world.kaggle_gateway import account_best, check_upload_authorization
from quantum_world.protocol import QuantumEnvironment
from quantum_world.runner import clean_env, digest, manifest, process
from quantum_world.service import REFERENCE, World
from quantum_world.store import WorldError, identifier
from quantum_world.tools import DEFINITIONS


def data(outcome):
    return json.loads(outcome["data"]["text"])


class FakeRunner:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = None

    async def run(self, job, candidate, folder):
        self.calls += 1
        self.started.set()
        if self.release:
            await self.release.wait()
        inputs = folder / "input"
        inputs.mkdir()
        if job["task"] == "dfthcsos":
            (inputs / "angle_k_0.kmb").write_bytes(b"unit-fixture-circuit")
            (inputs / "submission.npz").write_bytes(b"unit-fixture-factorization")
        else:
            (inputs / (job["task"] + ".kmb")).write_bytes(b"unit-fixture-circuit")
        return {
            "status": "PASS",
            "full": job["mode"] == "full",
            "task_cost": REFERENCE[job["task"]] / 2,
            "files": manifest(inputs),
        }


class Gateway:
    def __init__(self, best=1, fail=False):
        self.score, self.fail = best, fail
        self.lookups = self.uploads = 0

    async def best(self):
        self.lookups += 1
        return self.score

    async def upload(self, archive, folder):
        self.uploads += 1
        if self.fail:
            raise OSError("ambiguous transport failure")
        return {
            "submission_ref": "fake-unit-test-ref",
            "archive_sha256": digest(archive),
        }


class WorldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            self.root,
            self.root / "state",
            Path(sys.executable),
            self.root,
            allowed_agents={"agent": ["user", "other-user"]},
        )
        self.runner = FakeRunner()
        self.world = World(self.settings, self.runner)

    async def asyncTearDown(self):
        await self.world.close()
        self.temp.cleanup()

    async def call(self, name, params=None, action_id=None, user="user"):
        return await self.world.call(
            "agent",
            action_id or identifier("action"),
            name,
            {"userId": user, **(params or {})},
        )

    async def experiment(self, task="invsqrt", mode="full"):
        result = data(await self.call("RunExperiment", {"task": task, "mode": mode}))
        await self.world.workers[result["experiment_id"]]
        return result["experiment_id"]

    async def build(self):
        ids = [await self.experiment(task) for task in REFERENCE]
        return data(await self.call("BuildSubmission", {"experiments": ids}))

    def assert_code(self, expected):
        class Check:
            def __enter__(inner):
                return inner

            def __exit__(inner, kind, error, trace):
                self.assertIsInstance(error, WorldError)
                self.assertEqual(error.code, expected)
                return True

        return Check()

    async def test_tools_never_advertise_approval_or_upload(self):
        self.assertEqual(
            {t["name"] for t in DEFINITIONS},
            {
                "GetTask",
                "CreateCandidate",
                "RunExperiment",
                "GetExperiment",
                "CancelExperiment",
                "BuildSubmission",
            },
        )
        with self.assert_code("UNKNOWN_TOOL"):
            await self.call("SubmitSubmission")

    async def test_action_replay_is_durable_and_does_not_queue_twice(self):
        params = {"task": "invsqrt", "mode": "full"}
        a = await self.call("RunExperiment", params, "same-action")
        b = await self.call("RunExperiment", params, "same-action")
        self.assertEqual(a, b)
        await self.world.workers[data(a)["experiment_id"]]
        self.assertEqual(self.runner.calls, 1)
        await self.world.close()
        self.world = World(self.settings, self.runner)
        self.assertEqual(a, await self.call("RunExperiment", params, "same-action"))
        self.assertFalse(self.world.workers)
        with self.assert_code("IDEMPOTENCY_CONFLICT"):
            await self.call("RunExperiment", {**params, "mode": "quick"}, "same-action")

    async def test_owner_isolation(self):
        job_id = await self.experiment()
        with self.assert_code("NOT_FOUND"):
            await self.call(
                "GetExperiment", {"experiment_id": job_id}, user="other-user"
            )
        with self.assert_code("FORBIDDEN"):
            await self.call("GetTask", {"task": "invsqrt"}, user="attacker")

    async def test_candidate_paths_and_utf8_byte_limits(self):
        for name in (
            "../evil.py",
            "/tmp/evil.py",
            "a/../evil.py",
            "a\\evil.py",
            "./evil.py",
            "a//evil.py",
        ):
            with self.subTest(name=name), self.assert_code("PATH_REJECTED"):
                await self.call(
                    "CreateCandidate",
                    {"task": "invsqrt", "files": {name: "pass"}, "entrypoint": name},
                )
        with self.assert_code("SOURCE_LIMIT"):
            await self.call(
                "CreateCandidate",
                {
                    "task": "invsqrt",
                    "files": {"main.py": "量" * 23000},
                    "entrypoint": "main.py",
                },
            )
        with self.assert_code("PATH_REJECTED"):
            await self.call(
                "CreateCandidate",
                {
                    "task": "invsqrt",
                    "files": {"main.py": "", "main.py/x": ""},
                    "entrypoint": "main.py",
                },
            )

    async def test_success_outcome_is_accepted_by_pinned_sdk(self):
        result = await self.call("GetTask", {"task": "invsqrt"})
        validate_compact_content("outcome", result)
        self.assertEqual(set(result["data"]), {"text"})

    async def test_protocol_rejections_replay_exactly(self):
        endpoint = QuantumEnvironment(self.world)
        sent = []

        async def capture(sender, outcome):
            validate_compact_content("outcome", outcome)
            sent.append(outcome)

        endpoint.send_outcome = capture
        action = {
            "id": "rejected-once",
            "name": "GetTask",
            "params": {"userId": "attacker", "task": "invsqrt"},
        }
        await endpoint.on_action("agent", action)
        await endpoint.on_action("agent", action)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(sent[0]["status"], "rejected")
        with self.assertRaises(ProtocolConflictError):
            await endpoint.on_action(
                "agent", {**action, "params": {**action["params"], "task": "dqi"}}
            )

    async def test_quick_result_cannot_be_packaged(self):
        ids = [
            await self.experiment(t, "quick" if t == "invsqrt" else "full")
            for t in REFERENCE
        ]
        with self.assert_code("FULL_REQUIRED"):
            await self.call("BuildSubmission", {"experiments": ids})

    async def test_hash_change_blocks_packaging(self):
        ids = [await self.experiment(t) for t in REFERENCE]
        (
            self.settings.data_root / "experiments" / ids[0] / "input" / "invsqrt.kmb"
        ).write_bytes(b"changed")
        with self.assert_code("ARTIFACT_CHANGED"):
            await self.call("BuildSubmission", {"experiments": ids})

    async def test_package_layout_and_score(self):
        p = await self.build()
        self.assertAlmostEqual(p["local_score"], 2)
        self.assertEqual(p["status"], "awaiting_approval")
        with zipfile.ZipFile(
            self.settings.data_root / "packages" / p["id"] / "submission.zip"
        ) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "invsqrt.kmb",
                    "dqi.kmb",
                    "dfthcsos/angle_k_0.kmb",
                    "dfthcsos/submission.npz",
                },
            )

    async def test_no_approval_means_zero_kaggle_calls(self):
        p = await self.build()
        g = Gateway()
        with self.assert_code("APPROVAL_REQUIRED"):
            await submit(self.world, p["id"], g)
        self.assertEqual((g.lookups, g.uploads), (0, 0))

    async def test_private_upload_worker_also_checks_durable_approval(self):
        p = await self.build()
        archive = self.settings.data_root / "packages" / p["id"] / "submission.zip"
        with self.assertRaises(RuntimeError):
            check_upload_authorization(archive, self.settings.data_root, p["id"])
        approve(self.world, p["id"], "human", p["sha256"])
        # Approved alone is not enough: operator submit must consume it first.
        with self.assertRaises(RuntimeError):
            check_upload_authorization(archive, self.settings.data_root, p["id"])
        outer = self

        class CheckedGateway(Gateway):
            async def upload(inner, archive, folder):
                check_upload_authorization(archive, outer.settings.data_root, p["id"])
                return await super().upload(archive, folder)

        g = CheckedGateway()
        await submit(self.world, p["id"], g)
        self.assertEqual(g.uploads, 1)

    async def test_cli_cannot_approve_from_noninteractive_input(self):
        from quantum_world.cli import run

        p = await self.build()
        args = SimpleNamespace(
            config="unused", command="approve", package_id=p["id"], operator="human"
        )
        with (
            patch.object(Settings, "load", return_value=self.settings),
            patch("sys.stdin.isatty", return_value=False),
            patch("quantum_world.cli.show"),
        ):
            with self.assert_code("HUMAN_REQUIRED"):
                await run(args)
        self.assertEqual(package(self.world, p["id"])[1]["status"], "awaiting_approval")

    async def test_hash_change_during_live_score_lookup_prevents_upload(self):
        p = await self.build()
        approve(self.world, p["id"], "human", p["sha256"])
        archive = self.settings.data_root / "packages" / p["id"] / "submission.zip"

        class ChangedGateway(Gateway):
            async def best(inner):
                archive.write_bytes(b"changed during lookup")
                return 1

        g = ChangedGateway()
        with self.assert_code("HASH_CHANGED"):
            await submit(self.world, p["id"], g)
        self.assertEqual(g.uploads, 0)

    async def test_hash_specific_approval_and_single_use(self):
        p = await self.build()
        with self.assert_code("HASH_CHANGED"):
            approve(self.world, p["id"], "human", "wrong-hash")
        approve(self.world, p["id"], "human", p["sha256"])
        g = Gateway()
        submitted = await submit(self.world, p["id"], g)
        self.assertEqual(submitted["status"], "submitted")
        with self.assert_code("APPROVAL_REQUIRED"):
            await submit(self.world, p["id"], g)
        self.assertEqual(g.uploads, 1)

    async def test_approval_expiry_and_no_improvement_stop_before_upload(self):
        p = await self.build()
        approved = approve(self.world, p["id"], "human", p["sha256"])
        owner, _ = package(self.world, p["id"])
        approved["approval"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        self.world.store.put("package", owner, approved)
        g = Gateway()
        with self.assert_code("APPROVAL_EXPIRED"):
            await submit(self.world, p["id"], g)
        self.assertEqual(g.lookups, 0)
        approved["approval"]["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        self.world.store.put("package", owner, approved)
        g = Gateway(best=3)
        with self.assert_code("NO_IMPROVEMENT"):
            await submit(self.world, p["id"], g)
        self.assertEqual(g.uploads, 0)

    async def test_changed_archive_after_approval(self):
        p = await self.build()
        approve(self.world, p["id"], "human", p["sha256"])
        archive = self.settings.data_root / "packages" / p["id"] / "submission.zip"
        archive.write_bytes(b"changed")
        g = Gateway()
        with self.assert_code("HASH_CHANGED"):
            await submit(self.world, p["id"], g)
        self.assertEqual(g.lookups, 0)

    async def test_failed_upload_is_unknown_and_never_retries(self):
        p = await self.build()
        approve(self.world, p["id"], "human", p["sha256"])
        g = Gateway(fail=True)
        with self.assertRaises(OSError):
            await submit(self.world, p["id"], g)
        self.assertEqual(package(self.world, p["id"])[1]["status"], "unknown")
        with self.assert_code("APPROVAL_REQUIRED"):
            await submit(self.world, p["id"], g)
        self.assertEqual(g.uploads, 1)

    async def test_nonfinite_live_score_fails_closed(self):
        p = await self.build()
        approve(self.world, p["id"], "human", p["sha256"])
        g = Gateway(best=float("nan"))
        with self.assert_code("BEST_UNAVAILABLE"):
            await submit(self.world, p["id"], g)
        self.assertEqual(g.uploads, 0)

    async def test_cancel_running_and_not_yet_started(self):
        self.runner.release = asyncio.Event()
        first = data(
            await self.call("RunExperiment", {"task": "invsqrt", "mode": "full"})
        )["experiment_id"]
        await self.runner.started.wait()
        second = data(
            await self.call("RunExperiment", {"task": "dqi", "mode": "full"})
        )["experiment_id"]
        await self.call("CancelExperiment", {"experiment_id": second})
        await self.call("CancelExperiment", {"experiment_id": first})
        await asyncio.gather(*list(self.world.workers.values()), return_exceptions=True)
        for job_id in (first, second):
            self.assertEqual(
                data(await self.call("GetExperiment", {"experiment_id": job_id}))[
                    "status"
                ],
                "cancelled",
            )
        self.assertEqual(self.runner.calls, 1)

    async def test_restart_marks_stale_work_without_replay(self):
        owner = self.world.owner("agent", {"userId": "user"})
        self.world.store.put("experiment", owner, {"id": "stale", "status": "running"})
        await self.world.close()
        self.world = World(self.settings, self.runner)
        self.assertEqual(
            self.world.store.get("experiment", owner, "stale")["status"], "interrupted"
        )
        self.assertEqual(self.runner.calls, 0)

    async def test_admin_can_review_running_daemon_without_recovering_its_jobs(self):
        self.runner.release = asyncio.Event()
        job = data(
            await self.call("RunExperiment", {"task": "invsqrt", "mode": "full"})
        )["experiment_id"]
        await self.runner.started.wait()
        admin = World(self.settings, administrative=True)
        await admin.close()
        self.assertEqual(
            data(await self.call("GetExperiment", {"experiment_id": job}))["status"],
            "running",
        )
        with self.assert_code("ALREADY_RUNNING"):
            World(self.settings)


class AccountHistoryTests(unittest.TestCase):
    def row(self, ref, score=1, status="COMPLETE"):
        return SimpleNamespace(
            ref=ref, public_score=score, status=SimpleNamespace(name=status)
        )

    def test_older_page_best_is_not_lost(self):
        rows = [self.row(i) for i in range(100)]
        api = SimpleNamespace(
            competition_submissions=lambda name, page_number, page_size: rows
            if page_number == 1
            else [self.row(101, 30)]
        )
        self.assertEqual(account_best(api), 30)

    def test_pending_invalid_or_repeated_history_blocks_submission(self):
        for rows in (
            [self.row(1, None, "PENDING")],
            [self.row(1, float("nan"))],
            [self.row(1, None)],
            None,
            [self.row(1)] * 100,
        ):
            api = SimpleNamespace(competition_submissions=lambda *a, **k: rows)
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                account_best(api)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_interpreter_preserves_venv_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            link = root / "venv-python"
            link.symlink_to(sys.executable)
            settings = Settings(root, root, link, root)
            self.assertEqual(settings.worker_python, link)
            self.assertNotEqual(settings.worker_python, link.resolve())

    async def test_timeout_kills_entire_child_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            pid_file = folder / "child.pid"
            source = (
                'import subprocess,time,pathlib; p=subprocess.Popen(["'
                + sys.executable
                + '","-c","import time; time.sleep(30)"]); pathlib.Path("child.pid").write_text(str(p.pid)); time.sleep(30)'
            )
            with self.assertRaises(TimeoutError):
                await process(
                    [sys.executable, "-c", source],
                    folder,
                    folder / "log",
                    0.6,
                    dict(os.environ),
                )
            pid = int(pid_file.read_text())
            # The shell may briefly leave a zombie until init reaps it. It must
            # no longer be executing; ps state is stronger than kill(pid, 0).
            probe = await asyncio.create_subprocess_exec(
                "ps", "-o", "stat=", "-p", str(pid), stdout=asyncio.subprocess.PIPE
            )
            output, _ = await probe.communicate()
            self.assertTrue(
                not output.strip() or output.strip().startswith(b"Z"), output
            )

    async def test_verifier_environment_excludes_credentials(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ,
                {
                    "KAGGLE_API_TOKEN": "fake-unit-secret",
                    "HUB_CLIENT_TOKEN": "fake-unit-secret",
                },
            ),
        ):
            root = Path(temp)
            env = clean_env(Settings(root, root, Path(sys.executable), root))
            self.assertNotIn("KAGGLE_API_TOKEN", env)
            self.assertNotIn("HUB_CLIENT_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
