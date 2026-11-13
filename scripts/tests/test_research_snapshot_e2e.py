import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "research_snapshot_e2e", ROOT / "scripts/tests/research_snapshot_e2e.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SourceValidationTests(unittest.TestCase):
    def test_hw22_requires_notebook_only_scope_and_explicit_gaps(self):
        profile = module.SOURCES["hw22"]
        source = {"code_scope": profile["code_scope"], "reproduction_ready": False}
        manifest = {
            "code_scope": profile["code_scope"],
            "entrypoints": [],
            "dependencies": {"missing": ["电路和生成器未确认"]},
            "files": [{"path": name} for name in profile["files"]],
        }
        structure = {
            "code_scope": profile["code_scope"],
            "entrypoints": [],
            "dependencies": {"missing": ["电路和生成器未确认"]},
        }
        module.validate_source_details(source, manifest, structure, profile)
        bad_source = {**source, "reproduction_ready": True}
        with self.assertRaises(AssertionError):
            module.validate_source_details(bad_source, manifest, structure, profile)
        for key, replacement in (
            ("entrypoints", ["source.py"]),
            ("files", manifest["files"][:-1]),
            ("dependencies", {"missing": ["依赖缺失"]}),
        ):
            invalid = copy.deepcopy(manifest)
            invalid[key] = replacement
            with self.subTest(key=key), self.assertRaises(AssertionError):
                module.validate_source_details(source, invalid, structure, profile)

    def test_streamed_still_requires_its_generator_entrypoint(self):
        profile = module.SOURCES["streamed"]
        manifest = {
            "entrypoints": profile["entrypoints"],
            "dependencies": {"missing": ["unresolved dependency"]},
        }
        structure = copy.deepcopy(manifest)
        module.validate_source_details({}, manifest, structure, profile)
        structure["entrypoints"] = []
        with self.assertRaises(AssertionError):
            module.validate_source_details({}, manifest, structure, profile)
