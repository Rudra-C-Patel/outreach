"""tracker.csv: the single source of truth. All writes go through a file lock + atomic replace."""
from __future__ import annotations

import csv
import fcntl
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import config

FIELDS = [
    "id", "name", "company", "role", "email", "source", "verified", "status",
    "added_at", "drafted_at", "sent_at", "replied_at", "bounced_at",
    "followup_drafted_at", "followup_sent_at", "closed_at", "notes",
    # working columns
    "email_how", "detail", "detail_quote", "detail_url", "factcheck",
    "tz", "ask", "ask_org", "subject", "draft_id", "thread_id", "message_id", "rfc_message_id",
    "followup_draft_id", "approved_at", "scheduled_at",
]

# status lifecycle: new -> drafted -> approved(queued) -> sent -> replied|bounced|follow-up -> closed
STATUSES = {"new", "drafted", "approved", "sent", "replied", "bounced", "follow-up", "closed", "skipped"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def locked() -> Iterator[None]:
    config.ensure_data_dir()
    with open(config.LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read() -> list[dict]:
    if not config.TRACKER.exists():
        return []
    with open(config.TRACKER, newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for f in FIELDS:
            r.setdefault(f, "")
    return rows


def _write(rows: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=config.DATA, prefix=".tracker.")
    with os.fdopen(fd, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.chmod(tmp, 0o600)
    os.replace(tmp, config.TRACKER)


def load() -> list[dict]:
    with locked():
        return _read()


@contextmanager
def transaction() -> Iterator[list[dict]]:
    """Read-modify-write under the lock."""
    with locked():
        rows = _read()
        yield rows
        _write(rows)


def next_id(rows: list[dict]) -> str:
    return str(max((int(r["id"]) for r in rows if r["id"].isdigit()), default=0) + 1)


def find_dupe(rows: list[dict], email: str, name: str, company: str) -> dict | None:
    e = email.strip().lower()
    for r in rows:
        if e and r["email"].lower() == e:
            return r
        if r["name"].lower() == name.lower() and r["company"].lower() == company.lower():
            return r
    return None


def add_row(rows: list[dict], **fields) -> dict:
    row = {f: "" for f in FIELDS}
    row.update({k: ("" if v is None else str(v)) for k, v in fields.items()})
    row["id"] = next_id(rows)
    row["status"] = row["status"] or "new"
    row["added_at"] = row["added_at"] or now_iso()
    rows.append(row)
    return row


def get(rows: list[dict], rid: str) -> dict:
    for r in rows:
        if r["id"] == str(rid):
            return r
    raise KeyError(f"no tracker row with id {rid}")
