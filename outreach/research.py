"""Find real people at a company from PUBLIC sources only. Never LinkedIn. Never invent people.

Sources: YC company page (structured founder data), company site pages, Greenhouse/Lever/Ashby public
job APIs, GitHub public org members + their public profile email, linked personal sites, plus any
extra URLs you pass (press, blog posts).
"""
from __future__ import annotations

import html as htmllib
import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from . import config, fetch, llm

TARGET_ROLE = re.compile(
    r"founder|\bceo\b|\bcto\b|\bcoo\b|chief|head of|\bvp\b|vice president|director|recruit|talent|"
    r"people ops|hiring|engineering manager|\blead\b|president|university|campus", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
GENERIC_LOCAL = {"info", "hello", "hi", "contact", "support", "help", "jobs", "careers", "press", "media",
                 "team", "sales", "privacy", "legal", "security", "admin", "billing", "founders", "hr",
                 "recruiting", "talent", "noreply", "no-reply", "partners", "investors", "abuse", "dmca"}
SITE_PATHS = ["", "/about", "/about-us", "/team", "/company", "/careers", "/jobs", "/people", "/contact"]
TZ_HINTS = [
    (r"san francisco|\bsf\b|bay area|palo alto|mountain view|menlo park|san mateo|berkeley|oakland|san jose|"
     r"los angeles|san diego|seattle|redmond|bellevue|portland|sunnyvale|redwood city|vancouver", "America/Los_Angeles"),
    (r"new york|nyc|brooklyn|boston|cambridge, ma|toronto|atlanta|miami|washington|philadelphia|montreal|waterloo", "America/New_York"),
    (r"austin|chicago|dallas|houston|minneapolis", "America/Chicago"),
    (r"denver|boulder|salt lake", "America/Denver"),
    (r"london", "Europe/London"), (r"paris", "Europe/Paris"), (r"berlin|munich", "Europe/Berlin"),
    (r"bangalore|bengaluru|mumbai|delhi|hyderabad", "Asia/Kolkata"),
]


@dataclass
class Person:
    name: str
    role: str
    company: str
    source: str
    email: str = ""
    email_how: str = ""
    verified: bool = False
    detail: str = ""
    detail_quote: str = ""
    detail_url: str = ""
    tz: str = config.DEFAULT_TZ
    links: list[str] = field(default_factory=list)


@dataclass
class Company:
    name: str
    domain: str = ""
    yc_slug: str = ""
    location: str = ""
    pages: list[fetch.Page] = field(default_factory=list)
    facts: list[tuple[str, str, str]] = field(default_factory=list)  # (summary, quote, url)
    github_org: str = ""


# ---------- helpers ----------

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def tz_for(location: str) -> str:
    low = (location or "").lower()
    for pat, tz in TZ_HINTS:
        if re.search(pat, low):
            return tz
    return config.DEFAULT_TZ


def flatten_strings(obj) -> list[str]:
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out += flatten_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            out += flatten_strings(v)
    return out


def searchable(page: fetch.Page) -> str:
    """Visible text + strings inside embedded page JSON (YC/Inertia data-page, JSON APIs)."""
    parts = [page.text]
    m = re.search(r'data-page="([^"]+)"', page.html or "")
    if m:
        try:
            parts += flatten_strings(json.loads(htmllib.unescape(m.group(1))))
        except json.JSONDecodeError:
            pass
    if (page.html or "").lstrip().startswith(("{", "[")):
        try:
            parts += flatten_strings(json.loads(page.html))
        except json.JSONDecodeError:
            pass
    extra = re.sub(r"<[^>]+>", " ", " ".join(parts[1:]))  # embedded strings may hold HTML
    return fetch.normalize(parts[0] + " " + extra)


def has_mx(domain: str) -> bool:
    import dns.resolver
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except Exception:
        return False


def name_variants(name: str) -> dict[str, str]:
    parts = [p for p in re.sub(r"[^a-z ]", "", name.lower()).split() if p]
    if len(parts) < 2:
        return {"first": parts[0]} if parts else {}
    f, l = parts[0], parts[-1]
    return {"first": f, "first.last": f"{f}.{l}", "firstlast": f + l, "flast": f[0] + l,
            "f.last": f"{f[0]}.{l}", "firstl": f + l[0], "last": l}


def match_email(name: str, emails: set[str], domain: str = "") -> str:
    v = name_variants(name)
    for e in sorted(emails):
        local, _, dom = e.lower().partition("@")
        if domain and dom != domain and not dom.endswith("." + domain):
            continue
        if local in v.values() and local not in GENERIC_LOCAL:
            return e
    return ""


def infer_pattern(known: list[tuple[str, str]]) -> str:
    counts: dict[str, int] = {}
    for name, email in known:
        local = email.split("@")[0].lower()
        for pat, val in name_variants(name).items():
            if local == val:
                counts[pat] = counts.get(pat, 0) + 1
    return max(counts, key=counts.get) if counts else "first"


OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9\-]+(?:\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*[A-Za-z0-9\-]+)+)",
    re.I)


