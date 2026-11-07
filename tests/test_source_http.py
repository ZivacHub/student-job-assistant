import httpx
import pytest
import respx

from app.sources.http import PoliteClient, RobotsDisallowed, SourceBlocked, SourceError

ROBOTS = "User-agent: *\nCrawl-delay: 10\nDisallow: /pretraga\nDisallow: /api/\n"
SITE = "https://example.test"


def client(sleeps=None, clock=None, **kw):
    sleeps = sleeps if sleeps is not None else []
    return PoliteClient(
        min_interval_s=kw.pop("min_interval_s", 1.0),
        user_agent="TestBot/1.0",
        http=httpx.Client(),
        sleep=sleeps.append,
        clock=clock or (lambda: 1000.0),
        **kw,
    ), sleeps


@respx.mock
def test_disallowed_path_is_never_fetched():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    page = respx.get(f"{SITE}/pretraga").mock(return_value=httpx.Response(200, text="x"))
    c, _ = client()
    with pytest.raises(RobotsDisallowed):
        c.get(f"{SITE}/pretraga?q=konobar")
    assert page.call_count == 0


@respx.mock
def test_allowed_path_is_fetched_and_crawl_delay_wins_over_interval():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get(f"{SITE}/poslovi").mock(return_value=httpx.Response(200, text="ok"))
    t = [0.0]
    c, sleeps = client(clock=lambda: t[0], min_interval_s=2.0)
    assert c.get(f"{SITE}/poslovi").text == "ok"
    t[0] += 3.0  # only 3 s later; robots says 10 s
    c.get(f"{SITE}/poslovi")
    assert sleeps == [pytest.approx(7.0)]


@respx.mock
def test_no_robots_file_means_allowed():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/a").mock(return_value=httpx.Response(200, text="a"))
    c, _ = client()
    assert c.get(f"{SITE}/a").text == "a"


@respx.mock
def test_403_is_blocked_and_not_retried():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    route = respx.get(f"{SITE}/a").mock(return_value=httpx.Response(403))
    c, _ = client()
    with pytest.raises(SourceBlocked):
        c.get(f"{SITE}/a")
    assert route.call_count == 1  # we do not hammer a site that said no


@respx.mock
def test_429_backs_off_using_retry_after_then_succeeds():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/a").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "45"}),
                     httpx.Response(200, text="fine")]
    )
    c, sleeps = client()
    assert c.get(f"{SITE}/a").text == "fine"
    assert 45.0 in sleeps


@respx.mock
def test_repeated_429_gives_up_as_blocked():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/a").mock(return_value=httpx.Response(429))
    c, _ = client(max_retries=2)
    with pytest.raises(SourceBlocked):
        c.get(f"{SITE}/a")


@respx.mock
def test_404_and_network_errors_raise_source_error():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/gone").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/down").mock(side_effect=httpx.ConnectError("boom"))
    c, _ = client(max_retries=2)
    with pytest.raises(SourceError):
        c.get(f"{SITE}/gone")
    with pytest.raises(SourceError):
        c.get(f"{SITE}/down")


@respx.mock
def test_robots_403_means_stay_away():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(403))
    c, _ = client()
    with pytest.raises(RobotsDisallowed):
        c.get(f"{SITE}/anything")
