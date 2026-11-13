"""Send timing: 2-6 min random gaps, 8 AM-6 PM recipient-local, hard cap 20 sends/day (your local day)."""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config


def local_tz():
    return datetime.now().astimezone().tzinfo


def in_window(t: datetime, tz: str) -> bool:
    lt = t.astimezone(ZoneInfo(tz or config.DEFAULT_TZ))
    return config.SEND_WINDOW[0] <= lt.hour < config.SEND_WINDOW[1]


def next_window_open(t: datetime, tz: str, rng: random.Random) -> datetime:
    """t if inside the window, else the next 8 AM (+0-20 min jitter) in the recipient's zone."""
    if in_window(t, tz):
        return t
    z = ZoneInfo(tz or config.DEFAULT_TZ)
    lt = t.astimezone(z)
    day = lt.date() if lt.hour < config.SEND_WINDOW[0] else lt.date() + timedelta(days=1)
    opens = datetime(day.year, day.month, day.day, config.SEND_WINDOW[0], tzinfo=z)
    return (opens + timedelta(seconds=rng.randint(0, 1200))).astimezone(timezone.utc)


def sent_times() -> list[datetime]:
    if not config.SEND_LOG.exists():
        return []
    with open(config.SEND_LOG, newline="") as fh:
        return [datetime.fromisoformat(r["sent_at"]) for r in csv.DictReader(fh)]


def log_send(row_id: str, kind: str, to: str, when: datetime) -> None:
    new = not config.SEND_LOG.exists()
    with open(config.SEND_LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["sent_at", "row_id", "kind", "to"])
        if new:
            w.writeheader()
        w.writerow({"sent_at": when.isoformat(), "row_id": row_id, "kind": kind, "to": to})
    config.SEND_LOG.chmod(0o600)


def sends_on(day, times: list[datetime]) -> int:
    ltz = local_tz()
    return sum(1 for t in times if t.astimezone(ltz).date() == day)


def plan(tzs: list[str], *, now: datetime | None = None, already: list[datetime] | None = None,
         rng: random.Random | None = None) -> list[datetime]:
    """Return a UTC send time for each recipient tz, in order."""
    rng = rng or random.Random()
    now = now or datetime.now(timezone.utc)
    taken = list(already if already is not None else sent_times())
    ltz = local_tz()
    last = max(taken, default=None)
    cursor = now if last is None else max(now, last + timedelta(seconds=config.SEND_GAP_MIN_S))
    out = []
    for tz in tzs:
        t = cursor + timedelta(seconds=rng.randint(config.SEND_GAP_MIN_S, config.SEND_GAP_MAX_S)) if out else cursor
        for _ in range(400):
            t = next_window_open(t, tz, rng)
            day = t.astimezone(ltz).date()
            if sends_on(day, taken) < config.DAILY_SEND_CAP:
                break
            nxt = datetime(day.year, day.month, day.day, tzinfo=ltz) + timedelta(days=1)
            t = nxt.astimezone(timezone.utc)
        out.append(t)
        taken.append(t)
        cursor = t
    return out
