import asyncio
import unittest
from unittest.mock import patch

import test_world
from test_world import data
from quantum_world.invsqrt import OFFICIAL_SEED_SHA256
from quantum_world.store import WorldError, canonical

PARAMETERS = dict(delta_bits=13, coef_bits=17, square_bits=28, product_bits=27, result_bits=28)
VALIDATION = dict(module="invsqrt.verify_submission_invsqrt", verifier_sha256="a",
                  preflight_sha256="b", metric_sha256="c", max_workers=2)
SOURCE = {"type": "public", "url": "https://example.test/precision", "version": "3"}


def evidence(job, sha):
    job["provenance"] = {
        "seed": {"sha256": OFFICIAL_SEED_SHA256},
        "validation": {**VALIDATION, "mode": job["mode"],
                       "max_x": 4096 if job["mode"] == "quick" else None},
        "built_artifacts": {"invsqrt.kmb": sha},
        "verifier_report": {"status": "PASS", "circuit_sha256": sha},
    }


def passed(job, cost):
    return {"status": "PASS", "full": job["mode"] == "full", "diagnostic_cost": cost,
            "task_cost": cost if job["mode"] == "full" else None,
            "files": job["provenance"]["built_artifacts"]}


class OptimizationTests(unittest.IsolatedAsyncioTestCase):
    call = test_world.WorldTests.call

    async def asyncSetUp(self):
        await test_world.WorldTests.asyncSetUp(self)
        from dataclasses import replace
        from quantum_world.service import World
        await self.world.close()
        self.world = World(replace(self.settings, invsqrt_optimization={
            "baseline_full_id": "exp-base-full",
            "baseline_quick_id": "exp-base-quick",
            "baseline_parameters": PARAMETERS,
        }))
        self.addAsyncCleanup(self.cleanup)
        patcher = patch("quantum_world.optimization.validation_basis", return_value=VALIDATION)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = canonical(["agent", "user"])
        entry = data(await self.call("CreateCandidate", {
            "task": "invsqrt", "entrypoint": "gen.py", "files": {"gen.py": "# base\n"}
        }))
        for mode, cost in (("quick", 95), ("full", 100)):
            job = {"id": "exp-base-" + mode, "task": "invsqrt", "mode": mode,
                   "candidate_id": entry["candidate_id"], "status": "succeeded"}
            evidence(job, "base-sha")
            job["result"] = passed(job, cost)
            self.world.store.put("experiment", self.owner, job)
        self.world.research.query = lambda _, params: {"nodes": [{
            "id": params.get("node_id", "method-1"), "source": SOURCE,
            "title": "Official reference fixed-point precision"
        }]}
        self.written = []
        self.world.research.history = lambda user: list(self.written) if user == "user" else []
        self.world.research.write_result = lambda record: self.written.append(record)
        self.calls = []
        self.costs = {("quick", 12): 90, ("quick", 11): 89, ("full", 11): 98}
        self.release = asyncio.Event()
        self.release.set()
        self.world.runner.run = self.run_candidate

    async def cleanup(self):
        await self.world.close()
        self.temp.cleanup()

    async def run_candidate(self, job, candidate, path):
        value = candidate["precision"]["delta_bits"]
        self.calls.append((job["mode"], value))
        await self.release.wait()
        script = self.world.settings.data_root / "candidates" / candidate["id"] / "gen.py"
        code = script.read_text()
        self.assertIn("build_nr_circuit(10, prec=", code)
        self.assertIn("'delta_bits': " + str(value), code)
        self.assertNotIn("relative_error", code)
        outcome = self.costs[job["mode"], value]
        if isinstance(outcome, Exception):
            raise outcome
        evidence(job, f"sha-{value}")
        return passed(job, outcome)

    async def start(self, *, baseline="exp-base-full", changes=(12, 11),
                    method="method-1", action_id=None):
        return data(await self.call("StartInversqrtOptimization", {
            "baseline_full_id": baseline, "proposals": [
                {"method_node_id": method, "changes": {"delta_bits": value}} for value in changes
            ]
        }, action_id))

    async def read_method(self, user="user"):
        await self.call("GetResearchNode", {"task": "invsqrt", "node_id": "method-1"}, user=user)

    async def task(self, user="user"):
        return data(await self.call("GetTask", {"task": "invsqrt"}, user=user))

    async def finished(self, response):
        run_id = response["optimization_id"]
        for _ in range(400):
            row = self.world.store.get("optimization", self.owner, run_id)
            if row["status"] not in ("queued", "quick", "full"):
                return row
            await asyncio.sleep(.005)
        self.fail("Optimization did not finish")

    async def test_selects_best_comparable_quick_then_promotes_and_writes_results(self):
        self.assertEqual((await self.task())["baseline"]["full_id"], "exp-base-full")
        await self.read_method()
        queued = await self.start(action_id="retry")
        self.assertEqual(queued, await self.start(action_id="retry"))
        run = await self.finished(queued)
        self.assertEqual(self.calls, [("quick", 12), ("quick", 11), ("full", 11)])
        self.assertEqual(run["status"], "succeeded")
        self.assertTrue(run["comparison"]["improved"])
        updated = await self.task()
        self.assertEqual(updated["baseline"]["full_id"], run["full_id"])
        self.assertEqual(updated["baseline"]["parameters"]["delta_bits"], 11)
        self.assertEqual({row["mode"] for row in updated["history"]}, {"quick", "full"})
        self.assertEqual(len(self.written), 3)
        self.assertTrue(all(row["source"] == SOURCE for row in self.written))
        self.assertTrue(all(row["baseline_full_id"] == "exp-base-full" for row in self.written))
        self.assertNotIn("baseline", await self.task(user="other-user"))
        self.assertEqual((await self.task(user="other-user"))["history"], [])

    async def test_quick_failure_or_no_cost_reduction_never_starts_full(self):
        await self.read_method()
        self.costs["quick", 12] = TimeoutError()
        self.costs["quick", 11] = 95
        run = await self.finished(await self.start())
        self.assertEqual(self.calls, [("quick", 12), ("quick", 11)])
        self.assertEqual(run["status"], "no_improvement")
        self.assertEqual((await self.task())["baseline"]["full_id"], "exp-base-full")

    async def test_full_pass_without_reduction_does_not_promote(self):
        await self.read_method()
        self.costs["full", 11] = 101
        run = await self.finished(await self.start())
        self.assertEqual(run["status"], "no_improvement")
        self.assertFalse(run["comparison"]["improved"])
        self.assertEqual((await self.task())["baseline"]["full_id"], "exp-base-full")

    async def test_invalid_proposals_cross_user_evidence_and_stale_baseline_are_rejected(self):
        await self.read_method(user="other-user")
        with self.assertRaises(WorldError):
            await self.start(changes=(12,))
        await self.read_method()
        for changes in ((13,), (12, 12), (1,), (12, 11, 14, 15), (True,)):
            with self.subTest(changes=changes), self.assertRaises(WorldError):
                await self.start(changes=changes)
        with self.assertRaises(WorldError):
            await self.start(baseline="exp-wrong", changes=(12,))
        self.assertEqual(self.calls, [])

    async def test_one_active_cycle_and_history_blocks_duplicate_parameters(self):
        await self.read_method()
        self.release.clear()
        queued = await self.start()
        with self.assertRaises(WorldError):
            await self.start()
        self.release.set()
        run = await self.finished(queued)
        with self.assertRaises(WorldError):
            await self.start(baseline=run["full_id"], changes=(12,))
