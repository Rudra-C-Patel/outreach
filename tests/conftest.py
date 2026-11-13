import os

# Fixed, fictional identity for tests (set before outreach.config is imported).
os.environ.setdefault("OUTREACH_SENDER", "sender@example.com")
os.environ.setdefault("OUTREACH_TEST_RECIPIENT", "me@example.com")
os.environ.setdefault("OUTREACH_HOME", "/tmp/outreach-tests")
