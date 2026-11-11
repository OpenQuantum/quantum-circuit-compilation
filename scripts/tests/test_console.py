"""Local console initialization and validation image contracts."""

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/console.py"


class ConsoleInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "console"

    def init(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CLI), "--state-dir", str(self.state), "init", *arguments],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )

    def test_container_world_and_host_bff_share_the_same_identity(self):
        result = self.init(
            "--hub-url", "ws://127.0.0.1:28080", "--world-hub-url", "ws://hub:8000",
            "--user-id", "reviewer", "--agent-id", "agent-review", "--world-id", "world-review",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        console = json.loads((self.state / "console.json").read_text(encoding="utf-8"))
        world = tomllib.loads((self.state / "world.toml").read_text(encoding="utf-8"))
        agent, = json.loads((self.state / "agents.json").read_text(encoding="utf-8"))
        directory, = json.loads((self.state / "worlds.json").read_text(encoding="utf-8"))
        self.assertEqual(console["hub_url"], "ws://127.0.0.1:28080")
        self.assertEqual(Path(console["world_config"]), self.state / "world.toml")
        self.assertEqual(world["hub"]["url"], "ws://hub:8000")
        self.assertEqual(world["hub"]["allowed_agents"], {"agent-review": ["reviewer"]})
        self.assertEqual(world["hub"]["world_id"], "world-review")
        self.assertEqual((agent["ownerId"], agent["endpointId"], agent["worldId"]),
                         ("reviewer", "agent-review", "world-review"))
        self.assertEqual((directory["ownerId"], directory["environmentId"]),
                         ("reviewer", "world-review"))
        self.assertEqual(world["project_root"], "/project")
        self.assertEqual(world["data_root"], "/state/world")
        self.assertEqual(world["worker_python"], "/opt/quantum-worker/bin/python")
        self.assertEqual(world["seed_path"], "/project/official/data/sample_submission")

    def test_private_account_and_operator_token_are_created_once(self):
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stderr)
        account, = json.loads((self.state / "accounts.json").read_text(encoding="utf-8"))
        access_code = (self.state / "login-code").read_text(encoding="utf-8").strip()
        token = (self.state / "operator-token").read_text(encoding="utf-8").strip()
        digest = hashlib.pbkdf2_hmac("sha256", access_code.encode(), bytes.fromhex(account["salt"]), 200_000)
        self.assertEqual(account["passwordHash"], digest.hex())
        self.assertNotIn("username", account)
        self.assertGreaterEqual(len(token), 32)
        self.assertNotIn(access_code, result.stdout + result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.state / "login-code").stat().st_mode), 0o600)
        before = {path.name: path.read_bytes() for path in self.state.iterdir()}
        repeated = self.init()
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("overwrite", repeated.stderr)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.state.iterdir()})

    def test_existing_world_configuration_is_read_only_and_authorizes_the_pair(self):
        existing = self.state.parent / "world.toml"
        content = '[hub]\nworld_id = "kept-world"\nurl = "ws://hub:8000"\n[hub.allowed_agents]\nquantum-agent-local = ["local-user"]\n'
        existing.write_text(content, encoding="utf-8")
        result = self.init("--world-config", str(existing), "--user-id", "other-user")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authorize", result.stderr)
        self.assertFalse(self.state.exists())
        result = self.init("--world-config", str(existing), "--hub-url", "ws://127.0.0.1:28080")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((self.state / "console.json").read_text(encoding="utf-8"))
        self.assertEqual(config["world_id"], "kept-world")
        self.assertEqual(config["hub_url"], "ws://127.0.0.1:28080")
        self.assertEqual(existing.read_text(encoding="utf-8"), content)
        self.assertFalse((self.state / "world.toml").exists())

    def test_hub_url_cannot_embed_credentials(self):
        result = self.init("--hub-url", "ws://user:unprinted-credential@hub:8000")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Hub URL", result.stderr)
        self.assertNotIn("unprinted-credential", result.stdout + result.stderr)
        self.assertFalse(self.state.exists())

    def test_bff_launcher_uses_current_python_and_only_the_selected_credentials(self):
        result = self.init("--hub-url", "ws://127.0.0.1:28080", "--bff-port", "18320")
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = importlib.util.spec_from_file_location("console", CLI)
        console = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(console)
        root = self.state.parent
        (root / "frontend").mkdir()
        (root / "frontend/index.html").touch()
        env = {"PATH": "/test/bin", "HUB_CLIENT_TOKEN": "runtime-fixture-token",
               "KAGGLE_API_TOKEN": "unrelated-fixture-token", "QUANTUM_BFF_READ_ONLY": "false",
               "QUANTUM_OPERATOR_TOKEN": "unrelated-operator-token", "MODEL_API_KEY": "unrelated-model-token"}
        with patch.object(console.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "HUB_CLIENT_TOKEN"):
                console.serve_bff(self.state, root=root, environ={})
            process.assert_not_called()
            process.return_value.returncode = 0
            self.assertEqual(console.serve_bff(self.state, root=root, environ=env), 0)
        command, = process.call_args.args
        self.assertEqual(command, [sys.executable, "-m", "quantum_bff.cli", "--host", "127.0.0.1", "--port", "18320"])
        self.assertEqual(process.call_args.kwargs["cwd"], self.state / "run-bff")
        child = process.call_args.kwargs["env"]
        self.assertEqual(child["STAR_BFF_HUB_CLIENT_TOKEN"], "runtime-fixture-token")
        self.assertEqual(child["STAR_BFF_HUB_URL"], "ws://127.0.0.1:28080")
        self.assertEqual(child["STAR_BFF_HUB_ENVIRONMENT_ID"], "quantum-world-local")
        self.assertEqual(child["QUANTUM_BFF_READ_ONLY"], "true")
        self.assertEqual(child["QUANTUM_BFF_OPERATOR_TOKEN"], (self.state / "operator-token").read_text().strip())
        self.assertNotIn("KAGGLE_API_TOKEN", child)
        self.assertNotIn("MODEL_API_KEY", child)
        self.assertNotIn("HUB_CLIENT_TOKEN", child)
        self.assertNotIn("QUANTUM_OPERATOR_TOKEN", child)


class ValidationImageTests(unittest.TestCase):
    def test_image_builds_world_from_source_with_an_explicit_sdk_context(self):
        dockerfile = ROOT / "quantum-world/validation/Dockerfile"
        self.assertTrue(dockerfile.is_file(), "The validation image needs a tracked Dockerfile")
        content = dockerfile.read_text(encoding="utf-8")
        bases = re.findall(r"^FROM\s+(\S+)", content, re.M)
        self.assertTrue(any(base.startswith("python:3.13") for base in bases), bases)
        self.assertTrue(any(base.startswith("python:3.12") for base in bases), bases)
        self.assertTrue(all(base.startswith("python:") for base in bases), bases)
        self.assertIn("--from=star_sdk", content)
        self.assertIn("[http]", content)


if __name__ == "__main__":
    unittest.main()