def deobfuscate(text: str) -> set[str]:
    """'ada [at] adabyte (dot) dev' -> ada@adabyte.dev (human-readable published form)."""
    out = set()
    for local, rest in OBFUSCATED_RE.findall(text):
        dom = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", rest, flags=re.I)
        out.add(f"{local}@{dom}".lower())
    return out


def emails_in(page: fetch.Page) -> set[str]:
    from urllib.parse import unquote
    text = searchable(page)
    found = set(EMAIL_RE.findall(text)) | deobfuscate(text)
    found |= {unquote(m) for m in re.findall(r'mailto:([^"\'?>\s]+)', page.html or "")}
    return {e.strip(".").lower() for e in found
            if EMAIL_RE.fullmatch(e.strip(".")) and not e.lower().endswith((".png", ".jpg", ".svg", ".webp"))}


# ---------- source collectors ----------

def yc_company(c: Company) -> list[Person]:
    slug = c.yc_slug or slugify(c.name)
    url = f"https://www.ycombinator.com/companies/{slug}"
    page = fetch.get(url)
    m = re.search(r'data-page="([^"]+)"', page.html or "")
    if not (page.ok and m):
        return []
    data = json.loads(htmllib.unescape(m.group(1)))["props"].get("company") or {}
    if not data:
        return []
    if not c.yc_slug and slugify(data.get("name", "")) != slugify(c.name):
        return []  # different company at that slug
    c.yc_slug = slug
    c.pages.append(page)
    c.location = data.get("location") or c.location
    if not c.domain and data.get("website"):
        c.domain = (urlparse(data["website"]).hostname or "").removeprefix("www.")
    if data.get("github_url"):
        c.github_org = urlparse(data["github_url"]).path.strip("/").split("/")[0]
    if data.get("one_liner"):
        c.facts.append((f"YC ({data.get('batch_name', '')}) company: {data['one_liner']}", data["one_liner"], url))
    people = []
    for f in data.get("founders", []):
        if not f.get("full_name") or not f.get("is_active", True):
            continue
        p = Person(f["full_name"].strip(), f.get("title") or "Founder", data.get("name", c.name), url,
                   tz=tz_for(c.location))
        bio = fetch.normalize(f.get("founder_bio") or "")
        sent = next((s for s in re.split(r"(?<=[.!?])\s+", bio) if len(s.split()) >= 6), "")
        if sent:
            p.detail, p.detail_quote, p.detail_url = f"Founder bio: {sent}", sent, url
        people.append(p)
    return people


