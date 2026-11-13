import pytest

from outreach import config, gmail


def test_local_env_file_parsing(tmp_path, monkeypatch):
    f = tmp_path / "outreach.env"
    f.write_text('# comment\nOUTREACH_X_A=a@example.com   # trailing comment\nOUTREACH_X_B="Two Words"\n')
    monkeypatch.delenv("OUTREACH_X_A", raising=False)
    monkeypatch.delenv("OUTREACH_X_B", raising=False)
    config._load_local_env(f)
    import os
    assert os.environ["OUTREACH_X_A"] == "a@example.com"
    assert os.environ["OUTREACH_X_B"] == "Two Words"


def test_real_env_wins_over_file(tmp_path, monkeypatch):
    f = tmp_path / "outreach.env"
    f.write_text("OUTREACH_X_C=from-file\n")
    monkeypatch.setenv("OUTREACH_X_C", "from-env")
    config._load_local_env(f)
    import os
    assert os.environ["OUTREACH_X_C"] == "from-env"


def test_assert_sender_refuses_other_account(monkeypatch):
    monkeypatch.setattr(gmail, "whoami", lambda svc: "someone-else@example.com")
    with pytest.raises(gmail.GmailError):
        gmail.assert_sender(object())
    monkeypatch.setattr(gmail, "whoami", lambda svc: config.SENDER.upper())
    gmail.assert_sender(object())  # case-insensitive match is allowed


def test_no_sender_configured_refuses_everything(monkeypatch):
    monkeypatch.setattr(config, "SENDER", "")
    monkeypatch.setattr(gmail, "whoami", lambda svc: "")
    with pytest.raises(gmail.GmailError, match="OUTREACH_SENDER"):
        gmail.assert_sender(object())
