import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "local_stack", ROOT / "scripts/local_stack.py"
)
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)


class ResearchStackTests(unittest.TestCase):
    def test_research_proxy_environment_with_and_without_proxies(self):
        empty = stack.research_proxy_env({"UNRELATED_SECRET": "not-inherited"})
        self.assertEqual(set(empty), {"NO_PROXY", "no_proxy"})
        self.assertEqual(empty["NO_PROXY"], "127.0.0.1,localhost,::1")
        self.assertEqual(empty["no_proxy"], empty["NO_PROXY"])

        configured = {
            "HTTP_PROXY": "http://proxy-a:1",
            "HTTPS_PROXY": "http://proxy-b:2",
            "ALL_PROXY": "socks5://proxy-c:3",
            "http_proxy": "http://proxy-d:4",
            "https_proxy": "http://proxy-e:5",
            "all_proxy": "socks5://proxy-f:6",
            "NO_PROXY": "example.test,localhost",
            "no_proxy": "internal.test, example.test",
            "UNRELATED_SECRET": "not-inherited",
        }
        values = stack.research_proxy_env(configured)
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            self.assertEqual(values[name], configured[name])
        self.assertNotIn("UNRELATED_SECRET", values)
        self.assertEqual(values["NO_PROXY"], values["no_proxy"])
        self.assertEqual(
            values["NO_PROXY"].split(","),
            ["127.0.0.1", "localhost", "::1", "example.test", "internal.test"],
        )

    def test_python_selection_and_missing_explicit_path(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(stack.source_python(root, None))
            with self.assertRaises(ValueError):
                stack.source_python(root, root / "missing")
            python = root / ".runtime/kaggle-venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o700)
            self.assertEqual(stack.source_python(root, None), str(python))
            alternate = root / "explicit-python"
            alternate.symlink_to(python)
            self.assertEqual(stack.source_python(root, alternate), str(alternate))
