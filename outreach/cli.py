"""outreach: approval-gated cold email. Nothing sends without `outreach approve`."""
from __future__ import annotations

import argparse
import math
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import compose, config, factcheck, fetch, gmail, research, schedule, store, validate

TEST_RECIPIENTS = {a for a in (config.TEST_RECIPIENT, config.SENDER) if a}


def _dt(s: str) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _save_local_draft(row: dict, subject: str, body: str, kind: str = "initial") -> Path:
    config.ensure_data_dir()
    p = config.DRAFTS_DIR / f"{row['id']}-{kind}.txt"
    p.write_text(f"To: {row['email']}\nSubject: {subject}\n\n{body}\n")
    p.chmod(0o600)
    return p


def _print_draft(row: dict, subject: str, body: str, attach: bool) -> None:
    print("=" * 78)
    print(f"#{row['id']}  {row['name']} - {row['role']} @ {row['company']}")
    print(f"To: {row['email']}   [{'verified' if row['verified'] == 'yes' else 'UNVERIFIED'}: {row['email_how']}]")
    print(f"Source: {row['source']}")
    print(f"Personal detail: \"{row['detail_quote']}\"\n   cited: {row['detail_url']}   factcheck: {row['factcheck']}")
    print(f"Recipient tz: {row['tz']}   Attachment: {config.RESUME.name if attach else 'none'}")
    print("-" * 78)
    print(f"Subject: {subject}\n\n{body}")
    print(f"\n[{len(validate.words(body))} words]")


# ---------------- commands ----------------

def cmd_auth(a) -> None:
    addr = gmail.authorize()
    print(f"Authorized as {addr}. Token saved to data/ (0600).")


def _insert(people: list[research.Person], ask: str) -> int:
    n = 0
    with store.transaction() as rows:
        for p in people:
            if store.find_dupe(rows, p.email, p.name, p.company):
                continue
            store.add_row(rows, name=p.name, company=p.company, role=p.role, email=p.email, source=p.source,
                          verified="yes" if p.verified else "no", email_how=p.email_how, detail=p.detail,
                          detail_quote=p.detail_quote, detail_url=p.detail_url, tz=p.tz, ask=ask)
            n += 1
    return n


def cmd_research(a) -> None:
    items: list[str] = []
    for chunk in a.targets:
        if chunk.startswith("@"):
            items += [l.strip() for l in Path(chunk[1:]).read_text().splitlines() if l.strip() and not l.startswith("#")]
        else:
            items += [s.strip() for s in chunk.replace("\n", ",").split(",") if s.strip()]
    if a.yc_interns:
        found = research.yc_intern_companies(a.yc_interns)
        for name, slug, title, url in found:
            print(f"YC intern posting: {name} - {title}  ({url})")
            items.append(f"{name}|{slug}|{title}|{url}")
    batch: list[research.Person] = []
    for it in items:
        yc_title = yc_url = ""
        if "|" in it:
            name, slug, yc_title, yc_url = it.split("|")
            it = name
        try:
            c_people = research.research_company(it, a.url or [], a.ask, use_llm=not a.no_llm)
        except fetch.Blocked as e:
            print(f"  ! {e}")
            continue
        if "|" in it or yc_title:
            for p in c_people:  # the YC intern posting is the most relevant, citable detail
                if p.detail_url != yc_url:
                    summary, quote = research.posting_detail(yc_url, yc_title)
                    p.detail, p.detail_quote, p.detail_url = summary, quote, yc_url
        batch += c_people
    kept, dropped = research.cap_unverified(batch)
    for p in kept:
        flag = "verified  " if p.verified else "UNVERIFIED"
        print(f"  + {p.name} | {p.role} | {p.company} | {p.email} [{flag}: {p.email_how}]\n      source: {p.source}\n"
              f"      detail: \"{p.detail_quote}\" ({p.detail_url})")
    if dropped:
        print(f"  - dropped {dropped} pattern-guessed address(es) to keep unverified <= 20% of the batch")
    n = _insert(kept, a.ask)
    print(f"Added {n} new target(s) to tracker (duplicates skipped). Next: outreach factcheck")


