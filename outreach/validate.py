"""Hard rules every draft must pass before it can become a Gmail draft."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config

HYPE = [
    "revolutionary", "game-changing", "game changer", "cutting-edge", "cutting edge", "passionate",
    "synergy", "rockstar", "rock star", "ninja", "guru", "world-class", "world class", "incredible",
    "amazing", "groundbreaking", "disruptive", "visionary", "unparalleled", "excited", "thrilled",
    "love", "huge fan", "big fan", "innovative", "best-in-class", "10x", "blown away", "inspiring",
    "impressive", "awesome", "fantastic", "phenomenal", "stellar", "unique opportunity", "dream",
    "transformative", "next-level", "next level", "brilliant", "genius", "legendary", "insane",
    "mind-blowing", "seamless", "supercharge", "skyrocket", "hustle", "elite", "exceptional",
    "admire", "fascinated", "obsessed", "truly", "deeply", "really", "super ",
]

FAKE_FAMILIARITY = [
    r"\bi'?ve (been )?follow(ed|ing)\b", r"\bi have (been )?follow(ed|ing)\b", r"\bbeen following\b",
    r"\blong[- ]?time (fan|admirer|reader|follower)\b", r"\bi'?ve (always )?admired\b",
    r"\bi'?ve been (watching|tracking)\b", r"\bas we (discussed|talked)\b", r"\bwe met\b",
    r"\byou (may|might) remember\b", r"\bi know you\b", r"\bfollow(ed|ing)? your (work|journey|career)\b",
    r"\bfan of (you|your)\b", r"\bkeep up with your\b", r"\bi'?ve read (all|everything)\b",
    r"\bgreat to (re)?connect\b", r"\bhope (you'?re|you are) (doing )?well\b",
]

CLICKBAIT = [
    "quick question", "urgent", "re:", "fwd:", "opportunity", "you won't believe", "don't miss",
    "last chance", "hey", "can you help", "important", "act now", "secret", "guaranteed",
]

# first-person statements allowed in the personal line (true by construction: the sender reads the source
# before approving). Any other "I/my/me" claim is treated as a possibly made-up claim.
ALLOWED_FIRST_PERSON = [r"\bi (came across|saw|noticed|read)\b", r"\bi'?m\b"]

STOP = set("""a an the and or of to in on for with at by from as is are was were be been it its this that
these those your you their they them his her he she we our us i me my about into over after before
than then so but not no yes can could would should will just also more most very via per up out
""".split())


@dataclass
class Result:
    ok: bool = True
    errors: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"\S+", text) if re.search(r"[A-Za-z0-9]", w)]


def allowed_numbers() -> set[str]:
    nums = set()
    for chunk in config.TLDR + list(config.ASKS.values()):
        nums.update(re.findall(r"\d[\d,\.]*\d|\d", chunk))
    nums.update({"2027", "2028"})
    return {n.rstrip(".,") for n in nums}


def _numbers(text: str) -> list[str]:
    return [n.rstrip(".,") for n in re.findall(r"\d[\d,\.]*\d|\d", text)]


def _content(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'\-\.]*[a-z0-9]|[a-z0-9]", text.lower()) if w not in STOP and len(w) > 2}


def check_subject(subject: str, res: Result) -> None:
    s = subject.strip()
    n = len(words(s))
    if n == 0:
        res.fail("subject is empty")
    if n > config.MAX_SUBJECT_WORDS:
        res.fail(f"subject has {n} words (max {config.MAX_SUBJECT_WORDS})")
    low = s.lower()
    for c in CLICKBAIT:
        if re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", low):
            res.fail(f"subject clickbait/vague phrase: {c!r}")
    if "!" in s or "?" in s:
        res.fail("subject contains ! or ?")
    if any(len(w) > 3 and w.isupper() and w.isalpha() for w in words(s)):
        res.fail("subject has an ALL-CAPS word")


def check_personal_line(line: str, quote: str, context: str, res: Result) -> None:
    """The personal line must be grounded in the cited quote (made-up claim guard)."""
    low = line.lower()
    if len(words(line)) > 35:
        res.fail("personal line longer than 35 words")
    if len(re.findall(r"[.!?](\s|$)", line.strip())) > 2:
        res.fail("personal line is more than two sentences")
    # first-person claims
    stripped = low
    for pat in ALLOWED_FIRST_PERSON:
        stripped = re.sub(pat, "", stripped)
    # lookbehind: ignore "me"/"i" glued to a domain or path (adabyte.me, x.io/i)
    if re.search(r"(?<![\w.\-@/])(i|my|me|mine|i've|i'd|i'll)\b", stripped):
        res.fail("personal line makes a first-person claim beyond 'I came across/saw/noticed/read'")
    # proper nouns must exist in the source
    src = (quote + " " + context).lower()
    tokens = re.findall(r"[A-Za-z][\w\.\-\+&']*", line)
    for i, t in enumerate(tokens):
        if i == 0 or t in {"I", "I'm", "Hi"}:
            continue
        if t[0].isupper() and t.lower().strip(".'") not in src:
            res.fail(f"personal line mentions {t!r}, which is not in the cited source")
    # must actually reference the detail
    overlap = _content(line) & _content(quote)
    if len(overlap) < 2:
        res.fail("personal line does not reference the cited detail (need >=2 shared content words)")


def validate(subject: str, body: str, *, personal_line: str, quote: str, context: str) -> Result:
    res = Result()
    check_subject(subject, res)
    company = context.split("|")[1].strip() if "|" in context else ""
    if company and company.lower() not in subject.lower():
        res.fail(f"subject must name the company ({company}) to be specific")
    n = len(words(body))
    if n > config.MAX_BODY_WORDS:
        res.fail(f"body has {n} words (max {config.MAX_BODY_WORDS})")
    full = (subject + "\n" + body).lower()
    for h in HYPE:
        if re.search(r"(?<![a-z])" + re.escape(h.strip()) + r"(?![a-z])", full):
            res.fail(f"hype word: {h.strip()!r}")
    for pat in FAKE_FAMILIARITY:
        if re.search(pat, full):
            res.fail(f"fake familiarity: /{pat}/")
    if (subject + body).count("!") > 0:
        res.fail("contains '!'")
    allowed = allowed_numbers()
    for num in _numbers(subject + "\n" + body):
        if num not in allowed:
            res.fail(f"metric/number {num!r} is not in the TLDR")
    for b in config.TLDR:
        if b not in body:
            res.fail("a TLDR bullet was altered or missing")
            break
    if personal_line not in body:
        res.fail("personal line missing from body")
    check_personal_line(personal_line, quote, context, res)
    return res
