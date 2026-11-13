"""Fixed settings. The TLDR text and numbers are NOT to be edited by code or the model."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("OUTREACH_HOME", Path.home() / "outreach"))


def _load_local_env(path: Path) -> None:
    """Personal settings (sender address, resume path) live in a gitignored outreach.env, not in code.
    Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip("\""))


_load_local_env(ROOT / "outreach.env")
DATA = ROOT / "data"
TRACKER = DATA / "tracker.csv"
CREDENTIALS = DATA / "credentials.json"
TOKEN = DATA / "token.json"
SEND_LOG = DATA / "send_log.csv"
CACHE = DATA / "cache"
DRAFTS_DIR = DATA / "drafts"
LOCK = DATA / ".lock"

SENDER = os.environ.get("OUTREACH_SENDER", "").strip().lower()  # the ONLY account allowed to draft/send
SENDER_NAME = os.environ.get("OUTREACH_SENDER_NAME", "Rudra Patel")
RESUME = Path(os.path.expanduser(os.environ.get("OUTREACH_RESUME", "~/resume.pdf")))
TEST_RECIPIENT = os.environ.get("OUTREACH_TEST_RECIPIENT", "").strip().lower()  # your own inbox for test-send

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "bonsai-2:27b"

DAILY_DRAFT_LIMIT = 20
DAILY_SEND_CAP = 20
SEND_GAP_MIN_S = 120
SEND_GAP_MAX_S = 360
SEND_WINDOW = (8, 18)  # 8 AM inclusive -> 6 PM exclusive, recipient local time
FOLLOWUP_AFTER_DAYS = 6
MAX_BODY_WORDS = 120
MAX_SUBJECT_WORDS = 10
UNVERIFIED_MAX_FRACTION = 0.20
DEFAULT_TZ = "America/Los_Angeles"

USER_AGENT = "outreach-research/0.1 (personal job-search tool; respects robots.txt)"

TLDR = [
    "Founder, DealerScope AI (dealerscope.app): solo-built AI revenue-recovery platform for car "
    "dealerships; FastAPI + React on Railway/Vercel, 1,000+ automated tests, LLM fallback logic, in production",
    "Jarvis: private local LLM assistant (Ollama + RAG over my notes + chat history, local "
    "speech-to-text, secret scrubber with test suite)",
    "Backtester over 5,300 US stocks with walk-forward validation that caught overfitting "
    "(github.com/Rudra-C-Patel/trading-bot)",
    "Cognitive Science (ML & Neural Computation) @ UC San Diego, graduating June 2028",
]

ASKS = {
    "internship": (
        "Would you be open to a 15-minute chat about Summer 2027 AI/ML/SWE internships, "
        "or pointing me to the right person?"
    ),
    "referral": (
        "Would you be open to pointing me to the right person on {org}'s recruiting team "
        "for Summer 2027 AI/ML/SWE internships?"
    ),
    "startup": (
        "Would you be open to a 15-minute chat about joining the team, "
        "or pointing me to the right person?"
    ),
}

SIGNOFF = "Best,\nRudra Patel"

FOLLOWUP_BODY = (
    "Hi {first},\n\n"
    "Bumping this in case it got buried. Still glad to do a quick 15-minute chat, "
    "or to be pointed to the right person. Thanks either way.\n\n" + SIGNOFF
)


def ensure_data_dir() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    for d in (CACHE, DRAFTS_DIR):
        d.mkdir(exist_ok=True)
        os.chmod(d, 0o700)