def cmd_add(a) -> None:
    if not a.source.startswith("http"):
        sys.exit("--source must be the URL where you found this person")
    if fetch.is_blocked(a.source) or fetch.is_blocked(a.detail_url):
        sys.exit("LinkedIn URLs can't be used as sources (it isn't fetched or fact-checked). Use a public page.")
    with store.transaction() as rows:
        if store.find_dupe(rows, a.email, a.name, a.company):
            sys.exit("already in tracker")
        r = store.add_row(rows, name=a.name, company=a.company, role=a.role, email=a.email.lower(), source=a.source,
                          verified="yes" if a.verified else "no",
                          email_how=a.email_how or ("published (added manually)" if a.verified else "unverified (added manually)"),
                          detail=a.detail or a.quote, detail_quote=a.quote, detail_url=a.detail_url,
                          tz=a.tz or config.DEFAULT_TZ, ask=a.ask, notes=a.notes or "")
    print(f"Added #{r['id']} {r['name']} ({r['company']}). Run: outreach factcheck {r['id']}")


def cmd_factcheck(a) -> None:
    factcheck.run(a.ids or None)


def _make_followups(svc, rows: list[dict], budget: int) -> list[tuple[dict, str, str]]:
    made = []
    now = datetime.now(timezone.utc)
    for r in rows:
        if budget <= 0:
            break
        if r["status"] != "sent" or r["followup_drafted_at"] or not r["sent_at"]:
            continue
        if now - _dt(r["sent_at"]) < timedelta(days=config.FOLLOWUP_AFTER_DAYS):
            continue
        body = config.FOLLOWUP_BODY.format(first=compose.first_name(r["name"]))
        subject = r["subject"] if r["subject"].lower().startswith("re:") else f"Re: {r['subject']}"
        msg = gmail.build_message(r["email"], subject, body, in_reply_to=r["rfc_message_id"], references=r["rfc_message_id"])
        d = gmail.create_draft(svc, msg, thread_id=r["thread_id"])
        r["followup_draft_id"], r["followup_drafted_at"], r["status"] = d["id"], store.now_iso(), "follow-up"
        _save_local_draft(r, subject, body, "followup")
        made.append((r, subject, body))
        budget -= 1
    return made


def cmd_check(a, svc=None) -> list:
    svc = svc or gmail.service()
    gmail.assert_sender(svc)
    changed = []
    with store.transaction() as rows:
        for r in rows:
            if r["status"] not in ("sent", "follow-up") or not r["thread_id"]:
                continue
            after = int(_dt(r["sent_at"]).timestamp()) if r["sent_at"] else 0
            state = gmail.thread_state(svc, r["thread_id"], r["email"])
            if state == "none" and gmail.bounce_search(svc, r["email"], after):
                state = "bounced"
            if state == "none" and gmail.reply_search(svc, r["email"], after):
                state = "replied"
            if state in ("replied", "bounced"):
                r["status"], r[f"{state}_at"] = state, store.now_iso()
                r["approved_at"] = r["scheduled_at"] = ""  # stop any queued follow-up
                changed.append(f"#{r['id']} {r['name']}: {state}")
        made = _make_followups(svc, rows, a.max_followups if hasattr(a, "max_followups") else config.DAILY_DRAFT_LIMIT)
    for c in changed:
        print("  " + c)
    for r, s, b in made:
        print(f"  follow-up drafted for #{r['id']} {r['name']} (approve with: outreach approve {r['id']})")
    if not changed and not made:
        print("  no replies, bounces, or follow-ups due")
    return made


def _reuse_preview(r: dict):
    """Reuse the local preview you already reviewed (re-validated), so Gmail gets the same text."""
    p = config.DRAFTS_DIR / f"{r['id']}-initial.txt"
    if not (p.exists() and r["subject"] and "local preview" in r["notes"]):
        return None
    head, body = p.read_text().split("\n\n", 1)
    body = body.rstrip("\n")
    subj = next((l[len("Subject: "):] for l in head.splitlines() if l.startswith("Subject: ")), "")
    parts = body.split("\n\n")
    line = parts[1] if len(parts) > 1 else ""
    ask = r["ask"] or "internship"
    expected = compose.build_body(r["name"], line, ask, r.get("ask_org") or r["company"])
    if subj != r["subject"] or body != expected:
        return None  # preview is stale (template/ask changed) -> regenerate
    res = validate.validate(subj, body, personal_line=line, quote=r["detail_quote"], context=compose._context(r))
    if not res.ok:
        return None
    return compose.Draft(subj, body, line, ask in compose.INTERNSHIP_ASKS, [])


