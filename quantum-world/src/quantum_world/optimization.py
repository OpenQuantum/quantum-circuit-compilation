"""Bounded precision-only invsqrt optimization and durable research writeback."""

import asyncio
from hashlib import sha256
import json
import math
from pathlib import Path

from .invsqrt import (
    METRIC_PATH, OFFICIAL_REFERENCE_SHA256, OFFICIAL_SEED_SHA256,
    OFFICIAL_VERIFIER_SHA256, REFERENCE_PATH, VERIFIER_PATH, optional_digest,
)
from .runner import manifest
from .store import WorldError, canonical, identifier, now


PRECISION_FIELDS = ("delta_bits", "coef_bits", "square_bits", "product_bits", "result_bits")
OFFICIAL_PRECISION = dict(zip(PRECISION_FIELDS, (14, 17, 28, 28, 29)))
ACTIVE = {"queued", "quick", "full"}


def validation_basis(settings):
    root = settings.project_root
    verifier = optional_digest(root / VERIFIER_PATH)
    metric = optional_digest(root / METRIC_PATH)
    preflight = optional_digest(Path(__file__).with_name("preflight.py"))
    if (
        optional_digest(root / REFERENCE_PATH) != OFFICIAL_REFERENCE_SHA256
        or verifier != OFFICIAL_VERIFIER_SHA256
        or not metric
        or not preflight
    ):
        raise WorldError("BASELINE_UNAVAILABLE", "Official reference or verifier files could not be confirmed.")
    return {
        "module": "invsqrt.verify_submission_invsqrt",
        "verifier_sha256": verifier, "metric_sha256": metric,
        "preflight_sha256": preflight, "max_workers": settings.max_workers,
    }


def precision_key(parameters):
    if not isinstance(parameters, dict) or set(parameters) != set(PRECISION_FIELDS):
        return None
    if any(type(parameters[key]) is not int or not (
        1 <= parameters[key] <= 32 and abs(parameters[key] - OFFICIAL_PRECISION[key]) <= 4
    ) for key in PRECISION_FIELDS):
        return None
    return tuple(parameters[key] for key in PRECISION_FIELDS)


def comparable(job, mode, basis):
    result = job.get("result") or {}
    proof = job.get("provenance") or {}
    built = proof.get("built_artifacts") or {}
    report = proof.get("verifier_report") or {}
    cost = result.get("task_cost" if mode == "full" else "diagnostic_cost")
    expected = {**basis, "mode": mode, "max_x": 4096 if mode == "quick" else None}
    if (
        job.get("task") != "invsqrt" or job.get("mode") != mode
        or job.get("status") != "succeeded" or result.get("status") != "PASS"
        or result.get("full") is not (mode == "full")
        or type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0
        or proof.get("seed", {}).get("sha256") != OFFICIAL_SEED_SHA256
        or proof.get("validation") != expected or report.get("status") != "PASS"
        or not report.get("circuit_sha256")
        or len(built) != 1 or not set(built) <= {"invsqrt.kmb", "invsqrt.kmx"}
        or any(result.get("files", {}).get(name) != digest for name, digest in built.items())
    ):
        return None
    return cost, built, report["circuit_sha256"]


def candidate_code(parameters):
    return f"""import argparse
from pathlib import Path
from invsqrt.nr_reference_circuit import Precision, build_nr_circuit

parser = argparse.ArgumentParser()
parser.add_argument('--output-dir', required=True, type=Path)
args = parser.parse_args()
precision = Precision(**{parameters!r})
circuit = build_nr_circuit(10, prec=precision)
circuit.to_file(str(args.output_dir / 'invsqrt.kmb'), format='kmb')
"""


