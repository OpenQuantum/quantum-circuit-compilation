"""Native official verification; untrusted generators run only in Docker."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal

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
            paths = [
                p
                for p in source.glob(task + ".km[bx]")
                if p.is_file() and not p.is_symlink()
            ]
            if len(paths) != 1:
                raise WorldError("SEED_INVALID", "Seed task is ambiguous or missing")
            shutil.copyfile(paths[0], inputs / paths[0].name)
        if candidate:
            programs = s.data_root / "candidates" / candidate["id"]
            if manifest(programs) != candidate["files"]:
                raise WorldError("CANDIDATE_CHANGED", "Candidate source hashes changed")
            container = "qw-" + job["id"]
            command = [
                "docker",
                "run",
                "--name",
                container,
                "--rm",
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
                f"{os.getuid()}:{os.getgid()}",
                "--tmpfs",
                "/tmp:rw,nosuid,size=128m",
                "-e",
                "PYTHONPATH=/official/verifiers",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-v",
                f"{programs}:/candidate:ro",
                "-v",
                f"{s.project_root / 'official/data'}:/official:ro",
                "-v",
                f"{inputs}:/out:rw",
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
                    {
                        "PATH": os.environ.get("PATH", os.defpath),
                        "HOME": os.environ.get("HOME", ""),
                    },
                    inputs,
                )
                if code:
                    raise WorldError(
                        "BUILD_ERROR", "Generator failed; inspect bounded logs."
                    )
            finally:
                # Killing the docker CLI does not stop its container.
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "-f",
                    container,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await cleanup.wait()
        check_command = [
            s.worker_python,
            Path(__file__).with_name("preflight.py"),
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
        module = {
            "invsqrt": "invsqrt.verify_submission_invsqrt",
            "dqi": "dqi.verify_submission_dqi",
            "dfthcsos": "dfthcsos.verify",
        }[task]
        command = [s.worker_python, "-m", module, entry, "--salt", job["salt"]]
        if task != "dfthcsos":
            command += ["--max-workers", str(s.max_workers)]
        if job["mode"] == "quick":
            command += {
                "invsqrt": ["--max-x", "4096"],
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
        if code or report.get("status") != "PASS":
            raise WorldError(
                "VERIFICATION_FAILED",
                str(report.get("error", "Official verification failed"))[:1000],
            )
        cost = report.get("score")
        if not isinstance(cost, (int, float)) or not 0 < cost < float("inf"):
            raise WorldError("VERIFIER_ERROR", "Invalid task cost")
        return {
            "status": "PASS",
            "full": job["mode"] == "full",
            "task_cost": cost if job["mode"] == "full" else None,
            "diagnostic_cost": cost,
            "verified_shots": report.get("verified_shots"),
            "num_qubits": report.get("num_qubits"),
            "value_errors": report.get("value_errors"),
            "phase_errors": report.get("phase_errors"),
            "files": manifest(inputs),
        }