def cmd_today(a) -> None:
    svc = None
    followups = []
    if not a.local:
        svc = gmail.service()
        gmail.assert_sender(svc)
        print("Daily check (replies / bounces / follow-ups):")
        followups = cmd_check(a, svc)
    unchecked = [r for r in store.load() if r["status"] == "new" and not r["factcheck"]]
    if unchecked:
        print("Fact-checking new targets:")
        factcheck.run([r["id"] for r in unchecked])
    rows = store.load()
    today = datetime.now().astimezone().date()
    drafted_today = sum(1 for r in rows if r["drafted_at"] and _dt(r["drafted_at"]).astimezone().date() == today)
    budget = max(0, config.DAILY_DRAFT_LIMIT - drafted_today - len(followups))
    ready = [r for r in rows if r["status"] == "new" and r["factcheck"].startswith("ok")]
    if a.ids:
        ready = [r for r in ready if r["id"] in a.ids]
    ver = [r for r in ready if r["verified"] == "yes"]
    unv = [r for r in ready if r["verified"] != "yes"]
    picks_v = ver[:budget]
    allowed_unv = math.floor(len(picks_v) * config.UNVERIFIED_MAX_FRACTION / (1 - config.UNVERIFIED_MAX_FRACTION))
    picks = picks_v + unv[:min(allowed_unv, budget - len(picks_v))]
    if not picks:
        print(f"No targets ready to draft (ready={len(ready)}, budget={budget}). Add/research + factcheck first.")
    made = 0
    for r in picks:
        d = _reuse_preview(r) or compose.compose(r)
        if d.errors:
            print(f"  ! #{r['id']} {r['name']}: draft rejected by validator after retries: {'; '.join(d.errors)}")
            with store.transaction() as rows:
                store.get(rows, r["id"])["notes"] = "validator: " + "; ".join(d.errors)[:300]
            continue
        draft_id = ""
        if svc:
            attach = config.RESUME if d.attach_resume and config.RESUME.exists() else None
            msg = gmail.build_message(r["email"], d.subject, d.body, attach=attach)
            draft_id = gmail.create_draft(svc, msg)["id"]
        with store.transaction() as rows:
            row = store.get(rows, r["id"])
            row.update(subject=d.subject, draft_id=draft_id)
            if svc:
                row.update(status="drafted", drafted_at=store.now_iso())
            else:
                row["notes"] = "local preview only (no Gmail draft yet)"
            _save_local_draft(row, d.subject, d.body)
            _print_draft(row, d.subject, d.body, d.attach_resume)
        made += 1
    where = "Gmail drafts" if svc else "LOCAL previews (no Gmail drafts created; --local)"
    print("=" * 78)
    print(f"{made} {where}. {len(followups)} follow-up draft(s).")
    if svc and (made or followups):
        print("Review/edit them in Gmail Drafts if you like, then: outreach approve <ids|all>")


def _pending(rows: list[dict]) -> list[tuple[dict, str]]:
    out = []
    for r in rows:
        if r["status"] == "drafted" and r["draft_id"]:
            out.append((r, "initial"))
        elif r["status"] == "follow-up" and r["followup_draft_id"] and not r["followup_sent_at"]:
            out.append((r, "followup"))
    return out


