"""A-05: every redirect hop is checked against that host's robots.txt before it is fetched."""

import httpx
import pytest
import respx

from app.sources.http import PoliteClient, RobotsDisallowed, SourceError

SITE = "https://example.test"
OTHER = "https://other.test"


def client(sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return PoliteClient(min_interval_s=1.0, user_agent="TestBot/1.0", http=httpx.Client(),
                        sleep=sleeps.append, clock=lambda: 1000.0), sleeps


@respx.mock
def test_same_host_redirect_is_followed_and_throttled():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/old").mock(return_value=httpx.Response(301, headers={"Location": "/new"}))
    respx.get(f"{SITE}/new").mock(return_value=httpx.Response(200, text="new"))
    c, sleeps = client()
    assert c.get(f"{SITE}/old").text == "new"
    assert len(sleeps) == 1  # the second hop waited its turn


@respx.mock
def test_cross_host_redirect_to_disallowed_path_is_refused():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{OTHER}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /jobs\n")
    )
    respx.get(f"{SITE}/a").mock(
        return_value=httpx.Response(302, headers={"Location": f"{OTHER}/jobs/1"})
    )
    target = respx.get(f"{OTHER}/jobs/1").mock(return_value=httpx.Response(200, text="secret"))
    c, _ = client()
    with pytest.raises(RobotsDisallowed):
        c.get(f"{SITE}/a")
    assert target.call_count == 0  # never fetched


@respx.mock
def test_cross_host_redirect_to_allowed_path_works():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{OTHER}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/a").mock(
        return_value=httpx.Response(301, headers={"Location": f"{OTHER}/b"})
    )
    respx.get(f"{OTHER}/b").mock(return_value=httpx.Response(200, text="ok"))
    c, _ = client()
    assert c.get(f"{SITE}/a").text == "ok"


@respx.mock
def test_redirect_loop_gives_up():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/loop").mock(return_value=httpx.Response(302, headers={"Location": "/loop"}))
    c, _ = client()
    with pytest.raises(SourceError, match="too many redirects"):
        c.get(f"{SITE}/loop")


@respx.mock
def test_redirect_without_location_is_an_error():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/x").mock(return_value=httpx.Response(302))
    c, _ = client()
    with pytest.raises(SourceError, match="without Location"):
        c.get(f"{SITE}/x")


@respx.mock
def test_robots_txt_that_redirects_is_followed():
    """e.g. moj-posao.net -> mojposao.hr: the redirect target's rules apply, not 'allow all'."""
    respx.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(301, headers={"Location": f"{OTHER}/robots.txt"})
    )
    respx.get(f"{OTHER}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    page = respx.get(f"{SITE}/private/x").mock(return_value=httpx.Response(200, text="x"))
    c, _ = client()
    with pytest.raises(RobotsDisallowed):
        c.get(f"{SITE}/private/x")
    assert page.call_count == 0


def test_close_only_closes_clients_it_created():
    own = PoliteClient(user_agent="T/1")
    own.close()
    assert own.http.is_closed
    shared = httpx.Client()
    PoliteClient(user_agent="T/1", http=shared).close()
    assert not shared.is_closed
    shared.close()
