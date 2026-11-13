"""Durable business service, independent of Hub transport and model providers."""

import asyncio
import json
import math
from pathlib import PurePosixPath
import zipfile
import fcntl
import hashlib

from .runner import OfficialRunner, digest, manifest
from .store import Store, WorldError, canonical, identifier, now
from .tools import VALIDATORS
from .research import ResearchClient

REFERENCE = {
    "invsqrt": 2076774.233715282,
    "dqi": 24439463583672.93,
    "dfthcsos": 3355599588.3155375,
}

RESEARCH_V1 = {
    "SearchResearchSolutions",
    "StartResearchSourceDownload",
    "GetResearchDownloadJob",
    "GetResearchSnapshotStructure",
    "ReadResearchSnapshotSource",
    "ImportResearchSnapshot",
}


def research_job_id(owner, remote_id):
    return "rjob-" + hashlib.sha256(canonical([owner, remote_id]).encode()).hexdigest()


class World:
    def __init__(self, settings, runner=None, administrative=False):
        self.settings = settings
        self.administrative = administrative
        settings.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (
            settings.data_root / ("admin.lock" if administrative else "world.lock")
        ).open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise WorldError(
                "ALREADY_RUNNING", "Another World owns this data directory."
            ) from None
        self.store = Store(settings.data_root / "world.sqlite3")
        if not administrative:
            self.store.recover()
        self.runner = runner or OfficialRunner(settings)
        self.research = ResearchClient(settings)
        self.workers = {}
        self.capacity = asyncio.Semaphore(1)
        self.research_lock = asyncio.Lock()

    def owner(self, sender, params):
        if params.get("userId") not in self.settings.allowed_agents.get(sender, []):
            raise WorldError(
                "FORBIDDEN", "Caller/user is not allowed by this World deployment."
            )
        return canonical([sender, params["userId"]])

    async def call(self, sender, action_id, name, params):
        if name in RESEARCH_V1:
            async with self.research_lock:
                return await self._call(sender, action_id, name, params)
        return await self._call(sender, action_id, name, params)

    async def _call(self, sender, action_id, name, params):
        if self.administrative:
            raise WorldError(
                "ADMIN_ONLY", "Administrative session cannot execute agent actions"
            )
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 256:
            raise WorldError("INVALID_ACTION", "Action id required")
        if not isinstance(params, dict):
            raise WorldError("INVALID_ARGUMENTS", "Arguments must be an object")
        owner = self.owner(sender, params)
        if name not in VALIDATORS:
            raise WorldError("UNKNOWN_TOOL", "No such agent-facing tool")
        errors = list(VALIDATORS[name].iter_errors(params))
        if errors:
            raise WorldError(
                "INVALID_ARGUMENTS", "Arguments do not match the tool schema."
            )
        request = canonical({"name": name, "params": params})
        previous = self.store.action(owner, action_id)
        if previous:
            if previous[0] != request:
                raise WorldError(
                    "IDEMPOTENCY_CONFLICT", "Action id reused with different arguments"
                )
            return previous[1]
        schedule = None
        cancel = None
        # External I/O is outside SQLite transactions and the event loop.
        # Replays return the saved observation instead of refetching changing data.
        research_data = None
        research_record = None
        if name in (
            "SearchResearchMethods",
            "GetResearchNode",
        ) and not self.store.action(owner, action_id):
            research_data = await asyncio.to_thread(self.research.query, name, params)
        if name in RESEARCH_V1:
            job = None
            if name not in ("SearchResearchSolutions", "StartResearchSourceDownload"):
                job = self.store.get(
                    "research_job", owner, research_job_id(owner, params["job_id"])
                )
            if name in (
                "GetResearchSnapshotStructure",
                "ReadResearchSnapshotSource",
                "ImportResearchSnapshot",
            ):
                if job["status"] not in ("succeeded", "partial") or not job.get(
                    "snapshot_id"
                ):
                    raise WorldError(
                        "RESEARCH_NOT_READY", "Research snapshot is not ready"
                    )
            if name == "ImportResearchSnapshot":
                import_id = (
                    "research-"
                    + hashlib.sha256(canonical([owner, action_id]).encode()).hexdigest()
                )
                directory = self.settings.data_root / "research-imports" / import_id
                manifest_data = await asyncio.to_thread(
                    self.research.import_snapshot, job["snapshot_id"], directory, job
                )
                research_data = {
                    "import_id": import_id,
                    "snapshot_id": job["snapshot_id"],
                    "task": job["task"],
                    "file_count": len(manifest_data["files"]),
                    "executed": False,
                }
                research_record = (
                    "research_import",
                    {
                        "id": import_id,
                        **research_data,
                        "directory": str(directory / "files"),
                        "manifest": manifest_data,
                    },
                )
            else:
                key = hashlib.sha256(canonical([owner, action_id]).encode()).hexdigest()
                research_data = await asyncio.to_thread(
                    self.research.v1,
                    name,
                    params,
                    key=key,
                    snapshot_id=job.get("snapshot_id") if job else None,
                )
                if name in ("StartResearchSourceDownload", "GetResearchDownloadJob"):
                    remote = research_data["job"]
                    if job and any(
                        remote.get(k) != job.get(k)
                        for k in ("source_id", "source_version", "task")
                    ):
                        raise WorldError(
                            "RESEARCH_INVALID_RESPONSE", "Research job identity changed"
                        )
                    if (
                        job
                        and job.get("snapshot_id")
                        and remote.get("snapshot_id") != job["snapshot_id"]
                    ):
                        raise WorldError(
                            "RESEARCH_INVALID_RESPONSE",
                            "Research snapshot identity changed",
                        )
                    research_record = (
                        "research_job",
                        {**remote, "id": research_job_id(owner, remote["job_id"])},
                    )
        with self.store.transaction():
            previous = self.store.action(owner, action_id)
            if previous:
                if previous[0] != request:
                    raise WorldError(
                        "IDEMPOTENCY_CONFLICT",
                        "Action id reused with different arguments",
                    )
                return previous[1]
            if (
                name in ("SearchResearchMethods", "GetResearchNode")
                or name in RESEARCH_V1
            ):
                data = research_data
                if research_record:
                    self.store.put(research_record[0], owner, research_record[1])
            elif name == "GetTask":
                data = {
                    "task": params["task"],
                    "reference_cost": REFERENCE[params["task"]],
                    "configured_best": self.settings.current_best,
                    "comparison_basis": "operator-configured; live score rechecked before upload",
                    "quick_is_submission_eligible": False,
                    "approval_required": True,
                    "generator_contract": "Python entrypoint receives --output-dir /out. Seed task files are already there. Write invsqrt.kmb/kmx, dqi.kmb/kmx, or consecutive angle_k_*.kmb/kmx and submission.npz.",
                    "constraints": {
                        "invsqrt": "22-qubit named x register restored, scratch cleaned, phase tolerance 2^-24/sqrt(x)",
                        "dqi": "Official RS(4095,4025), GF(2^12); syndrome restored, scratch cleaned, phase verified",
                        "dfthcsos": "Official shape/angle contract, ANGLE <=128 qubits, coefficient errors <=0.020 Ha",
                    }[params["task"]],
                }
            elif name == "CreateCandidate":
                data = self._candidate(owner, params)
            elif name == "RunExperiment":
                if (
                    sum(
                        j["status"] in ("queued", "running")
                        for _, j in self.store.list("experiment")
                    )
                    >= 8
                ):
                    raise WorldError(
                        "QUEUE_FULL", "At most 8 outstanding experiments allowed"
                    )
                candidate = None
                if params.get("candidate_id"):
                    candidate = self.store.get(
                        "candidate", owner, params["candidate_id"]
                    )
                    if candidate["task"] != params["task"]:
                        raise WorldError(
                            "TASK_MISMATCH", "Candidate belongs to another task"
                        )
                job = {
                    "id": identifier("exp"),
                    "task": params["task"],
                    "mode": params["mode"],
                    "candidate_id": candidate["id"] if candidate else None,
                    "status": "queued",
                    "created_at": now(),
                    "salt": "qw-independent-" + identifier("draw"),
                    "result": None,
                    "error": None,
                }
                self.store.put("experiment", owner, job)
                schedule = (owner, job, candidate)
                data = {
                    "experiment_id": job["id"],
                    "status": "queued",
                    "full_verification_complete": False,
                }
            elif name == "GetExperiment":
                job = self.store.get("experiment", owner, params["experiment_id"])
                data = dict(job)
                if data.get("result"):
                    data["result"] = {
                        k: v for k, v in data["result"].items() if k != "files"
                    }
                directory = self.settings.data_root / "experiments" / job["id"]
                data["logs"] = {}
                for log in (
                    "generator.stderr",
                    "generator.stdout",
                    "preflight.stdout",
                    "verify.stderr",
                ):
                    path = directory / log
                    if path.exists():
                        with path.open("rb") as stream:
                            stream.seek(max(0, path.stat().st_size - 2000))
                            data["logs"][log] = stream.read(2000).decode(
                                "utf-8", "replace"
                            )
            elif name == "CancelExperiment":
                job = self.store.get("experiment", owner, params["experiment_id"])
                if job["status"] in ("queued", "running"):
                    job["cancel_requested"] = True
                    cancel = job["id"]
                    if job["status"] == "queued":
                        job.update(status="cancelled", finished_at=now())
                    self.store.put("experiment", owner, job)
                data = {
                    "experiment_id": job["id"],
                    "status": (
                        "cancelling"
                        if cancel and job["status"] == "running"
                        else job["status"]
                    ),
                }
            elif name == "BuildSubmission":
                data = self._package(owner, params["experiments"])
            outcome = {
                "id": identifier("outcome"),
                "refs": [action_id],
                "status": "completed",
                "data": {"text": json.dumps(data, ensure_ascii=False, allow_nan=False)},
            }
            if (
                len(outcome["data"]["text"]) > 24000
                or len(canonical(outcome).encode()) > 60000
            ):
                raise WorldError(
                    "RESULT_TOO_LARGE", "Result exceeds the protocol budget"
                )
            self.store.save_action(owner, action_id, request, outcome)
        if schedule:
            self.workers[schedule[1]["id"]] = asyncio.create_task(
                self._execute(*schedule)
            )
        if cancel and cancel in self.workers:
            self.workers[cancel].cancel()
            if data["status"] == "cancelled":
                self.workers.pop(cancel, None)
        return outcome

    def _candidate(self, owner, params):
        files = params["files"]
        entry = params["entrypoint"]
        if entry not in files or not entry.endswith(".py"):
            raise WorldError(
                "INVALID_ENTRYPOINT", "Entrypoint must be a supplied Python file"
            )
        if sum(len(s.encode()) for s in files.values()) > 256 * 1024:
            raise WorldError("SOURCE_LIMIT", "Total source limit is 256 KiB")
        for name, content in files.items():
            parts = PurePosixPath(name).parts
            if (
                not parts
                or len(name) > 240
                or name != PurePosixPath(name).as_posix()
                or name.startswith("/")
                or ".." in parts
                or "\\" in name
                or any(ord(c) < 32 for c in name)
            ):
                raise WorldError("PATH_REJECTED", "Use confined relative source paths")
            if any(
                PurePosixPath(*parts[:i]).as_posix() in files
                for i in range(1, len(parts))
            ):
                raise WorldError(
                    "PATH_REJECTED", "A source path cannot also be a directory"
                )
            if len(content.encode()) > 65536:
                raise WorldError("SOURCE_LIMIT", "Per-file source limit is 64 KiB")
        candidate = {
            "id": identifier("cand"),
            "task": params["task"],
            "entrypoint": entry,
            "description": params.get("description", ""),
            "created_at": now(),
        }
        folder = self.settings.data_root / "candidates" / candidate["id"]
        folder.mkdir(parents=True)
        for name, content in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        candidate["files"] = manifest(folder)
        self.store.put("candidate", owner, candidate)
        return {
            "candidate_id": candidate["id"],
            "task": candidate["task"],
            "files": candidate["files"],
        }

    async def _execute(self, owner, job, candidate):
        job_id = job["id"]
        try:
            async with self.capacity:
                directory = self.settings.data_root / "experiments" / job_id
                directory.mkdir(parents=True)
                job.update(status="running", started_at=now())
                self.store.put("experiment", owner, job)
                result = await self.runner.run(job, candidate, directory)
                job.update(status="succeeded", result=result)
        except asyncio.CancelledError:
            job.update(
                status="cancelled",
                error={"code": "CANCELLED", "message": "Worker has stopped."},
            )
        except TimeoutError:
            job.update(
                status="failed",
                error={
                    "code": "TIMEOUT",
                    "message": "Worker exceeded the configured timeout.",
                },
            )
        except WorldError as error:
            job.update(
                status="failed", error={"code": error.code, "message": error.message}
            )
        except Exception as error:
            job.update(
                status="failed",
                error={"code": "WORKER_ERROR", "message": type(error).__name__},
            )
        finally:
            job["finished_at"] = now()
            self.store.put("experiment", owner, job)
            self.workers.pop(job_id, None)

    def _package(self, owner, experiment_ids):
        jobs = [self.store.get("experiment", owner, i) for i in experiment_ids]
        if {j["task"] for j in jobs} != set(REFERENCE):
            raise WorldError("TASK_SET", "One experiment per benchmark is required")
        for j in jobs:
            if (
                j["status"] != "succeeded"
                or j["mode"] != "full"
                or not j["result"]["full"]
            ):
                raise WorldError(
                    "FULL_REQUIRED", "Only successful full experiments are eligible"
                )
            folder = self.settings.data_root / "experiments" / j["id"] / "input"
            if manifest(folder) != j["result"]["files"]:
                raise WorldError("ARTIFACT_CHANGED", "Verified artifact hashes changed")
        score = math.prod(
            REFERENCE[j["task"]] / j["result"]["task_cost"] for j in jobs
        ) ** (1 / 3)
        if not math.isfinite(score) or score <= self.settings.current_best:
            raise WorldError(
                "NO_IMPROVEMENT",
                "Local estimate does not exceed the configured best score",
            )
        package = {
            "id": identifier("pkg"),
            "status": "awaiting_approval",
            "created_at": now(),
            "local_score": score,
            "experiments": experiment_ids,
            "reference_costs": REFERENCE,
            "configured_best": self.settings.current_best,
            "approval_required": True,
            "comparison_basis": "local estimate; not a leaderboard result",
        }
        folder = self.settings.data_root / "packages" / package["id"]
        folder.mkdir(parents=True)
        archive = folder / "submission.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for j in jobs:
                source = self.settings.data_root / "experiments" / j["id"] / "input"
                paths = (
                    list(source.glob("angle_k_*.km[bx]")) + [source / "submission.npz"]
                    if j["task"] == "dfthcsos"
                    else list(source.glob(j["task"] + ".km[bx]"))
                )
                for p in sorted(paths):
                    z.write(
                        p, ("dfthcsos/" if j["task"] == "dfthcsos" else "") + p.name
                    )
        package["sha256"] = digest(archive)
        package["bytes"] = archive.stat().st_size
        (folder / "review.json").write_text(json.dumps(package, indent=2))
        self.store.put("package", owner, package)
        return package

    async def close(self):
        tasks = list(self.workers.values())
        for owner, job in [] if self.administrative else self.store.list("experiment"):
            if job["status"] == "queued":
                job.update(status="cancelled", finished_at=now())
                self.store.put("experiment", owner, job)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()
