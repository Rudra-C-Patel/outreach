"""Gmail API: OAuth (token in data/, never printed), drafts, sends, reply/bounce detection."""
from __future__ import annotations

import base64
import mimetypes
import os
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from . import config


class GmailError(RuntimeError):
    pass


def _save_token(creds) -> None:
    config.ensure_data_dir()
    fd = os.open(config.TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(config.TOKEN, 0o600)


def authorize() -> str:
    """Interactive browser login. Returns the authenticated address (never the token)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    require_sender()
    if not config.CREDENTIALS.exists():
        raise GmailError(f"missing {config.CREDENTIALS} - download the Desktop OAuth client JSON there (see README)")
    os.chmod(config.CREDENTIALS, 0o600)
    flow = InstalledAppFlow.from_client_secrets_file(str(config.CREDENTIALS), config.SCOPES)
    creds = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True, login_hint=config.SENDER,
        prompt="consent", access_type="offline",
        authorization_prompt_message=f"Opening browser for Google login (sign in as {config.SENDER})...",
        success_message="Outreach is authorized. You can close this tab.",
    )
    _save_token(creds)
    addr = whoami(service())
    if addr.lower() != config.SENDER:
        config.TOKEN.unlink(missing_ok=True)
        raise GmailError(f"logged in as {addr}, but only {config.SENDER} is allowed. Token deleted; run auth again.")
    return addr


def service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not config.TOKEN.exists():
        raise GmailError("not authorized yet - run: outreach auth")
    creds = Credentials.from_authorized_user_file(str(config.TOKEN), config.SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:  # refresh tokens expire after 7 days in Testing mode
                raise GmailError("Google token expired/revoked - run: outreach auth") from e
            _save_token(creds)
        else:
            raise GmailError("Google token invalid - run: outreach auth")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def whoami(svc) -> str:
    return svc.users().getProfile(userId="me").execute()["emailAddress"]


def require_sender() -> None:
    if not config.SENDER:
        raise GmailError("OUTREACH_SENDER is not set - put it in outreach.env (see outreach.env.example)")


def assert_sender(svc) -> None:
    require_sender()
    addr = whoami(svc)
    if addr.lower() != config.SENDER:
        raise GmailError(f"authenticated as {addr}; refusing - only {config.SENDER} may send")


def build_message(to: str, subject: str, body: str, *, attach: Path | None = None,
                  in_reply_to: str = "", references: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = formataddr((config.SENDER_NAME, config.SENDER))
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    if attach:
        ctype = mimetypes.guess_type(attach.name)[0] or "application/pdf"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(attach.read_bytes(), maintype=maintype, subtype=subtype, filename=attach.name)
    return msg


def _raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def create_draft(svc, msg: EmailMessage, thread_id: str = "") -> dict:
    assert_sender(svc)
    message = {"raw": _raw(msg)}
    if thread_id:
        message["threadId"] = thread_id
    return svc.users().drafts().create(userId="me", body={"message": message}).execute()


def draft_exists(svc, draft_id: str) -> bool:
    from googleapiclient.errors import HttpError
    try:
        svc.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
        return True
    except HttpError as e:
        if e.resp.status == 404:
            return False
        raise


def send_draft(svc, draft_id: str) -> dict:
    """Sends an existing draft (including any edits you made to it in Gmail)."""
    assert_sender(svc)
    return svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()


def message_header(svc, message_id: str, header: str) -> str:
    m = svc.users().messages().get(userId="me", id=message_id, format="metadata",
                                   metadataHeaders=[header]).execute()
    for h in m.get("payload", {}).get("headers", []):
        if h["name"].lower() == header.lower():
            return h["value"]
    return ""


def thread_state(svc, thread_id: str, recipient: str) -> str:
    """'replied' | 'bounced' | 'none' for a sent thread."""
    t = svc.users().threads().get(userId="me", id=thread_id, format="metadata",
                                  metadataHeaders=["From"]).execute()
    for m in t.get("messages", []):
        frm = next((h["value"] for h in m.get("payload", {}).get("headers", []) if h["name"] == "From"), "").lower()
        if "mailer-daemon" in frm or "postmaster" in frm:
            return "bounced"
        if config.SENDER not in frm:
            return "replied"
    return "none"


def bounce_search(svc, recipient: str, after_epoch: int) -> bool:
    q = f'from:(mailer-daemon OR postmaster) "{recipient}" after:{after_epoch}'
    res = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
    return bool(res.get("messages"))


def reply_search(svc, recipient: str, after_epoch: int) -> bool:
    """Catches replies that arrive outside the original thread."""
    res = svc.users().messages().list(userId="me", q=f"from:{recipient} after:{after_epoch}", maxResults=1).execute()
    return bool(res.get("messages"))