def cmd_approve(a) -> None:
    svc = gmail.service()
    gmail.assert_sender(svc)
    with store.transaction() as rows:
        pend = _pending(rows)
        if a.ids != ["all"]:
            pend = [(r, k) for r, k in pend if r["id"] in a.ids]
            missing = set(a.ids) - {r["id"] for r, _ in pend}
            if missing:
                print(f"  ! not approvable (no pending draft): {', '.join(sorted(missing))}")
        if not pend:
            sys.exit("Nothing to approve.")
        queued = [r for r in rows if r["approved_at"] and r["scheduled_at"]]
        already = schedule.sent_times() + [_dt(r["scheduled_at"]) for r in queued]
        times = schedule.plan([r["tz"] for r, _ in pend], already=already)
        for (r, kind), t in zip(pend, times):
            r["approved_at"], r["scheduled_at"] = store.now_iso(), t.isoformat()
            if kind == "initial":
                r["status"] = "approved"
            lt = t.astimezone(schedule.local_tz())
            rt = t.astimezone(__import__("zoneinfo").ZoneInfo(r["tz"]))
            print(f"  #{r['id']:>3} {kind:8} {r['email']:<34} at {lt:%a %H:%M} your time ({rt:%H:%M} {r['tz']})")
    if a.background:
        log = config.DATA / "worker.log"
        with open(log, "a") as fh:
            subprocess.Popen([sys.executable, "-m", "outreach.cli", "worker"], stdout=fh, stderr=fh,
                             start_new_session=True, env=os.environ.copy())
        print(f"Worker started in background (log: {log}). Stop it any time; `outreach worker` resumes.")
    else:
        print("Sending on schedule in this terminal. Ctrl-C pauses; `outreach worker` resumes.")
        cmd_worker(a)


def cmd_worker(a) -> None:
    rng = random.Random()
    svc = gmail.service()
    gmail.assert_sender(svc)
    while True:
        rows = store.load()
        queue = sorted([r for r in rows if r["approved_at"] and r["scheduled_at"]], key=lambda r: r["scheduled_at"])
        if not queue:
            print("Queue empty - done.")
            return
        r = queue[0]
        due = _dt(r["scheduled_at"])
        now = datetime.now(timezone.utc)
        if due > now:
            print(f"  next: #{r['id']} at {due.astimezone(schedule.local_tz()):%a %H:%M} (sleeping)", flush=True)
            time.sleep(min(60, (due - now).total_seconds()))
            continue
        kind = "followup" if r["status"] == "follow-up" else "initial"
        # re-verify every guard at send time
        last = max(schedule.sent_times(), default=None)
        problem = ""
        if r["status"] not in ("approved", "follow-up"):
            problem = f"status is {r['status']}"
        elif not schedule.in_window(now, r["tz"]) or (last and (now - last).total_seconds() < config.SEND_GAP_MIN_S) \
                or schedule.sends_on(now.astimezone(schedule.local_tz()).date(), schedule.sent_times()) >= config.DAILY_SEND_CAP:
            t = schedule.plan([r["tz"]], now=now)[0]
            with store.transaction() as rows:
                store.get(rows, r["id"])["scheduled_at"] = t.isoformat()
            print(f"  #{r['id']} rescheduled to {t.astimezone(schedule.local_tz()):%a %H:%M} (window/gap/cap)", flush=True)
            continue
        draft_id = r["draft_id"] if kind == "initial" else r["followup_draft_id"]
        if not problem and not gmail.draft_exists(svc, draft_id):
            problem = "Gmail draft was deleted (treating as un-approved)"
        if problem:
            with store.transaction() as rows:
                row = store.get(rows, r["id"])
                row["approved_at"] = row["scheduled_at"] = ""
                if row["status"] == "approved":
                    row["status"] = "skipped" if "deleted" in problem else "drafted"
                row["notes"] = (row["notes"] + f" | not sent: {problem}").strip(" |")
            print(f"  ! #{r['id']} not sent: {problem}", flush=True)
            continue
        sent = gmail.send_draft(svc, draft_id)
        when = datetime.now(timezone.utc)
        schedule.log_send(r["id"], kind, r["email"], when)
        rfc = gmail.message_header(svc, sent["id"], "Message-ID")
        with store.transaction() as rows:
            row = store.get(rows, r["id"])
            row["approved_at"] = row["scheduled_at"] = ""
            if kind == "initial":
                row.update(status="sent", sent_at=when.isoformat(), thread_id=sent.get("threadId", ""),
                           message_id=sent["id"], rfc_message_id=rfc)
            else:
                row["followup_sent_at"] = when.isoformat()
        print(f"  sent #{r['id']} ({kind}) -> {r['email']} at {when.astimezone(schedule.local_tz()):%H:%M}", flush=True)
        time.sleep(1)


