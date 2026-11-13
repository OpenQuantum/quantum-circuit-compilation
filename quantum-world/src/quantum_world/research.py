"""Restricted service adapter for the independently running research workspace."""

import gzip
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import shutil
import tarfile
import tempfile
import zlib
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError

from .store import WorldError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# The service uses a fixed loopback origin; source-controlled URLs are never fetched.
MAX_ARCHIVE = 10 * 1024 * 1024
MAX_SOURCE = 20 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 512
HEX = re.compile(r"[0-9a-f]{64}\Z")
REMOTE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("Invalid path")
    path = PurePosixPath(value)
    if (
        not path.parts
        or value != path.as_posix()
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Invalid path")
    return value


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def valid_id(value):
    if not isinstance(value, str) or not REMOTE_ID.fullmatch(value):
        raise ValueError("Invalid remote identifier")
    return value


def valid_snapshot(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Invalid snapshot identifier")
    return value


def matches_import(destination, expected):
    if destination.is_symlink() or not destination.is_dir():
        return False
    if {p.name for p in destination.iterdir()} != {"files"}:
        return False
    root = destination / "files"
    if root.is_symlink() or not root.is_dir():
        return False
    directories = {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in expected
        for index in range(1, len(PurePosixPath(path).parts))
    }
    seen = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            return False
        if path.is_dir():
            if relative not in directories:
                return False
        elif path.is_file() and relative in expected:
            size, checksum = expected[relative]
            if path.stat().st_size != size or sha256(path.read_bytes()) != checksum:
                return False
            seen.add(relative)
        else:
            return False
    return seen == set(expected)


class ResearchClient:
    def __init__(self, settings):
        self.url = settings.research_url
        self.token = settings.research_token
        self.download_token = settings.research_download_token

    def query(self, name, params):
        if not self.url or not self.token:
            raise WorldError(
                "RESEARCH_UNCONFIGURED", "Research workspace is not configured"
            )
        query = {"task": params["task"]}
        if name == "GetResearchNode":
            query["node_id"] = params["node_id"]
        else:
            query.update(query=params.get("query", ""), limit=params.get("limit", 5))
        request = Request(
            self.url + "/api/agent/research?" + urlencode(query),
            headers={"Authorization": "Bearer " + self.token},
        )
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=8) as response:
                raw = response.read(80001)
            if len(raw) > 80000:
                raise ValueError("Response exceeds budget")
            data = json.loads(raw)
            if (
                data.get("scope") != "public_and_official_only"
                or data.get("read_only") is not True
                or data.get("task") != params["task"]
            ):
                raise ValueError("Unexpected research response")
            if (
                len(json.dumps(data, ensure_ascii=False).encode()) > 55000
                or len(json.dumps(data, ensure_ascii=False)) > 23000
            ):
                raise ValueError("Research result exceeds protocol budget")
            return data
        except HTTPError as error:
            raise WorldError(
                "RESEARCH_HTTP_ERROR", f"Research workspace returned HTTP {error.code}"
            ) from None
        except (OSError, ValueError):
            raise WorldError(
                "RESEARCH_UNAVAILABLE",
                "Research workspace unavailable or invalid response",
            ) from None

    def _v1_request(self, path, *, body=None, key=None, binary=False, internal=False):
        token = self.download_token if body is not None else self.token
        if not self.url or not token:
            raise WorldError(
                "RESEARCH_UNCONFIGURED",
                "Research workspace credential is not configured",
            )
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        req = Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(
                req, timeout=15
            ) as response:
                data = response.read((MAX_ARCHIVE if binary else 80000) + 1)
            if len(data) > (MAX_ARCHIVE if binary else 80000):
                raise ValueError("Response exceeds budget")
            if binary:
                return data
            data = json.loads(data)
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or data.get("scope") != "public_and_official_only"
                or data.get("read_only") is not (body is None)
            ):
                raise ValueError("Unexpected research response")
            if len(json.dumps(data, ensure_ascii=False).encode()) > 55000 or (
                not internal and len(json.dumps(data, ensure_ascii=False)) > 23000
            ):
                raise ValueError("Research result exceeds protocol budget")
            return data
        except HTTPError as error:
            raise WorldError(
                "RESEARCH_HTTP_ERROR", f"Research workspace returned HTTP {error.code}"
            ) from None
        except (OSError, ValueError, TypeError, UnicodeError):
            raise WorldError(
                "RESEARCH_UNAVAILABLE",
                "Research workspace unavailable or invalid response",
            ) from None

    def v1(self, name, params, *, key=None, snapshot_id=None):
        try:
            if name == "SearchResearchSolutions":
                data = self._v1_request(
                    "/api/agent/v1/solutions?"
                    + urlencode(
                        {
                            "task": params["task"],
                            "query": params.get("query", ""),
                            "limit": params.get("limit", 5),
                        }
                    )
                )
                if data.get("task") != params["task"] or not isinstance(
                    data.get("solutions"), list
                ):
                    raise ValueError("Invalid solutions")
                return data
            if name == "StartResearchSourceDownload":
                data = self._v1_request(
                    "/api/agent/v1/source-downloads",
                    body={
                        "source_id": params["source_id"],
                        "version": params["version"],
                        "include_artifacts": False,
                    },
                    key=key,
                )
            elif name == "GetResearchDownloadJob":
                data = self._v1_request(
                    "/api/agent/v1/jobs/" + quote(valid_id(params["job_id"]), safe="")
                )
            else:
                base = "/api/agent/v1/snapshots/" + valid_snapshot(snapshot_id)
                if name == "GetResearchSnapshotStructure":
                    data = self._v1_request(base + "/structure")
                    if data.get("snapshot_id") != snapshot_id or not isinstance(
                        data.get("structure"), dict
                    ):
                        raise ValueError("Invalid structure")
                    return data
                if name == "ReadResearchSnapshotSource":
                    safe_path(params["path"])
                    if (
                        params["end_line"] < params["start_line"]
                        or params["end_line"] - params["start_line"] > 500
                    ):
                        raise ValueError("Invalid line range")
                    data = self._v1_request(
                        base
                        + "/source?"
                        + urlencode(
                            {k: params[k] for k in ("path", "start_line", "end_line")}
                        )
                    )
                    source = data.get("source")
                    if (
                        not isinstance(source, dict)
                        or source.get("snapshot_id") != snapshot_id
                        or source.get("path") != params["path"]
                    ):
                        raise ValueError("Invalid source")
                    return data
                raise ValueError("Unknown request")
            job = data.get("job")
            if (
                not isinstance(job, dict)
                or job.get("kind") != "source_download"
                or job.get("status")
                not in ("queued", "running", "succeeded", "partial", "failed")
                or not isinstance(job.get("source_id"), str)
                or not isinstance(job.get("source_version"), str)
                or job.get("task") not in ("invsqrt", "dqi", "dfthcsos")
            ):
                raise ValueError("Invalid job")
            valid_id(job.get("job_id"))
            if name == "GetResearchDownloadJob" and job["job_id"] != params["job_id"]:
                raise ValueError("Wrong job")
            if name == "StartResearchSourceDownload" and (
                job.get("source_id") != params["source_id"]
                or job.get("source_version") != params["version"]
            ):
                raise ValueError("Wrong source")
            if job.get("snapshot_id") is not None:
                valid_snapshot(job["snapshot_id"])
            if job["status"] == "succeeded" and not job.get("snapshot_id"):
                raise ValueError("Missing snapshot")
            return data
        except (ValueError, KeyError, TypeError):
            raise WorldError(
                "RESEARCH_INVALID_RESPONSE", "Invalid research request or response"
            ) from None

    def import_snapshot(self, snapshot_id, destination, job):
        """Check every archive byte and file before publishing an isolated import."""
        tmp = None
        try:
            valid_snapshot(snapshot_id)
            base = "/api/agent/v1/snapshots/" + snapshot_id
            envelope = self._v1_request(base + "/manifest", internal=True)
            m = envelope.get("manifest")
            if (
                not isinstance(m, dict)
                or m.get("snapshot_id") != snapshot_id
                or m.get("source_id") != job["source_id"]
                or m.get("source_version") != job["source_version"]
                or m.get("task") != job["task"]
            ):
                raise ValueError("Invalid manifest")
            archive = m.get("archive")
            if (
                not isinstance(archive, dict)
                or archive.get("format") != "tar.gz"
                or archive.get("sha256") != snapshot_id
                or type(archive.get("size")) is not int
                or not 0 < archive["size"] <= MAX_ARCHIVE
            ):
                raise ValueError("Invalid archive manifest")
            files = m.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
                raise ValueError("Invalid files")
            expected = {}
            total = 0
            for item in files:
                if not isinstance(item, dict):
                    raise ValueError("Invalid file")
                path = safe_path(item.get("path"))
                size, checksum = item.get("size"), item.get("sha256")
                if (
                    path in expected
                    or type(size) is not int
                    or not 0 <= size <= MAX_FILE
                    or not isinstance(checksum, str)
                    or not HEX.fullmatch(checksum)
                ):
                    raise ValueError("Invalid file metadata")
                expected[path] = (size, checksum)
                total += size
            if total > MAX_SOURCE or any(
                any(
                    PurePosixPath(*PurePosixPath(p).parts[:i]).as_posix() in expected
                    for i in range(1, len(PurePosixPath(p).parts))
                )
                for p in expected
            ):
                raise ValueError("Invalid file tree")
            raw = self._v1_request(base + "/archive", binary=True)
            if len(raw) != archive["size"] or sha256(raw) != snapshot_id:
                raise ValueError("Archive hash mismatch")
            with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as compressed:
                unpacked = compressed.read(MAX_SOURCE + 1024 * 1024 + 1)
            if len(unpacked) > MAX_SOURCE + 1024 * 1024:
                raise ValueError("Uncompressed archive exceeds budget")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = tempfile.mkdtemp(prefix=".research-", dir=destination.parent)
            seen = set()
            with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as tar:
                for member in tar:
                    path = safe_path(member.name)
                    if (
                        not member.isfile()
                        or path in seen
                        or path not in expected
                        or member.size != expected[path][0]
                    ):
                        raise ValueError("Invalid archive member")
                    seen.add(path)
                    stream = tar.extractfile(member)
                    content = stream.read(MAX_FILE + 1) if stream is not None else None
                    if (
                        content is None
                        or len(content) != member.size
                        or sha256(content) != expected[path][1]
                    ):
                        raise ValueError("File hash mismatch")
                    target = os.path.join(tmp, path)
                    os.makedirs(os.path.dirname(target), exist_ok=True, mode=0o700)
                    with open(target, "xb") as output:
                        output.write(content)
                if any(unpacked[tar.offset :]):
                    raise ValueError("Unexpected data after archive members")
            if seen != set(expected):
                raise ValueError("Missing archive member")
            try:
                destination.mkdir(mode=0o700)
            except FileExistsError:
                if not matches_import(destination, expected):
                    raise WorldError(
                        "RESEARCH_IMPORT_CONFLICT",
                        "An existing import differs from this snapshot",
                    ) from None
            else:
                try:
                    os.rename(tmp, destination / "files")
                    tmp = None
                except OSError:
                    destination.rmdir()
                    raise
            return m
        except (ValueError, OSError, tarfile.TarError, EOFError, zlib.error):
            raise WorldError(
                "RESEARCH_SNAPSHOT_INVALID",
                "Research snapshot failed integrity or safety checks",
            ) from None
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
