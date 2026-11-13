"""Assemble a draft: model writes subject + one personal line; everything else is fixed template."""
from __future__ import annotations

from dataclasses import dataclass

from . import config, llm, validate

SYSTEM = """You write one line of a cold email for Rudra Patel, a UC San Diego Cognitive Science (ML)
student who builds AI products. Voice: plain, direct, specific, calm. No flattery, no hype, no
exclamation marks, no claims of familiarity ("I've followed your work"), no numbers.

You get ONE cited fact about the recipient. Write:
- "line": ONE sentence (max 24 words) that states that fact accurately and, if natural, ties it to
  why Rudra is writing. Use only words/names that appear in the fact, the recipient's name/company,
  or plain everyday words. You may start with "I came across", "I saw" or "I read", or address them
  directly ("Your ..."). Do not invent anything not in the fact. Do not mention Rudra's projects.
- "subject": 4-9 words, must contain the company name, specific to the ask, no clickbait, no ! or ?,
  no numbers except 2027. Example: "Summer 2027 ML internship at Acme".
The line must read naturally as plain English. Good example:
  "I saw your post on Acme's queue rewrite, including how it now replays failed jobs safely."
  (only if that is what the fact says). Bad: "I read your research interests focus on X." (clunky)
Return JSON: {"subject": "...", "line": "..."}"""


@dataclass
class Draft:
    subject: str
    body: str
    personal_line: str
    attach_resume: bool
    errors: list[str]


def first_name(name: str) -> str:
    return name.strip().split()[0] if name.strip() else "there"


INTERNSHIP_ASKS = {"internship", "referral"}  # resume attached only for these


def ask_text(ask: str, org: str = "") -> str:
    return config.ASKS[ask].format(org=org)


def build_body(name: str, line: str, ask: str, org: str = "") -> str:
    bullets = "\n".join(f"- {b}" for b in config.TLDR)
    return (
        f"Hi {first_name(name)},\n\n{line}\n\nQuick TLDR on me:\n{bullets}\n\n"
        f"{ask_text(ask, org)}\n\n{config.SIGNOFF}"
    )


def _context(row: dict) -> str:
    return " | ".join([row["name"], row["company"], row["role"], row["detail"]])


def compose(row: dict, attempts: int = 4) -> Draft:
    ask = row["ask"] or "internship"
    user = (
        f"Recipient: {row['name']}, {row['role']} at {row['company']}\n"
        f"Ask type: {'Summer 2027 internship' if ask in INTERNSHIP_ASKS else 'joining the startup'}\n"
        f"Cited fact (summary): {row['detail']}\n"
        f"Verbatim source text: \"{row['detail_quote']}\"\n"
        f"Source URL: {row['detail_url']}"
    )
    errors: list[str] = []
    for i in range(attempts):
        prompt = user
        if errors:
            prompt += "\n\nYour previous attempt was rejected for: " + "; ".join(errors) + ". Fix it."
        out = llm.chat_json(SYSTEM, prompt, temperature=0.3 + 0.15 * i)
        subject = str(out.get("subject", "")).strip().strip('"')
        line = " ".join(str(out.get("line", "")).split())
        body = build_body(row["name"], line, ask, row.get("ask_org") or row["company"])
        res = validate.validate(subject, body, personal_line=line, quote=row["detail_quote"], context=_context(row))
        if res.ok:
            return Draft(subject, body, line, ask in INTERNSHIP_ASKS, [])
        errors = res.errors
    return Draft(subject, body, line, ask in INTERNSHIP_ASKS, errors)