def ats_facts(c: Company) -> None:
    """Public job APIs: used for real, citable hiring details (esp. intern roles)."""
    slugs = {slugify(c.name), slugify(c.name).replace("-", ""), (c.domain or "").split(".")[0]}
    for s in filter(None, slugs):
        gh = fetch.get_json(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
        for j in (gh or {}).get("jobs", []):
            _job_fact(c, j.get("title", ""), j.get("absolute_url", ""), j.get("location", {}).get("name", ""))
        ash = fetch.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
        for j in (ash or {}).get("jobs", []):
            _job_fact(c, j.get("title", ""), j.get("jobUrl", ""), j.get("location", ""))
        lev = fetch.get_json(f"https://api.lever.co/v0/postings/{s}?mode=json")
        for j in lev if isinstance(lev, list) else []:
            _job_fact(c, j.get("text", ""), j.get("hostedUrl", ""), (j.get("categories") or {}).get("location", ""))


def _job_fact(c: Company, title: str, url: str, location: str) -> None:
    if not title or not url:
        return
    if re.search(r"intern|new grad|university|student|early career", title, re.I):
        c.facts.insert(0, (f"{c.name} is hiring: {title}", fetch.normalize(title), url))
        if location and not c.location:
            c.location = location


def site_pages(c: Company, extra_urls: list[str]) -> None:
    if c.domain:
        base = f"https://{c.domain}"
        for path in SITE_PATHS:
            p = fetch.get(base + path)
            if p.ok and p.text and all(p.url != q.url for q in c.pages):
                c.pages.append(p)
    for u in extra_urls:
        p = fetch.get(u)
        if p.ok:
            c.pages.append(p)


EXTRACT_SYS = """Extract REAL people named on this web page who work at the given company in a leadership,
founder, recruiting/talent, or hiring-manager/engineering-lead role. Only people explicitly named on the
page. For each give a verbatim "quote" (copied exactly, <=200 chars) from the page that contains their
name and shows their role. Also give "fact": a verbatim sentence from the page about something concrete
this person or the company did/built/wrote (or "" if none). Never guess. If none, return {"people": []}.
Return JSON {"people": [{"name": "...", "role": "...", "quote": "...", "fact": "..."}]}"""


def llm_people(c: Company, page: fetch.Page) -> list[Person]:
    text = searchable(page)
    if len(text) < 200:
        return []
    out = llm.chat_json(EXTRACT_SYS, f"Company: {c.name}\nURL: {page.url}\nPAGE TEXT:\n{text[:14000]}", temperature=0.1)
    low = text.lower()
    people = []
    for d in out.get("people", []) if isinstance(out, dict) else []:
        name = fetch.normalize(str(d.get("name", "")))
        role = fetch.normalize(str(d.get("role", "")))
        quote = fetch.normalize(str(d.get("quote", "")))
        fact = fetch.normalize(str(d.get("fact", "")))
        # grounding: name and quote must literally be on the page; role must be a target role
        if len(name.split()) < 2 or name.lower() not in low or not quote or quote.lower() not in low:
            continue
        if name.split()[0].lower() not in quote.lower() or not TARGET_ROLE.search(role + " " + quote):
            continue
        p = Person(name, role, c.name, page.url, tz=tz_for(c.location))
        if fact and fact.lower() in low and len(fact.split()) >= 6:
            p.detail, p.detail_quote, p.detail_url = fact, fact, page.url
        people.append(p)
    return people


def github_people(c: Company) -> list[Person]:
    org = c.github_org or slugify(c.name).replace("-", "")
    members = fetch.get_json(f"https://api.github.com/orgs/{org}/public_members?per_page=50")
    if not isinstance(members, list):
        return []
    people = []
    for m in members[:30]:
        u = fetch.get_json(f"https://api.github.com/users/{m['login']}")
        if not u or not u.get("name") or not u.get("email"):
            continue
        bio = (u.get("bio") or "") + " " + (u.get("company") or "")
        if not TARGET_ROLE.search(bio):
            continue
        url = f"https://github.com/{m['login']}"
        p = Person(u["name"], fetch.normalize(u.get("bio") or "GitHub org member"), c.name, url,
                   email=u["email"].lower(), email_how=f"published: public GitHub profile email ({url})",
                   verified=True, tz=tz_for(u.get("location") or c.location))
        if u.get("blog"):
            p.links.append(u["blog"] if u["blog"].startswith("http") else "https://" + u["blog"])
        people.append(p)
    return people


def hn_launch(c: Company) -> None:
    """Launch HN / Show HN posts by the company: founders often publish their email there."""
    q = fetch.get_json(f"https://hn.algolia.com/api/v1/search?tags=story&query={c.name.replace(' ', '%20')}")
    for h in (q or {}).get("hits", [])[:20]:
        title = h.get("title") or ""
        if not re.match(rf"(Launch|Show) HN: {re.escape(c.name)}\b", title, re.I):
            continue
        body = htmllib.unescape(h.get("story_text") or "")
        url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        c.pages.append(fetch.Page(url, 200, body, fetch.normalize(title + " " + fetch.html_to_text(body))))


def github_lookup(p: Person, c: Company) -> None:
    """Find a person's own GitHub profile by name; accept only if the profile names the company."""
    q = fetch.get_json(f"https://api.github.com/search/users?q={p.name.replace(' ', '+')}+in:name&per_page=5")
    keys = {c.name.lower(), slugify(c.name).replace("-", ""), (c.domain or "~").lower(), (c.domain or "~").split(".")[0]}
    for item in (q or {}).get("items", [])[:5]:
        u = fetch.get_json(f"https://api.github.com/users/{item['login']}")
        if not u or (u.get("name") or "").lower() != p.name.lower():
            continue
        prof = " ".join([u.get("company") or "", u.get("bio") or "", u.get("blog") or ""]).lower()
        if not any(k and k in prof for k in keys):
            continue
        url = f"https://github.com/{item['login']}"
        if u.get("email"):
            p.email, p.email_how, p.verified = u["email"].lower(), f"published: public GitHub profile email ({url})", True
        if u.get("blog"):
            p.links.append(u["blog"] if u["blog"].startswith("http") else "https://" + u["blog"])
        return


# ---------- orchestration ----------

def research_company(item: str, extra_urls: list[str], ask: str, use_llm: bool = True, log=print) -> list[Person]:
    name, _, domain = item.partition("=")
    c = Company(name.strip(), domain.strip().removeprefix("https://").removeprefix("www.").strip("/"))
    if "." in c.name and not c.domain:
        c.domain, c.name = c.name, c.name.split(".")[0].title()
    log(f"[{c.name}] YC directory...")
    people = yc_company(c)
    log(f"[{c.name}] domain={c.domain or '?'}; job boards...")
    ats_facts(c)
    log(f"[{c.name}] company site + extra URLs...")
    site_pages(c, extra_urls)
    if use_llm:
        for page in c.pages:
            if "ycombinator.com" in page.url:
                continue
            try:
                people += llm_people(c, page)
            except llm.LLMError as e:
                log(f"  ! extraction skipped for {page.url}: {e}")
    log(f"[{c.name}] Launch HN posts...")
    hn_launch(c)
    if use_llm:
        for page in [pg for pg in c.pages if "news.ycombinator.com" in pg.url]:
            try:
                people += llm_people(c, page)
            except llm.LLMError as e:
                log(f"  ! extraction skipped for {page.url}: {e}")
    log(f"[{c.name}] GitHub...")
    people += github_people(c)

    # dedupe by name
    uniq: dict[str, Person] = {}
    for p in people:
        k = p.name.lower()
        if k not in uniq or (p.email and not uniq[k].email):
            uniq[k] = p
    people = list(uniq.values())

    # emails: published first
    all_emails: set[str] = set()
    for pg in c.pages:
        all_emails |= emails_in(pg)
    for p in people:
        if not p.email and "founder" in p.role.lower():
            github_lookup(p, c)
        for link in p.links[:2]:
            try:
                pg = fetch.get(link)
                if pg.ok:
                    e = match_email(p.name, emails_in(pg))
                    if e and not p.email:
                        p.email, p.email_how, p.verified = e, f"published on personal site ({pg.url})", True
            except fetch.Blocked:
                pass
        if not p.email:
            e = match_email(p.name, all_emails, c.domain)
            if e:
                src = next((pg.url for pg in c.pages if e in emails_in(pg)), c.domain)
                p.email, p.email_how, p.verified = e, f"published on {src}", True

    # pattern guesses (unverified), only where the domain accepts mail
    known = [(p.name, p.email) for p in people if p.verified and c.domain and p.email.endswith("@" + c.domain)]
    pattern = infer_pattern(known)
    mx = bool(c.domain) and has_mx(c.domain)
    for p in people:
        if not p.email and mx:
            local = name_variants(p.name).get(pattern) or name_variants(p.name).get("first")
            if local:
                basis = "inferred from published addresses" if known else "common startup default"
                p.email, p.email_how, p.verified = f"{local}@{c.domain}", f"pattern guess ({pattern}@; {basis}; MX ok)", False

    # company-level detail fallback (real, cited)
    for p in people:
        if not p.detail and c.facts:
            p.detail, p.detail_quote, p.detail_url = c.facts[0]
        p.tz = p.tz if p.tz != config.DEFAULT_TZ else tz_for(c.location)

    kept = [p for p in people if p.email and p.detail_quote and TARGET_ROLE.search(p.role)]
    skipped = len(people) - len(kept)
    if skipped:
        log(f"[{c.name}] skipped {skipped} (no plausible address, no citable detail, or non-target role)")
    return kept


def cap_unverified(people: list[Person]) -> tuple[list[Person], int]:
    """Unverified (pattern-guessed) <= 20% of the batch: U <= floor(0.25 * P)."""
    verified = [p for p in people if p.verified]
    unverified = [p for p in people if not p.verified]
    allowed = math.floor(len(verified) * config.UNVERIFIED_MAX_FRACTION / (1 - config.UNVERIFIED_MAX_FRACTION))
    # prefer founders among guesses (startup founders most often use first@)
    unverified.sort(key=lambda p: 0 if "founder" in p.role.lower() else 1)
    return verified + unverified[:allowed], max(0, len(unverified) - allowed)


def yc_intern_companies(limit: int = 5, scan: int = 120, log=print) -> list[tuple[str, str, str, str]]:
    """(company, slug, posting title, posting url): YC companies currently hiring with an intern posting.

    Candidate list: yc-oss public dataset of hiring YC companies (recent batches, AI/software first);
    each company's own public YC jobs page is then checked for intern titles.
    """
    data = fetch.get_json("https://yc-oss.github.io/api/companies/hiring.json") or []
    def score(co):
        tags = " ".join(co.get("tags") or []) + " " + (co.get("industry") or "")
        ai = bool(re.search(r"\bAI\b|machine learning|developer tools|infrastructure|B2B", tags, re.I))
        us = "United States of America" in (co.get("regions") or []) or "America / Canada" in (co.get("regions") or [])
        return (ai and us, co.get("launched_at") or 0)
    data.sort(key=score, reverse=True)
    out = []
    for co in data[:scan]:
        page = fetch.get(f"https://www.ycombinator.com/companies/{co['slug']}/jobs")
        m = re.search(r'data-page="([^"]+)"', page.html or "")
        if not m:
            continue
        for j in json.loads(htmllib.unescape(m.group(1)))["props"].get("jobPostings", []) or []:
            if re.search(r"\bintern(ship)?\b", j.get("title", ""), re.I):
                url = urljoin("https://www.ycombinator.com", j.get("url") or f"/companies/{co['slug']}/jobs")
                out.append((co["name"], co["slug"], j["title"].strip(), url))
                log(f"  found: {co['name']} - {j['title'].strip()}")
                break
        if len(out) >= limit:
            break
    return out


def posting_detail(url: str, title: str) -> tuple[str, str]:
    """(summary, verbatim quote) from a job posting page: first concrete sentence about the role/product."""
    page = fetch.get(url)
    text = searchable(page) if page.ok else ""
    after = text.split("About the role", 1)[-1] if "About the role" in text else ""
    for sent in re.split(r"(?<=[.!?])\s+", after):
        sent = sent.replace("About the role", "").strip()
        if 8 <= len(sent.split()) <= 40 and not re.search(r"\d", sent):
            return f"Hiring: {title}. The posting says: {sent}", sent
    return f"Hiring: {title}", title.strip()
