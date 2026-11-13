"""Daemon-side mount topology for candidate execution from a container."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from quantum_world.runner import OfficialRunner, manifest
from quantum_world.store import WorldError


class CandidateDockerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.state_volume = root / "state"
        self.state = self.state_volume / "world"
        self.project = root / "project"
        self.official = self.project / "official/data"
        self.seed = self.official / "sample_submission"
        self.seed.mkdir(parents=True)
        (self.seed / "invsqrt.kmb").write_bytes(b"test circuit")
        self.programs = self.state / "candidates/cand-test"
        self.programs.mkdir(parents=True)
        (self.programs / "main.py").write_text("print('candidate')", encoding="utf-8")
        self.directory = self.state / "experiments/exp-test"
        self.directory.mkdir(parents=True)
        self.settings = SimpleNamespace(
            data_root=self.state, project_root=self.project, seed_path=self.seed,
            worker_python=Path("python"), max_workers=1, generator_timeout=1, job_timeout=1,
            generator_image="fixture-generator:local",
        )
        self.mounts = [
            {"Type": "volume", "Source": "test-state", "Target": str(self.state_volume)},
            {"Type": "volume", "Source": "test-official", "Target": str(self.official), "ReadOnly": True},
            {"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/var/run/docker.sock"},
        ]
        self.job = {"id": "exp-test", "task": "invsqrt", "mode": "quick", "salt": "fixture"}
        self.candidate = {"id": "cand-test", "entrypoint": "main.py", "files": manifest(self.programs)}

    async def invoke(self, env, *, uid=1000):
        async def fake_process(command, cwd, log, timeout, environment, monitor=None):
            if log.name == "preflight":
                log.with_suffix(".stdout").write_text(json.dumps({"input": str(self.directory / "input/invsqrt.kmb")}))
            elif log.name == "verify":
                log.with_suffix(".stdout").write_text(json.dumps({"status": "PASS", "score": 2.0}))
            return 0

        with ExitStack() as stack:
            self.process = stack.enter_context(patch("quantum_world.runner.process", side_effect=fake_process))
            self.cleanup = stack.enter_context(patch("quantum_world.runner.asyncio.create_subprocess_exec"))
            self.cleanup.return_value.wait = AsyncMock(return_value=0)
            self.inspect = stack.enter_context(patch("subprocess.run"))
            active = [{"Type": mount["Type"], "Destination": mount["Target"], "Name": mount["Source"],
                       "RW": not mount.get("ReadOnly", False), "Driver": "local"} for mount in self.mounts]
            self.inspect.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps({
                "id": "c" * 64, "running": True, "configured": self.mounts, "active": active,
            }))
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch.object(socket, "gethostname", return_value="c" * 12))
            stack.enter_context(patch.object(os, "getuid", return_value=uid, create=True))
            stack.enter_context(patch.object(os, "getgid", return_value=uid, create=True))
            self.chown = stack.enter_context(patch.object(os, "chown", create=True))
            return await OfficialRunner(self.settings).run(self.job, self.candidate, self.directory)

    async def test_container_passes_only_daemon_resolvable_scoped_volumes(self):
        result = await self.invoke({"QUANTUM_DOCKER_SELF": "world-fixture", "HUB_CLIENT_TOKEN": "fixture-secret"})
        self.assertEqual(result["status"], "PASS")
        self.inspect.assert_called_once()
        query, = self.inspect.call_args.args
        self.assertEqual(query[:7], ["docker", "--host", "unix:///var/run/docker.sock", "container", "inspect",
                                     "--format", '{"id":{{json .Id}},"running":{{json .State.Running}},'
                                     '"configured":{{json .HostConfig.Mounts}},"active":{{json .Mounts}}}'])
        self.assertEqual(query[7:], ["world-fixture"])
        command, _, _, _, environment, _ = self.process.call_args_list[0].args
        mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
        self.assertEqual(mounts, [
            "type=volume,source=test-state,target=/candidate,volume-nocopy,volume-subpath=world/candidates/cand-test,readonly",
            "type=volume,source=test-official,target=/official,volume-nocopy,readonly",
            "type=volume,source=test-state,target=/out,volume-nocopy,volume-subpath=world/experiments/exp-test/input",
        ])
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
        self.assertIn("--read-only", command)
        self.assertIn("--pull=never", command)
        self.assertNotIn("--privileged", command)
        self.assertEqual(command[command.index("--user") + 1], "1000:1000")
        self.assertNotIn("HUB_CLIENT_TOKEN", environment)
        self.assertEqual(self.cleanup.call_args.args[:3], ("docker", "--host", "unix:///var/run/docker.sock"))
        self.assertEqual(self.cleanup.call_args.kwargs["env"], environment)

    async def test_root_world_runs_candidate_as_nonroot_on_its_own_output(self):
        await self.invoke({"QUANTUM_DOCKER_SELF": "world-fixture"}, uid=0)
        command = self.process.call_args_list[0].args[0]
        self.assertEqual(command[command.index("--user") + 1], "65534:65534")
        self.assertEqual([call.args for call in self.chown.call_args_list], [
            (self.directory / "input", 65534, 65534),
            (self.directory / "input/invsqrt.kmb", 65534, 65534),
        ])

    async def test_container_rejects_a_bind_mount_for_writable_state(self):
        self.mounts[0]["Type"] = "bind"
        with self.assertRaisesRegex(WorldError, "named volume"):
            await self.invoke({"QUANTUM_DOCKER_SELF": "world-fixture"})
        self.process.assert_not_called()

    async def test_container_rejects_official_volume_mounted_writable(self):
        self.mounts[1]["ReadOnly"] = False
        with self.assertRaisesRegex(WorldError, "read-only"):
            await self.invoke({"QUANTUM_DOCKER_SELF": "world-fixture"})
        self.process.assert_not_called()

    async def test_container_rejects_subpath_mount_as_parent_of_writable_state(self):
        self.mounts[0]["VolumeOptions"] = {"Subpath": "some-other-directory"}
        with self.assertRaisesRegex(WorldError, "volume root"):
            await self.invoke({"QUANTUM_DOCKER_SELF": "world-fixture"})
        self.process.assert_not_called()

    async def test_container_rejects_mount_inside_candidate_source(self):
        self.mounts.append({"Type": "bind", "Source": "/fixture", "Target": str(self.programs / "main.py")})
        with self.assertRaisesRegex(WorldError, "overlap"):
            await self.invoke({"QUANTUM_DOCKER_SELF": "world-fixture"})
        self.process.assert_not_called()

    async def test_host_world_keeps_its_native_bind_mounts(self):
        await self.invoke({})
        self.inspect.assert_not_called()
        command = self.process.call_args_list[0].args[0]
        self.assertIn(f"{self.programs}:/candidate:ro", command)
        self.assertIn(f"{self.official}:/official:ro", command)
        self.assertIn(f"{self.directory / 'input'}:/out:rw", command)


if __name__ == "__main__":
    unittest.main()