class InversqrtOptimization:
    def __init__(self, world):
        self.world = world
        self.settings = world.settings
        self.store = world.store
        self.tasks = {}
        self.sync_task = None
        self.sync_event = asyncio.Event()

    @staticmethod
    def baseline_id(owner):
        return "opt-baseline-" + sha256(owner.encode()).hexdigest()

    def baseline(self, owner, basis):
        config = self.settings.invsqrt_optimization
        if config is None:
            raise WorldError("OPTIMIZATION_UNAVAILABLE", "World has no invsqrt optimization configured.")
        try:
            saved = self.store.get("optimization_baseline", owner, self.baseline_id(owner))
        except WorldError:
            saved = {
                "full_id": config["baseline_full_id"], "quick_id": config["baseline_quick_id"],
                "parameters": config["baseline_parameters"],
            }
        try:
            full = self.store.get("experiment", owner, saved["full_id"])
            quick = self.store.get("experiment", owner, saved["quick_id"])
            candidate = self.store.get("candidate", owner, full.get("candidate_id"))
            checked_full = comparable(full, "full", basis)
            checked_quick = comparable(quick, "quick", basis)
            if (
                not checked_full or not checked_quick or checked_full[1:] != checked_quick[1:]
                or full["candidate_id"] != quick["candidate_id"]
                or candidate["task"] != "invsqrt" or not precision_key(saved["parameters"])
                or candidate.get("precision", saved["parameters"]) != saved["parameters"]
                or manifest(self.settings.data_root / "candidates" / candidate["id"]) != candidate["files"]
            ):
                raise ValueError
        except (WorldError, OSError, ValueError):
            raise WorldError(
                "BASELINE_UNAVAILABLE",
                "No matching owned quick/full PASS pair and immutable precision recipe for the current verifier.",
            ) from None
        return {
            "full_id": full["id"], "quick_id": quick["id"], "candidate_id": candidate["id"],
            "parameters": saved["parameters"], "task_cost": checked_full[0],
            "diagnostic_cost": checked_quick[0], "circuit_sha256": checked_full[2],
        }

    async def context(self, owner):
        context = {"history": []}
        self.ensure_sync_worker()
        try:
            basis = await asyncio.to_thread(validation_basis, self.settings)
            context["baseline"] = self.baseline(owner, basis)
        except WorldError as error:
            context["optimization_error"] = {"code": error.code, "message": error.message}
        local = [item["record"] for _, item in self.store.list("optimization_outbox", owner)]
        try:
            remote = await asyncio.to_thread(self.world.research.history, json.loads(owner)[1])
        except WorldError as error:
            remote = []
            context["history_error"] = {"code": error.code, "message": error.message}
        records = {record["experiment_id"]: record for record in [*remote, *local]}
        newest = sorted(records.values(), key=lambda record: record["finished_at"], reverse=True)[:8]
        fields = ("experiment_id", "method_node_id", "parameters", "changes", "mode",
                  "status", "diagnostic_cost", "task_cost", "baseline_full_id")
        context["history"] = [{key: record.get(key) for key in fields} for record in newest]
        return context

    def prepare(self, owner, params):
        if self.settings.invsqrt_optimization is None:
            raise WorldError("OPTIMIZATION_UNAVAILABLE", "World has no invsqrt optimization configured.")
        if any(row["status"] in ACTIVE for _, row in self.store.list("optimization", owner)):
            raise WorldError("OPTIMIZATION_ACTIVE", "This caller already has an active optimization.")
        basis = validation_basis(self.settings)
        baseline = self.baseline(owner, basis)
        if baseline["full_id"] != params["baseline_full_id"]:
            raise WorldError("BASELINE_CHANGED", "Read GetTask for the current owned baseline.")
        seen = {precision_key(baseline["parameters"])}
        seen.update(
            precision_key(item["record"]["parameters"])
            for _, item in self.store.list("optimization_outbox", owner)
        )
        proposals = []
        for proposal in params["proposals"]:
            node_id = proposal["method_node_id"]
            node = self.store.research_node(owner, node_id)
            source = node.get("source") if node else None
            method_text = " ".join(str(node.get(key, "")) for key in
                                   ("title", "summary", "explanation")) if node else ""
            if (
                not isinstance(source, dict) or source.get("type") not in ("public", "official")
                or not any(term in method_text.casefold()
                           for term in ("precision", "fixed-point", "bit width", "bit-width",
                                        "\u7cbe\u5ea6", "\u4f4d\u5bbd"))
            ):
                raise WorldError("METHOD_EVIDENCE_REQUIRED", "Read the public or official invsqrt method with GetResearchNode.")
            changes = proposal["changes"]
            parameters = {**baseline["parameters"], **changes}
            key = precision_key(parameters)
            if (
                key is None or key in seen
                or any(not 1 <= abs(parameters[name] - baseline["parameters"][name]) <= 2
                       for name in changes)
            ):
                raise WorldError(
                    "PRECISION_PROPOSAL_REJECTED",
                    "Use new precision settings within two bits per changed field and four bits of official defaults.",
                )
            seen.add(key)
            proposals.append({
                "method_node_id": node_id, "changes": changes, "parameters": parameters,
                "source": {key: source[key] for key in ("type", "url", "label", "version") if key in source},
            })
        run = {
            "id": identifier("opt"), "task": "invsqrt", "status": "queued",
            "baseline": baseline, "attempts": [], "proposals": proposals,
            "validation_basis": basis, "created_at": now(),
        }
        self.store.put("optimization", owner, run)
        return run

    def start(self, owner, run):
        self.tasks[run["id"]] = asyncio.create_task(self.run(owner, run))
        self.ensure_sync_worker()

    async def experiment(self, owner, run, proposal, *, quick=None):
        mode = "quick" if quick is None else "full"
        with self.store.transaction():
            if quick is None:
                saved = self.world._candidate(owner, {
                    "task": "invsqrt", "entrypoint": "gen.py",
                    "description": "Official invsqrt reference with a fixed precision recipe.",
                    "files": {"gen.py": candidate_code(proposal["parameters"])},
                })
                candidate_id = saved["candidate_id"]
                candidate = self.store.get("candidate", owner, candidate_id)
                candidate.update(precision=proposal["parameters"], research=proposal)
                self.store.put("candidate", owner, candidate)
            else:
                candidate_id = quick["candidate_id"]
            job, candidate = self.world._queue_experiment(
                owner, {"task": "invsqrt", "mode": mode, "candidate_id": candidate_id},
                optimization_id=run["id"],
            )
            run["status"] = mode
            if quick is None:
                run["attempts"].append({
                    "quick_id": job["id"], "candidate_id": candidate_id,
                    "method_node_id": proposal["method_node_id"], "changes": proposal["changes"],
                })
            else:
                run["full_id"] = job["id"]
            self.save_sync_status(owner, run)
        worker = asyncio.create_task(self.world._execute(owner, job, candidate))
        self.world.workers[job["id"]] = worker
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            if not worker.cancelled():
                raise
            with self.store.transaction():
                job = self.store.get("experiment", owner, job["id"])
                job.update(status="cancelled", finished_at=now(), error={
                    "code": "CANCELLED", "message": "Experiment was cancelled before starting."
                })
                self.store.put("experiment", owner, job)
                self.enqueue_result(owner, job, candidate)
            self.world.workers.pop(job["id"], None)
        return self.store.get("experiment", owner, job["id"])

    async def run(self, owner, run):
        status = "no_improvement"
        baseline, basis = run["baseline"], run["validation_basis"]
        try:
            eligible = []
            for proposal in run["proposals"]:
                quick = await self.experiment(owner, run, proposal)
                checked = comparable(quick, "quick", basis)
                if quick["status"] == "cancelled":
                    raise WorldError("CANCELLED", "Current experiment was cancelled.")
                if checked and checked[2] != baseline["circuit_sha256"] and checked[0] < baseline["diagnostic_cost"]:
                    eligible.append((checked, quick))
            if eligible:
                quick_evidence, quick = min(eligible, key=lambda item: item[0][0])
                full = await self.experiment(owner, run, None, quick=quick)
                full_evidence = comparable(full, "full", basis)
                if not full_evidence or full_evidence[1:] != quick_evidence[1:]:
                    reason = full.get("error") or {
                        "code": "FULL_NOT_COMPARABLE", "message": "Full did not verify the selected quick circuit."
                    }
                    raise WorldError(reason["code"], reason["message"])
                new_cost = full_evidence[0]
                run["comparison"] = {
                    "baseline_cost": baseline["task_cost"], "task_cost": new_cost,
                    "reduction_pct": (baseline["task_cost"] - new_cost) / baseline["task_cost"] * 100,
                    "improved": new_cost < baseline["task_cost"],
                }
                if run["comparison"]["improved"]:
                    status = "succeeded"
            else:
                run["error"] = {
                    "code": "NO_QUALIFYING_QUICK",
                    "message": "No quick experiment passed on a changed circuit with lower diagnostic cost.",
                }
        except asyncio.CancelledError:
            status = "interrupted"
            run["error"] = {"code": "INTERRUPTED", "message": "World stopped during optimization."}
        except WorldError as error:
            status = "failed"
            run["error"] = {"code": error.code, "message": error.message}
        except Exception as error:
            status = "failed"
            run["error"] = {"code": "OPTIMIZATION_ERROR", "message": type(error).__name__}
        finally:
            run.update(status=status, finished_at=now())
            with self.store.transaction():
                if status == "succeeded":
                    candidate = self.store.get("candidate", owner, full["candidate_id"])
                    self.store.put("optimization_baseline", owner, {
                        "id": self.baseline_id(owner), "full_id": full["id"],
                        "quick_id": quick["id"], "parameters": candidate["precision"],
                    })
                self.save_sync_status(owner, run)
            self.tasks.pop(run["id"], None)

    def enqueue_result(self, owner, job, candidate):
        optimization_id = job["optimization_id"]
        run = self.store.get("optimization", owner, optimization_id)
        proposal = candidate["research"]
        result = job.get("result") or {}
        diagnostic_cost = result.get("diagnostic_cost")
        if job["mode"] == "full" and diagnostic_cost is None:
            diagnostic_cost = result.get("task_cost")
        record = {
            "world_id": self.settings.world_id, "user_id": json.loads(owner)[1], "task": "invsqrt",
            "experiment_id": job["id"], "optimization_id": optimization_id,
            "method_node_id": proposal["method_node_id"], "candidate_id": candidate["id"],
            "source": proposal["source"], "parameters": candidate["precision"], "changes": proposal["changes"],
            "baseline_full_id": run["baseline"]["full_id"], "baseline_quick_id": run["baseline"]["quick_id"],
            "mode": job["mode"], "status": job["status"], "diagnostic_cost": diagnostic_cost,
            "task_cost": result.get("task_cost"), "provenance": job.get("provenance"),
            "error": job.get("error"), "finished_at": job["finished_at"],
        }
        self.store.put("optimization_outbox", owner, {
            "id": "sync-" + job["id"], "status": "pending", "record": record,
        })
        run["sync_status"] = "pending"
        self.store.put("optimization", owner, run)
        self.sync_event.set()

    def save_sync_status(self, owner, run):
        records = [row for _, row in self.store.list("optimization_outbox", owner)
                   if row["record"]["optimization_id"] == run["id"]]
        if records:
            run["sync_status"] = "synced" if all(row["status"] == "synced" for row in records) else "pending"
        self.store.put("optimization", owner, run)

    def ensure_sync_worker(self):
        if self.sync_task is None:
            self.sync_task = asyncio.create_task(self.sync())

    async def sync(self):
        while True:
            self.sync_event.clear()
            retry = False
            for owner, row in self.store.list("optimization_outbox"):
                if row["status"] != "pending":
                    continue
                try:
                    await asyncio.to_thread(self.world.research.write_result, row["record"])
                except (OSError, WorldError):
                    retry = True
                    continue
                with self.store.transaction():
                    row["status"] = "synced"
                    self.store.put("optimization_outbox", owner, row)
                    run = self.store.get("optimization", owner, row["record"]["optimization_id"])
                    self.save_sync_status(owner, run)
            try:
                await asyncio.wait_for(self.sync_event.wait(), 5 if retry else None)
            except TimeoutError:
                pass

    def recover(self):
        with self.store.transaction():
            for owner, run in self.store.list("optimization"):
                if run["status"] in ACTIVE:
                    run.update(status="interrupted", finished_at=now(), error={
                        "code": "INTERRUPTED", "message": "World restarted before optimization completed.",
                    })
                    self.store.put("optimization", owner, run)
            for owner, job in self.store.list("experiment"):
                if job.get("optimization_id") and job["status"] in ("interrupted", "cancelled"):
                    try:
                        self.store.get("optimization_outbox", owner, "sync-" + job["id"])
                    except WorldError:
                        candidate = self.store.get("candidate", owner, job["candidate_id"])
                        self.enqueue_result(owner, job, candidate)

    async def close(self):
        tasks = list(self.tasks.values())
        if self.sync_task:
            tasks.append(self.sync_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
