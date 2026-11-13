import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from quantum_world.config import Settings
from quantum_world.service import World
from quantum_world.store import WorldError, canonical


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class InvsqrtEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / "official/data/sample_submission/invsqrt.kmb"
        self.seed.parent.mkdir(parents=True)
        self.seed.write_bytes(b"test seed")
        self.source = self.root / "official/data/verifiers/invsqrt"
        self.source.mkdir(parents=True)
        (self.source / "nr_reference_circuit.py").write_text("# test source\n")
        self.verifier = self.source / "verify_submission_invsqrt.py"
        self.verifier.write_text("# test verifier\n")
        self.metric = self.root / "official/metric/google_quantum_ai_metric.py"
        self.metric.parent.mkdir()
        self.metric.write_text("# test metric\n")
        settings = Settings(self.root, self.root / "state", Path("/unused"), self.seed.parent,
                            allowed_agents={"agent": ["user"]})
        self.world = World(settings)
        self.addAsyncCleanup(self.world.close)
        self.action = 0

    async def call(self, tool, **params):
        self.action += 1
        response = await self.world.call("agent", f"action-{self.action}", tool, {"userId": "user", **params})
        return json.loads(response["data"]["text"])

    async def test_get_task_identifies_configured_seed_by_bytes_not_directory_name(self):
        task = await self.call("GetTask", task="invsqrt")
        self.assertEqual(task["seed"], {"filename": "invsqrt.kmb", "sha256": sha256(b"test seed"),
                                       "source": "configured_seed"})
        reference = task["official_reference"]
        self.assertEqual(reference["module"], "invsqrt.nr_reference_circuit")
        self.assertEqual(reference["source_status"], "hash_mismatch")
        self.assertIn("Precision", reference["usage"])
        self.assertIn("build_nr_circuit", reference["usage"])
        self.assertIn("Toffoli", reference["cost_formula"])
        self.assertNotIn(str(self.root), json.dumps(task))

    async def test_get_task_distinguishes_missing_seed_and_reference(self):
        self.seed.unlink()
        (self.source / "nr_reference_circuit.py").unlink()
        task = await self.call("GetTask", task="invsqrt")
        self.assertEqual(task["seed"], {"filename": None, "sha256": None, "source": "unavailable"})
        self.assertEqual(task["official_reference"]["source_status"], "unavailable")

    async def test_agent_can_read_a_bounded_range_of_the_verified_reference(self):
        reference = self.source / "nr_reference_circuit.py"
        content = "".join(f"# reference line {line}\n" for line in range(1, 100))
        reference.write_text(content, encoding="utf-8")
        with patch("quantum_world.invsqrt.OFFICIAL_REFERENCE_SHA256", sha256(reference.read_bytes())):
            report = await self.call("ReadInversqrtReference", start_line=2, lines=3)
            self.assertEqual(report["source"], "invsqrt.nr_reference_circuit")
            self.assertEqual(report["start_line"], 2)
            self.assertEqual(report["end_line"], 4)
            self.assertEqual(report["next_line"], 5)
            self.assertEqual(report["text"], "".join(content.splitlines(keepends=True)[1:4]))
            self.assertEqual(report["sha256"], sha256(reference.read_bytes()))
        with self.assertRaisesRegex(WorldError, "verified official reference"):
            await self.call("ReadInversqrtReference", start_line=2, lines=3)
        with self.assertRaisesRegex(WorldError, "tool schema"):
            await self.call("ReadInversqrtReference", path="/etc/passwd")

    async def test_official_seed_classification_requires_the_manifest_digest(self):
        # Replace the known official hash with that of this small fixture. The
        # real pinned digest is checked against the archive in deployment.
        with patch("quantum_world.invsqrt.OFFICIAL_SEED_SHA256", sha256(b"test seed")):
            task = await self.call("GetTask", task="invsqrt")
            self.assertEqual(task["seed"]["source"], "official_sample_submission")
            self.seed.write_bytes(b"changed seed")
            changed = await self.call("GetTask", task="invsqrt")
            self.assertEqual(changed["seed"]["source"], "configured_seed")

    async def test_full_baseline_uses_official_defaults_and_preserves_failure_evidence(self):
        async def fake_process(command, directory, log, timeout, env, monitor=None):
            if log.name == "preflight":
                circuit = directory / "input/invsqrt.kmb"
                log.with_suffix(".stdout").write_text(json.dumps({"input": str(circuit)}))
                return 0
            self.assertNotIn("--max-x", command)
            log.with_suffix(".stdout").write_text(json.dumps({
                "status": "FAIL", "error": "test verification failed", "num_qubits": 22,
            }))
            return 1

        with (
            patch("quantum_world.invsqrt.OFFICIAL_SEED_SHA256", sha256(b"test seed")),
            patch("quantum_world.runner.process", fake_process),
            patch("quantum_world.runner.shutil.disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)),
        ):
            queued = await self.call("RunExperiment", task="invsqrt", mode="full")
            ident = queued["experiment_id"]
            await self.world.workers[ident]

        report = await self.call("GetExperiment", experiment_id=ident)
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["result"])
        self.assertEqual(report["error"]["code"], "VERIFICATION_FAILED")
        evidence = report["provenance"]
        self.assertEqual(evidence["input_kind"], "official_baseline")
        self.assertIsNone(evidence["candidate"])
        self.assertEqual(evidence["validation"]["mode"], "full")
        self.assertIsNone(evidence["validation"]["max_x"])
        self.assertEqual(evidence["built_artifacts"], {"invsqrt.kmb": sha256(b"test seed")})
        self.assertEqual(evidence["verifier_report"], {"status": "FAIL", "num_qubits": 22})

    async def test_completed_candidate_keeps_seed_source_and_output_hashes_separate(self):
        source_text = "# inert candidate fixture\n"
        candidate = await self.call(
            "CreateCandidate", task="invsqrt", entrypoint="main.py", files={"main.py": source_text}
        )

        async def fake_process(command, directory, log, timeout, env, monitor=None):
            circuit = directory / "input/invsqrt.kmb"
            if log.name == "generator":
                self.assertEqual(circuit.read_bytes(), b"test seed")
                circuit.write_bytes(b"candidate output")
                self.seed.write_bytes(b"operator replaced original seed")
            elif log.name == "preflight":
                log.with_suffix(".stdout").write_text(json.dumps({"input": str(circuit)}))
            else:
                self.assertIn("--max-x", command)
                self.assertEqual(command[command.index("--max-x") + 1], "4096")
                log.with_suffix(".stdout").write_text(json.dumps({
                    "status": "PASS", "score": 333.0, "num_qubits": 22, "toffoli_count": 12.0,
                    "verified_shots": 262144, "worst_error_over_tolerance": 0.5,
                    "circuit_sha256": "a" * 64, "unbounded_unknown": "x" * 25000,
                }))
            return 0

        with (
            patch("quantum_world.runner.process", fake_process),
            patch("quantum_world.runner.shutil.disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)),
            patch("quantum_world.runner.asyncio.create_subprocess_exec") as cleanup,
        ):
            cleanup.return_value.wait = AsyncMock(return_value=0)
            queued = await self.call(
                "RunExperiment", task="invsqrt", mode="quick", candidate_id=candidate["candidate_id"]
            )
            ident = queued["experiment_id"]
            await self.world.workers[ident]

        report = await self.call("GetExperiment", experiment_id=ident)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["result"]["diagnostic_cost"], 333.0)
        self.assertIsNone(report["result"]["task_cost"])
        self.assertTrue(report["salt"].startswith("qw-independent-"))
        evidence = report["provenance"]
        self.assertEqual(evidence["input_kind"], "candidate")
        self.assertEqual(evidence["seed"]["sha256"], sha256(b"test seed"))
        self.assertEqual(evidence["candidate"], {"id": candidate["candidate_id"], "entrypoint": "main.py",
                                               "files": {"main.py": sha256(source_text.encode())}})
        self.assertEqual(evidence["validation"]["mode"], "quick")
        self.assertEqual(evidence["validation"]["max_x"], 4096)
        self.assertEqual(evidence["validation"]["verifier_sha256"], sha256(self.verifier.read_bytes()))
        self.assertEqual(evidence["validation"]["metric_sha256"], sha256(self.metric.read_bytes()))
        self.assertEqual(evidence["built_artifacts"], {"invsqrt.kmb": sha256(b"candidate output")})
        self.assertEqual(evidence["verifier_report"]["circuit_sha256"], "a" * 64)
        self.assertEqual(evidence["verifier_report"]["worst_error_over_tolerance"], 0.5)
        self.assertNotIn("unbounded_unknown", evidence["verifier_report"])
        saved = self.world.store.get("experiment", canonical(["agent", "user"]), ident)
        self.assertEqual(saved["provenance"], evidence)
        self.assertEqual(saved["result"]["files"], evidence["built_artifacts"])

    async def test_legacy_report_without_provenance_remains_readable(self):
        ident = "exp-legacy"
        self.world.store.put("experiment", canonical(["agent", "user"]), {
            "id": ident, "task": "invsqrt", "mode": "quick", "status": "succeeded",
            "result": {"diagnostic_cost": 22.0, "task_cost": None, "files": {"invsqrt.kmb": "old"}},
        })
        report = await self.call("GetExperiment", experiment_id=ident)
        self.assertEqual(report["result"]["diagnostic_cost"], 22.0)
        self.assertNotIn("provenance", report)
        self.assertNotIn("files", report["result"])