def cmd_status(a) -> None:
    rows = store.load()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    order = ["new", "drafted", "approved", "sent", "follow-up", "replied", "bounced", "closed", "skipped"]
    print("Pipeline: " + "  ".join(f"{s}={counts.get(s, 0)}" for s in order))
    contacted = [r for r in rows if r["sent_at"]]
    delivered = [r for r in contacted if r["status"] != "bounced"]
    replied = [r for r in contacted if r["status"] == "replied" or r["replied_at"]]
    rate = (100 * len(replied) / len(delivered)) if delivered else 0
    today = datetime.now().astimezone().date()
    print(f"Sent: {len(contacted)}  delivered: {len(delivered)}  replied: {len(replied)}  reply rate: {rate:.0f}%"
          f"  bounced: {len(contacted) - len(delivered)}   sends today: {schedule.sends_on(today, schedule.sent_times())}/{config.DAILY_SEND_CAP}")
    unv = sum(1 for r in rows if r["verified"] != "yes")
    print(f"Unverified addresses in tracker: {unv}/{len(rows)}")
    print()
    for r in rows:
        if a.all or r["status"] not in ("closed", "skipped"):
            last = r["followup_sent_at"] or r["sent_at"] or r["drafted_at"] or r["added_at"]
            print(f"  #{r['id']:>3} {r['status']:<9} {r['name'][:22]:<22} {r['company'][:16]:<16} {r['email'][:32]:<32} "
                  f"{'v' if r['verified'] == 'yes' else '?'} {last[:10]} {('FC:' + r['factcheck'][:4]) if r['factcheck'] else ''}")


def cmd_show(a) -> None:
    for rid in a.ids:
        for kind in ("initial", "followup"):
            p = config.DRAFTS_DIR / f"{rid}-{kind}.txt"
            if p.exists():
                print(f"--- #{rid} {kind} ---\n{p.read_text()}")


def cmd_mark(a) -> None:
    with store.transaction() as rows:
        for rid in a.ids:
            r = store.get(rows, rid)
            r["status"] = a.status
            r["approved_at"] = r["scheduled_at"] = ""
            if a.status in ("closed", "replied", "bounced"):
                r[f"{a.status}_at"] = store.now_iso()
            if a.note:
                r["notes"] = (r["notes"] + " | " + a.note).strip(" |")
            print(f"  #{rid} -> {a.status}")


def cmd_edit(a) -> None:
    changes = {k: v for k, v in {"detail_quote": a.quote, "detail_url": a.detail_url, "detail": a.detail,
                                 "ask": a.ask, "ask_org": a.ask_org, "tz": a.tz}.items() if v is not None}
    if a.detail_url and fetch.is_blocked(a.detail_url):
        sys.exit("LinkedIn URLs can't be used.")
    with store.transaction() as rows:
        r = store.get(rows, a.id)
        if r["status"] not in ("new", "drafted"):
            sys.exit(f"#{a.id} is {r['status']}; only new/drafted rows can be edited")
        if r["status"] == "drafted":
            sys.exit(f"#{a.id} already has a Gmail draft; delete it in Gmail and `outreach mark new {a.id}` first")
        r.update(changes)
        if {"detail_quote", "detail_url"} & changes.keys():
            r["factcheck"] = ""  # must be re-verified before drafting
            if a.detail is None:
                r["detail"] = r["detail_quote"]
    print(f"  #{a.id} updated: {', '.join(changes)}" + ("; run: outreach factcheck " + a.id if not r["factcheck"] else ""))


