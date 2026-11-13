import asyncio
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from quantum_world.config import Settings
from quantum_world.service import World
from quantum_world.store import WorldError


def checksum(value):
    return hashlib.sha256(value).hexdigest()


def archive(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content, kind in entries:
            member = tarfile.TarInfo(name)
            if kind == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                tar.addfile(member)
            else:
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
    return data.getvalue()


class FakeService:
    def __init__(self):
        self.content = b"print('untrusted source')\n"
        self.raw = archive([("source.py", self.content, "file")])
        self.source_id = "src-" + "a" * 64
        self.calls = []
        self.job = {
            "job_id": "job-123",
            "kind": "source_download",
            "status": "succeeded",
            "source_id": self.source_id,
            "source_version": "42",
            "task": "dqi",
            "snapshot_id": checksum(self.raw),
        }

    def set_archive(self, raw, files=None):
        self.raw = raw
        self.job["snapshot_id"] = checksum(raw)
        self.files = files

    def manifest(self):
        return {
            "snapshot_id": self.job["snapshot_id"],
            "source_id": self.source_id,
            "source_version": "42",
            "task": "dqi",
            "archive": {
                "format": "tar.gz",
                "sha256": self.job["snapshot_id"],
                "size": len(self.raw),
            },
            "files": getattr(self, "files", None)
            or [
                {
                    "path": "source.py",
                    "size": len(self.content),
                    "sha256": checksum(self.content),
                }
            ],
        }

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        post = request.get_method() == "POST"
        self.calls.append(
            (
                path,
                request.get_header("Authorization"),
                request.get_header("Idempotency-key"),
            )
        )
        assert request.get_header("Authorization") == (
            "Bearer download-token" if post else "Bearer read-token"
        )
        data = {
            "schema_version": 1,
            "scope": "public_and_official_only",
            "read_only": not post,
        }
        if path.endswith("/solutions"):
            data.update(
                task="dqi",
                solutions=[{"source_id": self.source_id, "source_version": "42"}],
            )
        elif path.endswith("/source-downloads"):
            assert json.loads(request.data) == {
                "source_id": self.source_id,
                "version": "42",
                "include_artifacts": False,
            }
            assert request.get_header("Idempotency-key")
            data["job"] = self.job.copy()
        elif "/jobs/" in path:
            data["job"] = self.job.copy()
        elif path.endswith("/manifest"):
            data["manifest"] = self.manifest()
        elif path.endswith("/archive"):
            return io.BytesIO(self.raw)
        elif path.endswith("/structure"):
            data.update(snapshot_id=self.job["snapshot_id"], structure={"symbols": []})
        elif path.endswith("/source"):
            data["source"] = {
                "snapshot_id": self.job["snapshot_id"],
                "path": "source.py",
                "text": self.content.decode(),
            }
        else:
            raise AssertionError(path)
        return io.BytesIO(json.dumps(data).encode())


class ResearchSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.world = World(
            Settings(
                root,
                root / "state",
                root / "python",
                root,
                allowed_agents={"agent": ["user", "other"]},
                research_url="http://127.0.0.1:8787",
                research_token="read-token",
                research_download_token="download-token",
            )
        )
        self.fake = FakeService()
        self.patcher = patch("quantum_world.research.build_opener")
        self.patcher.start().return_value.open.side_effect = self.fake.open

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.world.close()
        self.tmp.cleanup()

    async def call(self, name, args, action=None, user="user"):
        outcome = await self.world.call(
            "agent", action or name, name, {"userId": user, **args}
        )
        return json.loads(outcome["data"]["text"])

    async def start(self):
        return await self.call(
            "StartResearchSourceDownload",
            {"source_id": self.fake.source_id, "version": "42"},
        )

    async def test_full_flow_owned_idempotent_and_never_executes(self):
        result = await self.call("SearchResearchSolutions", {"task": "dqi"})
        self.assertEqual(result["solutions"][0]["source_version"], "42")
        first, second = await asyncio.gather(self.start(), self.start())
        self.assertEqual(first, second)
        self.assertEqual(
            sum(p.endswith("/source-downloads") for p, _, _ in self.fake.calls), 1
        )
        await self.call("GetResearchDownloadJob", {"job_id": "job-123"})
        await self.call("GetResearchSnapshotStructure", {"job_id": "job-123"})
        source = await self.call(
            "ReadResearchSnapshotSource",
            {"job_id": "job-123", "path": "source.py", "start_line": 1, "end_line": 1},
        )
        self.assertIn("untrusted", source["source"]["text"])
        imported = await self.call("ImportResearchSnapshot", {"job_id": "job-123"})
        self.assertFalse(imported["executed"])
        self.assertNotIn("directory", imported)
        owner = self.world.owner("agent", {"userId": "user"})
        record = self.world.store.get("research_import", owner, imported["import_id"])
        self.assertEqual(
            (Path(record["directory"]) / "source.py").read_bytes(), self.fake.content
        )
        self.assertFalse((self.world.settings.data_root / "candidates").exists())
        with self.assertRaises(WorldError) as error:
            await self.call(
                "ImportResearchSnapshot",
                {"job_id": "job-123"},
                action="other-user",
                user="other",
            )
        self.assertEqual(error.exception.code, "NOT_FOUND")
        with self.assertRaises(WorldError) as error:
            await self.call(
                "StartResearchSourceDownload",
                {"source_id": self.fake.source_id, "version": "different"},
            )
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")

    async def test_import_reuses_matching_directory_and_never_overwrites_conflicts(
        self,
    ):
        await self.start()
        destination = self.world.settings.data_root / "research-imports" / "fixed"
        client = self.world.research
        client.import_snapshot(self.fake.job["snapshot_id"], destination, self.fake.job)
        file = destination / "files" / "source.py"
        original_stat = file.stat().st_ino
        client.import_snapshot(self.fake.job["snapshot_id"], destination, self.fake.job)
        self.assertEqual(file.stat().st_ino, original_stat)
        file.write_bytes(b"existing content")
        with self.assertRaises(WorldError) as error:
            client.import_snapshot(
                self.fake.job["snapshot_id"], destination, self.fake.job
            )
        self.assertEqual(error.exception.code, "RESEARCH_IMPORT_CONFLICT")
        self.assertEqual(file.read_bytes(), b"existing content")
        file.unlink()
        file.symlink_to(Path(self.tmp.name) / "outside")
        with self.assertRaises(WorldError) as error:
            client.import_snapshot(
                self.fake.job["snapshot_id"], destination, self.fake.job
            )
        self.assertEqual(error.exception.code, "RESEARCH_IMPORT_CONFLICT")
        self.assertTrue(file.is_symlink())

    async def test_rejects_unsafe_and_tampered_archives(self):
        bad_cases = [
            (
                archive([("C:/outside", b"x", "file")]),
                [{"path": "C:/outside", "size": 1, "sha256": checksum(b"x")}],
            ),
            (
                archive([("../outside", b"x", "file")]),
                [{"path": "../outside", "size": 1, "sha256": checksum(b"x")}],
            ),
            (
                archive([("source.py", b"", "link")]),
                [{"path": "source.py", "size": 0, "sha256": checksum(b"")}],
            ),
            (
                archive([("source.py", b"x", "file"), ("source.py", b"x", "file")]),
                [{"path": "source.py", "size": 1, "sha256": checksum(b"x")}],
            ),
            (
                archive([("source.py", b"x", "file")]),
                [{"path": "source.py", "size": 1, "sha256": checksum(b"y")}],
            ),
            (
                gzip.compress(b"0" * (22 * 1024 * 1024)),
                [{"path": "source.py", "size": 1, "sha256": checksum(b"x")}],
            ),
        ]
        for index, (raw, files) in enumerate(bad_cases):
            self.fake.set_archive(raw, files)
            await self.call(
                "StartResearchSourceDownload",
                {"source_id": self.fake.source_id, "version": "42"},
                action=f"start-{index}",
            )
            with self.assertRaises(WorldError) as error:
                await self.call(
                    "ImportResearchSnapshot",
                    {"job_id": "job-123"},
                    action=f"import-{index}",
                )
            self.assertEqual(error.exception.code, "RESEARCH_SNAPSHOT_INVALID")
        outside = Path(self.tmp.name) / "outside"
        self.assertFalse(outside.exists())
        self.assertFalse(
            list(
                (self.world.settings.data_root / "research-imports").glob("research-*")
            )
        )

    async def test_rejects_archive_size_and_source_identity_mismatch(self):
        await self.start()
        original_manifest = self.fake.manifest
        for kind in ("size", "identity"):

            def tampered():
                manifest = original_manifest()
                if kind == "size":
                    manifest["archive"]["size"] += 1
                else:
                    manifest["source_version"] = "different"
                return manifest

            self.fake.manifest = tampered
            with self.assertRaises(WorldError) as error:
                await self.call(
                    "ImportResearchSnapshot", {"job_id": "job-123"}, action=kind
                )
            self.assertEqual(error.exception.code, "RESEARCH_SNAPSHOT_INVALID")

    async def test_download_requires_separate_credential(self):
        self.world.research.download_token = None
        self.assertEqual(
            (await self.call("SearchResearchSolutions", {"task": "dqi"}))["task"], "dqi"
        )
        with self.assertRaises(WorldError) as error:
            await self.start()
        self.assertEqual(error.exception.code, "RESEARCH_UNCONFIGURED")

    async def test_large_manifest_stays_internal_and_preserves_every_file(self):
        entries = [
            (f"embedded/{i:03d}-" + "x" * 150 + ".py", b"a", "file") for i in range(120)
        ]
        files = [
            {"path": name, "size": len(content), "sha256": checksum(content)}
            for name, content, _ in entries
        ]
        self.fake.set_archive(archive(entries), files)
        self.assertGreater(len(json.dumps(self.fake.manifest())), 23000)
        await self.start()
        imported = await self.call("ImportResearchSnapshot", {"job_id": "job-123"})
        self.assertEqual(imported["file_count"], 120)
        self.assertEqual(
            len(
                self.world.store.get(
                    "research_import",
                    self.world.owner("agent", {"userId": "user"}),
                    imported["import_id"],
                )["manifest"]["files"]
            ),
            120,
        )
