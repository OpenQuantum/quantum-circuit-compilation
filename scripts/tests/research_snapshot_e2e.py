#!/usr/bin/env python3
"""Opt-in integration test against an explicitly supplied isolated research service.

Set QCC_ISOLATED_RESEARCH_CONNECTION to a private JSON file containing url,
read_token and download_token. The script creates a real download job on that
service; use only a disposable, isolated service and its disposable database.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from quantum_world.config import Settings
from quantum_world.service import World
from quantum_world.store import WorldError

SOURCES = {
    "streamed": {
        "query": "streamed",
        "node_id": "public-dqi-streamed-node-seven-repair",
        "version": "11",
        "source_path": "embedded/build_streamed_repair.py",
        "entrypoints": ["embedded/build_streamed_repair.py"],
    },
    "hw22": {
        "query": "HW-22",
        "node_id": "dqi-hw-22",
        "version": "24",
        "source_path": "source.py",
        "entrypoints": [],
        "files": {"notebook.ipynb", "source.py", "cell-map.json", "provenance.json"},
        "code_scope": "notebook_only_generator_not_confirmed",
    },
}


def validate_source_details(source, manifest, structure, profile):
    if (
        manifest.get("entrypoints") != profile["entrypoints"]
        or structure.get("entrypoints") != profile["entrypoints"]
        or not manifest.get("dependencies", {}).get("missing")
        or not structure.get("dependencies", {}).get("missing")
    ):
        raise AssertionError("Snapshot entrypoints or missing dependencies mismatch")
    if "code_scope" in profile:
        scope = profile["code_scope"]
        if (
            source.get("code_scope") != scope
            or source.get("reproduction_ready") is not False
            or manifest.get("code_scope") != scope
            or structure.get("code_scope") != scope
        ):
            raise AssertionError("Notebook-only source scope or readiness mismatch")
        missing = " ".join(manifest["dependencies"]["missing"])
        structure_missing = " ".join(structure["dependencies"]["missing"])
        if any(
            term not in missing or term not in structure_missing
            for term in ("电路", "生成器")
        ):
            raise AssertionError("Missing circuit and generator are not identified")
        if {item["path"] for item in manifest["files"]} != profile["files"]:
            raise AssertionError("Notebook-only snapshot files mismatch")


async def exercise(connection_path, source_name):
    connection = json.loads(Path(connection_path).read_text(encoding="utf-8"))
    profile = SOURCES[source_name]
    run_id = uuid4().hex
    with TemporaryDirectory(prefix="qcc-world-snapshot-e2e-") as temporary:
        root = Path(temporary)
        world = World(
            Settings(
                root,
                root / "state",
                root / "python",
                root,
                allowed_agents={"test-agent": ["test-user", "other-user"]},
                research_url=connection["url"],
                research_token=connection["read_token"],
                research_download_token=connection["download_token"],
            )
        )

        async def call(name, params, action, user="test-user"):
            result = await world.call(
                "test-agent", f"{run_id}-{action}", name, {"userId": user, **params}
            )
            text = result["data"]["text"]
            if (
                len(text) > 24000
                or len(json.dumps(result, ensure_ascii=False).encode()) > 60000
            ):
                raise AssertionError("World response exceeds protocol limits")
            return json.loads(text)

        try:
            found = await call(
                "SearchResearchSolutions",
                {"task": "dqi", "query": profile["query"], "limit": 8},
                "search",
            )
            matches = [
                source
                for source in found["solutions"]
                if source.get("node_id") == profile["node_id"]
                and source.get("source_version") == profile["version"]
            ]
            if len(matches) != 1:
                raise AssertionError("Expected exactly one matching source and version")
            source = matches[0]
            started = await call(
                "StartResearchSourceDownload",
                {"source_id": source["source_id"], "version": profile["version"]},
                "start",
            )
            job = started["job"]
            deadline = time.monotonic() + 120
            poll = 0
            while (
                job["status"] in ("queued", "running") and time.monotonic() < deadline
            ):
                await asyncio.sleep(1)
                job = (
                    await call(
                        "GetResearchDownloadJob",
                        {"job_id": job["job_id"]},
                        f"poll-{poll}",
                    )
                )["job"]
                poll += 1
            if job["status"] != "succeeded" or not job.get("snapshot_id"):
                raise AssertionError(
                    "Source download did not produce a complete snapshot"
                )

            args = {"job_id": job["job_id"]}
            structure = await call("GetResearchSnapshotStructure", args, "structure")
            source_path = profile["source_path"]
            if structure["structure"].get("entrypoints") != profile["entrypoints"]:
                raise AssertionError("Snapshot entrypoints mismatch")
            excerpt = await call(
                "ReadResearchSnapshotSource",
                {**args, "path": source_path, "start_line": 1, "end_line": 10},
                "source",
            )
            if not excerpt["source"]["text"]:
                raise AssertionError("Expected source is empty")

            for name, arguments in (
                ("GetResearchDownloadJob", args),
                ("GetResearchSnapshotStructure", args),
                (
                    "ReadResearchSnapshotSource",
                    {**args, "path": source_path, "start_line": 1, "end_line": 10},
                ),
                ("ImportResearchSnapshot", args),
            ):
                try:
                    await call(name, arguments, f"other-{name}", user="other-user")
                except WorldError as error:
                    if error.code != "NOT_FOUND":
                        raise
                else:
                    raise AssertionError("Another user could access this research job")

            imported = await call("ImportResearchSnapshot", args, "import")
            if (
                imported["executed"] is not False
                or imported["snapshot_id"] != job["snapshot_id"]
            ):
                raise AssertionError("Unexpected import result")
            if "directory" in imported:
                raise AssertionError("World response exposed a host path")
            owner = world.owner("test-agent", {"userId": "test-user"})
            record = world.store.get("research_import", owner, imported["import_id"])
            directory = Path(record["directory"])
            if (
                directory.parent.parent != world.settings.data_root / "research-imports"
                or directory.parent.name != imported["import_id"]
                or directory.name != "files"
                or not directory.is_dir()
            ):
                raise AssertionError(
                    "Import was not placed in the isolated World directory"
                )
            manifest = record["manifest"]
            if (
                manifest["snapshot_id"] != job["snapshot_id"]
                or manifest["source_id"] != source["source_id"]
                or manifest["source_version"] != profile["version"]
                or manifest["task"] != "dqi"
                or manifest["archive"]["sha256"] != job["snapshot_id"]
            ):
                raise AssertionError(
                    "Manifest provenance, dependencies or entrypoint mismatch"
                )
            validate_source_details(source, manifest, structure["structure"], profile)
            files = manifest["files"]
            expected = {f["path"]: f for f in files}
            actual = {
                p.relative_to(directory).as_posix()
                for p in directory.rglob("*")
                if p.is_file()
            }
            if (
                len(expected) != len(files)
                or actual != set(expected)
                or imported["file_count"] != len(files)
            ):
                raise AssertionError("Imported file set does not match manifest")
            for name, item in expected.items():
                path = directory / name
                if path.is_symlink() or path.stat().st_size != item["size"]:
                    raise AssertionError("Imported file type or size mismatch")
                if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                    raise AssertionError("Imported file hash mismatch")
            if excerpt["source"]["file_sha256"] != expected[source_path]["sha256"]:
                raise AssertionError(
                    "Source excerpt hash does not match imported entrypoint"
                )
            structure_files = {
                item["path"]: item["sha256"] for item in structure["structure"]["files"]
            }
            if structure_files != {
                name: item["sha256"] for name, item in expected.items()
            }:
                raise AssertionError("Structure and manifest files differ")
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "source_id": source["source_id"],
                        "version": profile["version"],
                        "snapshot_id": job["snapshot_id"],
                        "files_verified": len(files),
                        "source_path": source_path,
                        "entrypoints": profile["entrypoints"],
                        "user_isolation": True,
                        "executed": False,
                    }
                )
            )
        finally:
            await world.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=tuple(SOURCES), default="streamed")
    args = parser.parse_args()
    connection_path = os.environ.get("QCC_ISOLATED_RESEARCH_CONNECTION")
    if not connection_path:
        print(
            "Skipped: set QCC_ISOLATED_RESEARCH_CONNECTION to an isolated test-service connection file."
        )
        return 0
    try:
        asyncio.run(exercise(connection_path, args.source))
    except Exception as error:
        # Do not reveal connection contents, request headers or service error payloads.
        print(f"Integration test failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
