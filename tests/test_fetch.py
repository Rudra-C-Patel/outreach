import httpx
import pytest

from outreach import fetch


def test_redirect_to_linkedin_is_never_requested():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "https://www.linkedin.com/in/someone"})
        return httpx.Response(200, text="should never be reached")

    with fetch._client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(fetch.Blocked):
            c.get("https://example.com/profile")
    assert seen == ["example.com"]  # the LinkedIn hop was refused before it was sent


@pytest.mark.parametrize("url", ["https://lnkd.in/x", "https://ae.linkedin.com/in/x", "https://media.licdn.com/a.png"])
def test_blocked_hosts_refused_before_any_request(url):
    with pytest.raises(fetch.Blocked):
        fetch.get(url)
