"""Private worker for operator CLI; Python 3.12 / pinned Kaggle dependency.

Not an agent tool or a standalone approval API. The parent owns approval state.
"""

import contextlib
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

COMPETITION = "google-verifiable-quantum-circuit-compilation"


def check_upload_authorization(archive, state_root, package_id):
    """Fail closed even when somebody invokes this private worker directly."""
    expected = state_root / "packages" / package_id / "submission.zip"
    if archive.resolve() != expected.resolve():
        raise RuntimeError("Archive does not belong to this package")
    with sqlite3.connect(
        f"file:{state_root / 'world.sqlite3'}?mode=ro", uri=True
    ) as db:
        row = db.execute(
            "SELECT body FROM objects WHERE id=? AND kind=?", (package_id, "package")
        ).fetchone()
    if row is None:
        raise RuntimeError("Missing approval record")
    package = json.loads(row[0])
    approval = package.get("approval", {})
    if package["status"] != "submitting" or not approval.get("operator"):
        raise RuntimeError("Human approval was not consumed by the operator command")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != package["sha256"] or digest != approval.get("sha256"):
        raise RuntimeError("Approved archive hash changed")
    if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(timezone.utc):
        raise RuntimeError("Approval expired")
    if package["local_score"] <= package["live_best"]:
        raise RuntimeError("No verified improvement")


def account_best(api):
    scores = []
    seen = set()
    # Full account history, including any older best and pending submissions.
    for page in range(1, 101):
        rows = api.competition_submissions(COMPETITION, page_number=page, page_size=100)
        if rows is None:
            raise RuntimeError("Submission history unavailable")
        for row in rows:
            if row is None or str(row.ref) in seen:
                raise RuntimeError("Incomplete or repeated history page")
            seen.add(str(row.ref))
            status = getattr(row.status, "name", str(row.status)).upper()
            if status not in ("COMPLETE", "ERROR"):
                raise RuntimeError(
                    "Wait for pending submissions before comparing the best score"
                )
            value = row.public_score
            if status == "COMPLETE" and value in (None, ""):
                raise RuntimeError("Completed submission score unavailable")
            if value not in (None, ""):
                score = float(value)
                if not math.isfinite(score) or score < 0:
                    raise RuntimeError("Invalid public score")
                scores.append(score)
        if len(rows) < 100:
            return max(scores, default=0.0)
    raise RuntimeError("Submission history pagination limit reached")


def main():
    action = sys.argv[1]
    if action == "upload":
        if len(sys.argv) != 6:
            raise RuntimeError("Upload requires a durable human approval record")
        check_upload_authorization(Path(sys.argv[2]), Path(sys.argv[4]), sys.argv[5])
    from kaggle.api.kaggle_api_extended import KaggleApi
    from kaggle.models.kaggle_models_extended import ResumableUploadResult
    import requests

    api = KaggleApi()
    api.authenticate()
    if action == "best":
        return {"best": account_best(api)}
    if action != "upload":
        raise RuntimeError("Unknown operation")
    archive, receipt_path = map(Path, sys.argv[2:4])
    if archive.name != "submission.zip" or receipt_path.exists():
        raise RuntimeError("Wrong archive name or existing receipt; reconcile first")

    def chunks(path, url, quiet, resume=False):
        size = Path(path).stat().st_size
        with open(path, "rb") as source, requests.Session() as session:
            offset = 0
            while offset < size:
                source.seek(offset)
                data = source.read(256 * 1024)
                response = session.put(
                    url,
                    data=data,
                    headers={
                        "Content-Length": str(len(data)),
                        "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}",
                    },
                    timeout=(30, 60),
                )
                if response.status_code in (200, 201):
                    return ResumableUploadResult.COMPLETE
                acknowledged = response.headers.get("Range", "")
                if response.status_code != 308 or not acknowledged.startswith(
                    "bytes=0-"
                ):
                    raise RuntimeError("Upload failed; no automatic retry")
                next_offset = int(acknowledged.split("-")[-1]) + 1
                if not offset < next_offset <= offset + len(data):
                    raise RuntimeError("Unexpected upload offset")
                offset = next_offset
        return ResumableUploadResult.INCOMPLETE

    api.upload_complete = chunks
    response = api.competition_submit(
        str(archive),
        "Quantum World: human-approved verified archive",
        COMPETITION,
        quiet=True,
    )
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {"submission_ref": response.ref, "archive_sha256": digest}
    receipt_path.write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    try:
        # Some Kaggle SDK versions print authentication/progress messages.
        with contextlib.redirect_stdout(sys.stderr):
            result = main()
        print(json.dumps(result))
    except Exception:
        print(
            json.dumps(
                {
                    "error": "Kaggle operation failed; manually reconcile before retrying."
                }
            )
        )
        sys.exit(1)
