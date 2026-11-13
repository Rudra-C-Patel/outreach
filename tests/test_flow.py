"""End-to-end flow against a fake Gmail: nothing real is sent."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from outreach import cli, compose, config, gmail, schedule, store


class FakeGmail:
    def __init__(self):
        self.drafts, self.sent, self.n = {}, [], 0
        self.replied_threads, self.deleted = set(), set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, sub in [("DATA", ""), ("TRACKER", "tracker.csv"), ("SEND_LOG", "send_log.csv"),
                      ("CACHE", "cache"), ("DRAFTS_DIR", "drafts"), ("LOCK", ".lock"), ("TOKEN", "token.json")]:
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    fg = FakeGmail()

    def create_draft(svc, msg, thread_id=""):
        fg.n += 1
        did = f"d{fg.n}"
        fg.drafts[did] = (msg, thread_id)
        return {"id": did}

    def send_draft(svc, did):
        msg, tid = fg.drafts.pop(did)
        fg.sent.append(msg)
        return {"id": f"m{len(fg.sent)}", "threadId": tid or f"t{len(fg.sent)}"}

    monkeypatch.setattr(gmail, "service", lambda: "svc")
    monkeypatch.setattr(gmail, "assert_sender", lambda svc: None)
    monkeypatch.setattr(gmail, "create_draft", create_draft)
    monkeypatch.setattr(gmail, "send_draft", send_draft)
    monkeypatch.setattr(gmail, "draft_exists", lambda svc, did: did in fg.drafts and did not in fg.deleted)
    monkeypatch.setattr(gmail, "message_header", lambda svc, mid, h: f"<{mid}@mail.gmail.com>")
    monkeypatch.setattr(gmail, "thread_state", lambda svc, tid, r: "replied" if tid in fg.replied_threads else "none")
    monkeypatch.setattr(gmail, "bounce_search", lambda *a: False)
    monkeypatch.setattr(gmail, "reply_search", lambda *a: False)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(schedule, "in_window", lambda t, tz: True)
    monkeypatch.setattr(schedule, "plan", lambda tzs, **k: [datetime.now(timezone.utc) - timedelta(seconds=1)] * len(tzs))
    monkeypatch.setattr(cli.factcheck, "run", lambda ids=None, log=print: None)
    monkeypatch.setattr(compose, "compose", lambda row: compose.Draft(
        f"Internship at {row['company']}", compose.build_body(row["name"], "line", "internship"), "line", False, []))
    with store.transaction() as rows:
        for i in range(3):
            store.add_row(rows, name=f"Person {i}", company=f"Co{i}", role="Founder", email=f"p{i}@co{i}.com",
                          source="https://x", verified="yes", detail_quote="q", detail_url="https://x",
                          factcheck="ok 2026-11-04", tz="America/Los_Angeles", ask="internship")
    return fg


def A(**kw):
    return SimpleNamespace(**{"local": False, "ids": None, "background": False, **kw})


def test_nothing_sends_without_approve(env):
    cli.cmd_today(A())
    assert len(env.drafts) == 3 and env.sent == []
    assert {r["status"] for r in store.load()} == {"drafted"}


def test_approve_sends_only_selected(env, monkeypatch):
    # gaps: the worker's own gap guard must see real spacing; disable it via a fake send log clock
    monkeypatch.setattr(config, "SEND_GAP_MIN_S", 0)
    cli.cmd_today(A())
    cli.cmd_approve(A(ids=["1", "3"]))
    rows = {r["id"]: r for r in store.load()}
    assert [m["To"] for m in env.sent] == ["p0@co0.com", "p2@co2.com"]
    assert rows["1"]["status"] == "sent" and rows["2"]["status"] == "drafted" and rows["3"]["status"] == "sent"
    assert all(m["From"].endswith(f"<{config.SENDER}>") for m in env.sent)


def test_deleted_draft_is_not_sent(env, monkeypatch):
    monkeypatch.setattr(config, "SEND_GAP_MIN_S", 0)
    cli.cmd_today(A())
    env.deleted.add("d1")
    cli.cmd_approve(A(ids=["1"]))
    assert env.sent == [] and store.get(store.load(), "1")["status"] == "skipped"


def test_followup_once_and_reply_stops(env, monkeypatch):
    monkeypatch.setattr(config, "SEND_GAP_MIN_S", 0)
    cli.cmd_today(A())
    cli.cmd_approve(A(ids=["all"]))
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with store.transaction() as rows:
        for r in rows:
            r["sent_at"] = old
    env.replied_threads.add(store.get(store.load(), "2")["thread_id"])
    cli.cmd_check(A())
    rows = {r["id"]: r for r in store.load()}
    assert rows["2"]["status"] == "replied" and not rows["2"]["followup_drafted_at"]
    assert rows["1"]["status"] == "follow-up" and rows["1"]["followup_drafted_at"]
    n_drafts = len(env.drafts)
    cli.cmd_check(A())  # second check must NOT create a second follow-up
    assert len(env.drafts) == n_drafts
    cli.cmd_approve(A(ids=["1"]))
    assert env.sent[-1]["In-Reply-To"] and env.sent[-1]["Subject"].startswith("Re: ")
    assert store.get(store.load(), "1")["followup_sent_at"]
    cli.cmd_check(A())
    assert len(env.drafts) == n_drafts - 1  # still no new follow-up after the first was sent
