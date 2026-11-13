import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from outreach import compose, config, fetch, research, schedule, validate

QUOTE = "Quillstack is hiring a Software Engineering Intern to work on merge queue tooling"
CTX = "Maya Chen | Quillstack | Founder/CEO"
GOOD_LINE = "I saw Quillstack is hiring a Software Engineering Intern for its merge queue tooling."
GOOD_SUBJ = "Software Engineering Intern role at Quillstack"


def v(line=GOOD_LINE, subject=GOOD_SUBJ, ask="internship", body=None):
    body = body or compose.build_body("Maya Chen", line, ask)
    return validate.validate(subject, body, personal_line=line, quote=QUOTE, context=CTX)


def test_good_draft_passes():
    r = v()
    assert r.ok, r.errors


@pytest.mark.parametrize("line", [
    "I've followed your work on merge queue tooling at Quillstack for years.",
    "Quillstack is hiring a Software Engineering Intern for its amazing merge queue tooling.",
    "Quillstack is hiring a Software Engineering Intern and raised 12 million for merge queue tooling.",
    "Quillstack is hiring a Software Engineering Intern for merge queue tooling!",
    "I built merge queue tooling like the Quillstack Software Engineering Intern role needs.",
    "Quillstack is hiring a Software Engineering Intern and Google uses its merge queue tooling.",
    "Hope you're doing well and that Quillstack merge queue tooling is going great.",
])
def test_bad_lines_rejected(line):
    assert not v(line=line).ok


def test_too_long_rejected():
    long_line = "Quillstack is hiring a Software Engineering Intern for merge queue tooling " + "and more " * 12
    assert not v(line=long_line.strip() + ".").ok


@pytest.mark.parametrize("subj", ["Quick question", "URGENT internship", "Internship at Quillstack!",
                                  "A very long subject line that goes on and on past ten words total"])
def test_bad_subjects(subj):
    assert not v(subject=subj).ok


def test_tdlr_tamper_rejected():
    body = compose.build_body("Maya Chen", GOOD_LINE, "internship").replace("5,300", "6,000")
    assert not v(body=body).ok


def test_tldr_numbers_allowed():
    assert {"1,000", "5,300", "2028", "15", "2027"} <= validate.allowed_numbers()


def test_window_and_gaps():
    rng = random.Random(1)
    # 7 PM Pacific -> nothing until 8 AM Pacific next day
    now = datetime(2026, 11, 4, 19, 0, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)
    times = schedule.plan(["America/Los_Angeles"] * 5 + ["America/New_York"], now=now, already=[], rng=rng)
    for t, tz in zip(times, ["America/Los_Angeles"] * 5 + ["America/New_York"]):
        assert schedule.in_window(t, tz)
    for a, b in zip(times, times[1:]):
        assert config.SEND_GAP_MIN_S <= (b - a).total_seconds()
    assert (times[1] - times[0]).total_seconds() <= config.SEND_GAP_MAX_S


def test_daily_cap():
    rng = random.Random(2)
    now = datetime(2026, 11, 4, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)
    times = schedule.plan(["America/Los_Angeles"] * 45, now=now, already=[], rng=rng)
    ltz = schedule.local_tz()
    per_day = {}
    for t in times:
        per_day[t.astimezone(ltz).date()] = per_day.get(t.astimezone(ltz).date(), 0) + 1
    assert max(per_day.values()) <= config.DAILY_SEND_CAP


def test_unverified_cap():
    mk = lambda i, ver: research.Person(f"P {i}", "Founder", "X", "https://x", email=f"p{i}@x.com", verified=ver)
    kept, dropped = research.cap_unverified([mk(i, True) for i in range(8)] + [mk(i + 100, False) for i in range(5)])
    unv = sum(1 for p in kept if not p.verified)
    assert unv / len(kept) <= 0.20 and unv == 2 and dropped == 3
    kept, _ = research.cap_unverified([mk(i, False) for i in range(3)])
    assert kept == []


def test_linkedin_blocked():
    assert fetch.is_blocked("https://www.linkedin.com/in/someone")
    with pytest.raises(fetch.Blocked):
        fetch.get("https://linkedin.com/company/x")


def test_email_matching_and_patterns():
    assert research.match_email("Jane Doe", {"jane@acme.com", "info@acme.com"}, "acme.com") == "jane@acme.com"
    assert research.match_email("Jane Doe", {"info@acme.com"}, "acme.com") == ""
    assert research.infer_pattern([("Jane Doe", "jane.doe@acme.com"), ("Bo Li", "bo.li@acme.com")]) == "first.last"


def test_obfuscated_and_encoded_emails():
    from outreach import fetch as f
    pg = f.Page("u", 200, '<a href="mailto:%6b%61@%70.%65%64%75">x</a> reach me: ada [at] adabyte (dot) me', "reach me: ada [at] adabyte (dot) me")
    found = research.emails_in(pg)
    assert "ada@adabyte.me" in found and "ka@p.edu" in found
    assert research.match_email("Ada Byte", found) == "ada@adabyte.me"


def test_subject_must_name_company():
    assert not v(subject="Summer 2027 internship application").ok


def test_referral_ask_passes():
    body = compose.build_body("Maya Chen", GOOD_LINE, "referral", "Quillstack")
    assert "right person on Quillstack's recruiting team" in body
    assert v(body=body).ok, v(body=body).errors


def test_domain_me_is_not_first_person():
    res = validate.Result()
    validate.check_personal_line("I saw the TinyCache post on adabyte.me about caching SSM inputs.",
                                 "TinyCache: Cache SSM Inputs, Not State", "Ada Byte | Lumen Labs | adabyte.me", res)
    assert res.ok, res.errors
    res = validate.Result()
    validate.check_personal_line("Your TinyCache post on caching SSM inputs helped me a lot.",
                                 "TinyCache: Cache SSM Inputs, Not State", "Ada Byte", res)
    assert not res.ok