def cmd_test_send(a) -> None:
    to = a.to.lower()
    if to not in TEST_RECIPIENTS:
        sys.exit(f"test-send only goes to your own addresses: {', '.join(sorted(TEST_RECIPIENTS))}")
    rows = store.load()
    src = store.get(rows, a.from_id) if a.from_id else next((r for r in reversed(rows) if r["subject"]), None)
    if not src:
        sys.exit("no drafted email to copy; run `outreach today` first")
    p = config.DRAFTS_DIR / f"{src['id']}-initial.txt"
    body = p.read_text().split("\n\n", 1)[1].rstrip() + "\n"
    subject = f"[TEST of #{src['id']} -> {src['email']}] {src['subject']}"
    svc = gmail.service()
    gmail.assert_sender(svc)
    times = schedule.sent_times()
    if schedule.sends_on(datetime.now().astimezone().date(), times) >= config.DAILY_SEND_CAP:
        sys.exit("daily send cap reached")
    attach = config.RESUME if src["ask"] in compose.INTERNSHIP_ASKS and config.RESUME.exists() else None
    d = gmail.create_draft(svc, gmail.build_message(to, subject, body, attach=attach))
    sent = gmail.send_draft(svc, d["id"])
    schedule.log_send("test", "test", to, datetime.now(timezone.utc))
    print(f"Test email sent from {config.SENDER} to {to} (Gmail id {sent['id']}). Check that inbox.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="outreach", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth", help="log in to Gmail as OUTREACH_SENDER (browser)").set_defaults(fn=cmd_auth)

    p = sub.add_parser("research", help="find people at companies from public sources")
    p.add_argument("targets", nargs="*", help='"Company", "Company=domain.com", comma list, or @file')
    p.add_argument("--url", action="append", help="extra public URL to mine (press, blog, team page)")
    p.add_argument("--ask", choices=["internship", "referral", "startup"], default="internship")
    p.add_argument("--yc-interns", type=int, default=0, metavar="N", help="also research N YC cos with intern postings")
    p.add_argument("--no-llm", action="store_true", help="structured sources only (no page extraction)")
    p.set_defaults(fn=cmd_research)

    p = sub.add_parser("add", help="add a person you found yourself")
    p.add_argument("--name", required=True); p.add_argument("--company", required=True)
    p.add_argument("--role", required=True); p.add_argument("--email", required=True)
    p.add_argument("--source", required=True, help="URL where you found them")
    p.add_argument("--quote", required=True, help="verbatim text from --detail-url for the personal line")
    p.add_argument("--detail-url", required=True)
    p.add_argument("--detail", help="short summary of the detail (defaults to quote)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--verified", action="store_true", help="email is published somewhere")
    g.add_argument("--unverified", action="store_true", help="email is a guess")
    p.add_argument("--email-how", help="where the email was published / how guessed")
    p.add_argument("--tz", help="recipient IANA tz, e.g. America/New_York")
    p.add_argument("--ask", choices=["internship", "referral", "startup"], default="internship")
    p.add_argument("--notes")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("factcheck", help="re-fetch cited URLs and verify quotes + names")
    p.add_argument("ids", nargs="*"); p.set_defaults(fn=cmd_factcheck)

    p = sub.add_parser("today", help="daily check + create up to 20 Gmail drafts and print them")
    p.add_argument("--local", action="store_true", help="preview only, no Gmail")
    p.add_argument("--ids", nargs="*", help="limit to these tracker ids")
    p.set_defaults(fn=cmd_today)

    p = sub.add_parser("approve", help="send approved drafts on a spaced, windowed schedule")
    p.add_argument("ids", nargs="+", help="tracker ids, or 'all'")
    p.add_argument("--background", action="store_true", help="run the sender detached")
    p.set_defaults(fn=cmd_approve)

    sub.add_parser("worker", help="resume sending the approved queue").set_defaults(fn=cmd_worker)
    sub.add_parser("check", help="detect replies/bounces, draft due follow-ups").set_defaults(fn=cmd_check)

    p = sub.add_parser("status", help="pipeline + reply rate"); p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("show", help="print saved draft text"); p.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("mark", help="manually set status (closed, replied, skipped, ...)")
    p.add_argument("status", choices=["closed", "replied", "bounced", "skipped", "new"])
    p.add_argument("ids", nargs="+"); p.add_argument("--note")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("edit", help="change a not-yet-drafted target's cited detail / ask")
    p.add_argument("id"); p.add_argument("--quote"); p.add_argument("--detail-url"); p.add_argument("--detail")
    p.add_argument("--ask", choices=["internship", "referral", "startup"])
    p.add_argument("--ask-org", help="team name used in the referral ask, e.g. Together")
    p.add_argument("--tz")
    p.set_defaults(fn=cmd_edit)

    p = sub.add_parser("test-send", help="send a copy of a draft to YOUR OWN address to prove the send path")
    p.add_argument("to"); p.add_argument("--from-id", help="tracker id of the draft to copy")
    p.set_defaults(fn=cmd_test_send)

    a = ap.parse_args(argv)
    config.ensure_data_dir()
    try:
        a.fn(a)
    except (gmail.GmailError, compose.llm.LLMError) as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("\ninterrupted - queue is saved; `outreach worker` resumes")


if __name__ == "__main__":
    main()
