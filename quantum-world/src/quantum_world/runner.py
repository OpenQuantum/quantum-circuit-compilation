"""Native official verification; untrusted generators run only in Docker."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal

from .candidate_docker import candidate_mounts, docker_environment
from .invsqrt import (
    METRIC_PATH, VERIFIER_PATH, bounded_report, circuit_seed_files, optional_digest, seed_identity,
)
from .store import WorldError


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def manifest(folder):
    result = {}
    for p in sorted(folder.rglob("*")):
        if p.is_symlink():
            raise WorldError("INVALID_ARTIFACT", "Symlinks are not allowed.")
        if p.is_file():
            if p.stat().st_nlink != 1:
                raise WorldError("INVALID_ARTIFACT", "Hardlinks are not allowed.")
            result[p.relative_to(folder).as_posix()] = digest(p)
    return result


def clean_env(settings):
    return {
        "PATH": os.defpath,
        "HOME": str(settings.data_root),
        "PYTHONPATH": str(settings.project_root / "official/data/verifiers"),
        "PYTHONNOUSERSITE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "LANG": "en_US.UTF-8",
    }


async def process(command, cwd, log, timeout, env, monitor=None):
    with (
        log.with_suffix(".stdout").open("wb") as out,
        log.with_suffix(".stderr").open("wb") as err,
    ):
        proc = await asyncio.create_subprocess_exec(
            *map(str, command),
            cwd=cwd,
            env=env,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        waited = asyncio.create_task(proc.wait())
        try:
            async with asyncio.timeout(timeout):
                while proc.returncode is None:
                    if (
                        monitor
                        and sum(
                            p.stat().st_size
                            for p in monitor.rglob("*")
                            if p.is_file() and not p.is_symlink()
                        )
                        > 2**31
                    ):
                        raise WorldError(
                            "DISK_LIMIT", "Generator output exceeds 2 GiB."
                        )
                    if out.tell() + err.tell() > 4 * 1024 * 1024:
                        raise WorldError("LOG_LIMIT", "Process log limit exceeded.")
                    try:
                        await asyncio.wait_for(asyncio.shield(waited), 0.25)
                    except TimeoutError:
                        pass
            return proc.returncode
        except BaseException:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            raise
        finally:
            await waited


class OfficialRunner:
    def __init__(self, settings):
        self.settings = settings

    async def run(self, job, candidate, directory):
        s = self.settings
        task = job["task"]
        inputs = directory / "input"
        inputs.mkdir()
        used = sum(
            p.stat().st_size
            for p in s.data_root.rglob("*")
            if p.is_file() and not p.is_symlink()
        )
        if used > 12 * 1024**3 or shutil.disk_usage(s.data_root).free < 3 * 1024**3:
            raise WorldError(
                "DISK_LIMIT",
                "Clean up old experiments before scheduling more work (12 GiB retention / 3 GiB reserve).",
            )
        source = s.seed_path / ("dfthcsos" if task == "dfthcsos" else "")
        if task == "dfthcsos":
            for p in source.iterdir():
                if p.name == "submission.npz" or (
                    p.name.startswith("angle_k_") and p.suffix in (".kmb", ".kmx")
                ):
                    if p.is_symlink() or not p.is_file():
                        raise WorldError(
                            "SEED_INVALID", "Seed must contain regular files"
                        )
                    shutil.copyfile(p, inputs / p.name)
        else:
            paths = circuit_seed_files(source, task)
            if len(paths) != 1:
                raise WorldError("SEED_INVALID", "Seed task is ambiguous or missing")
            shutil.copyfile(paths[0], inputs / paths[0].name)
        if task == "invsqrt":
            seed = seed_identity(inputs / paths[0].name)
            if seed["sha256"] is None:
                raise WorldError("SEED_INVALID", "Could not fingerprint the copied seed")
            if candidate:
                input_kind = "candidate"
            elif seed["source"] == "official_sample_submission":
                input_kind = "official_baseline"
            else:
                input_kind = "configured_seed"
            job["provenance"] = {"input_kind": input_kind, "seed": seed, "candidate": None}
        if candidate:
            programs = s.data_root / "candidates" / candidate["id"]
            if manifest(programs) != candidate["files"]:
                raise WorldError("CANDIDATE_CHANGED", "Candidate source hashes changed")
            if task == "invsqrt":
                job["provenance"]["candidate"] = {
                    "id": candidate["id"], "entrypoint": candidate["entrypoint"], "files": candidate["files"],
                }
            container = "qw-" + job["id"]
            docker_env = docker_environment()
            docker, mounts = await asyncio.to_thread(
                candidate_mounts, programs, s.project_root / "official/data", inputs, docker_env
            )
            uid, gid = os.getuid(), os.getgid()
            if uid == 0:
                # Only the job's output is writable by the unprivileged candidate.
                uid = gid = 65534
                os.chown(inputs, uid, gid)
                for path in inputs.iterdir():
                    os.chown(path, uid, gid)
            command = [
                *docker,
                "run",
                "--name",
                container,
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--cpus",
                "2",
                "--memory",
                "3g",
                "--user",
                f"{uid}:{gid}",
                "--tmpfs",
                "/tmp:rw,nosuid,size=128m",
                "-e",
                "PYTHONPATH=/official/verifiers",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                *mounts,
                "-w",
                "/candidate",
                s.generator_image,
                "python",
                "/candidate/" + candidate["entrypoint"],
                "--output-dir",
                "/out",
            ]
            try:
                code = await process(
                    command,
                    directory,
                    directory / "generator",
                    s.generator_timeout,
                    docker_env,
                    inputs,
                )
                if code:
                    raise WorldError(
                        "BUILD_ERROR", "Generator failed; inspect bounded logs."
                    )
            finally:
                # Killing the docker CLI does not stop its container.
                cleanup = await asyncio.create_subprocess_exec(
                    *docker,
                    "rm",
                    "-f",
                    container,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=docker_env,
                )
                await cleanup.wait()
        preflight = Path(__file__).with_name("preflight.py")
        module = {
            "invsqrt": "invsqrt.verify_submission_invsqrt",
            "dqi": "dqi.verify_submission_dqi",
            "dfthcsos": "dfthcsos.verify",
        }[task]
        invsqrt_max_x = 4096 if task == "invsqrt" and job["mode"] == "quick" else None
        if task == "invsqrt":
            job["provenance"]["validation"] = {
                "module": module,
                "verifier_sha256": optional_digest(s.project_root / VERIFIER_PATH),
                "preflight_sha256": optional_digest(preflight),
                "metric_sha256": optional_digest(s.project_root / METRIC_PATH),
                "mode": job["mode"],
                "max_x": invsqrt_max_x,
                "max_workers": s.max_workers,
            }
        check_command = [
            s.worker_python,
            preflight,
            task,
            inputs,
            s.project_root,
        ]
        env = clean_env(s)
        code = await process(check_command, directory, directory / "preflight", 60, env)
        if code:
            raise WorldError(
                "INVALID_CIRCUIT", "Official metric preflight rejected the artifacts."
            )
        entry = json.loads((directory / "preflight.stdout").read_text())["input"]
        if task == "invsqrt":
            # The verifier sees the generated circuit after preflight. Record
            # the one actual artifact rather than echoing arbitrary generator files.
            circuit_path = Path(entry)
            job["provenance"]["built_artifacts"] = {circuit_path.name: digest(circuit_path)}
        command = [s.worker_python, "-m", module, entry, "--salt", job["salt"]]
        if task != "dfthcsos":
            command += ["--max-workers", str(s.max_workers)]
        if job["mode"] == "quick":
            command += {
                "invsqrt": ["--max-x", str(invsqrt_max_x)],
                "dqi": ["--num-shots", "4096"],
                "dfthcsos": ["--shots-per-address", "1"],
            }[task]
        code = await process(
            command, directory, directory / "verify", s.job_timeout, env
        )
        try:
            report = json.loads((directory / "verify.stdout").read_text())
        except (ValueError, OSError):
            raise WorldError(
                "VERIFIER_ERROR", "Verifier did not produce valid JSON."
            ) from None
        if not isinstance(report, dict):
            raise WorldError("VERIFIER_ERROR", "Verifier did not produce a JSON object.")
        if task == "invsqrt":
            job["provenance"]["verifier_report"] = bounded_report(report)
        if code or report.get("status") != "PASS":
            raise WorldError(
                "VERIFICATION_FAILED",
                str(report.get("error", "Official verification failed"))[:1000],
            )
        cost = report.get("score")
        if not isinstance(cost, (int, float)) or not 0 < cost < float("inf"):
            raise WorldError("VERIFIER_ERROR", "Invalid task cost")
        files = manifest(inputs)
        if task == "invsqrt":
            verified_sha256 = job["provenance"]["built_artifacts"][circuit_path.name]
            if files.get(circuit_path.name) != verified_sha256:
                raise WorldError("ARTIFACT_CHANGED", "Circuit changed during verification")
        return {
            "status": "PASS",
            "full": job["mode"] == "full",
            "task_cost": cost if job["mode"] == "full" else None,
            "diagnostic_cost": cost,
            "verified_shots": report.get("verified_shots"),
            "num_qubits": report.get("num_qubits"),
            "value_errors": report.get("value_errors"),
            "phase_errors": report.get("phase_errors"),
            "files": files,
        }
