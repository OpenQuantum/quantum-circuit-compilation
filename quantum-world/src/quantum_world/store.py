"""SQLite ownership and action idempotency; no automatic recovery of external work."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix):
    return prefix + "-" + uuid4().hex


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class WorldError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, kind TEXT, owner TEXT, body TEXT);
          CREATE TABLE IF NOT EXISTS actions(owner TEXT, id TEXT, request TEXT, outcome TEXT,
            PRIMARY KEY(owner,id));
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def put(self, kind, owner, data):
        self.db.execute(
            "INSERT INTO objects VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (data["id"], kind, owner, canonical(data)),
        )

    def get(self, kind, owner, object_id):
        row = self.db.execute(
            "SELECT body FROM objects WHERE id=? AND kind=? AND owner=?",
            (object_id, kind, owner),
        ).fetchone()
        if row is None:
            raise WorldError("NOT_FOUND", "No matching object owned by this caller.")
        return json.loads(row[0])

    def list(self, kind, owner=None):
        rows = self.db.execute(
            "SELECT owner,body FROM objects WHERE kind=?"
            + (" AND owner=?" if owner is not None else ""),
            (kind, owner) if owner is not None else (kind,),
        ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def action(self, owner, action_id):
        row = self.db.execute(
            "SELECT request,outcome FROM actions WHERE owner=? AND id=?",
            (owner, action_id),
        ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def save_action(self, owner, action_id, request, outcome):
        self.db.execute(
            "INSERT INTO actions VALUES(?,?,?,?)",
            (owner, action_id, request, canonical(outcome)),
        )

    def recover(self):
        for owner, job in self.list("experiment"):
            if job["status"] in ("queued", "running"):
                job.update(
                    status="interrupted",
                    error={
                        "code": "INTERRUPTED",
                        "message": "World restarted; work was not automatically replayed.",
                    },
                    finished_at=now(),
                )
                self.put("experiment", owner, job)
        for owner, package in self.list("package"):
            if package["status"] == "submitting":
                package.update(
                    status="unknown",
                    error="Submission interrupted. Check Kaggle before any further upload.",
                )
                self.put("package", owner, package)

    def close(self):
        self.db.close()
