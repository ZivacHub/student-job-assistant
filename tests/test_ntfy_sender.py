"""ntfy phone push (N1): one JSON message to ntfy.sh with a safe topic, no redirects, plain-Croatian
errors. A pretend server (respx); nothing real is contacted."""

import json
import re

import httpx
import pytest
import respx

from app.notify.ntfy import (
    NETWORK_MESSAGE,
    NTFY_ACCOUNT,
    REFUSED_MESSAGE,
    NtfyError,
    NtfySender,
    build_ntfy_sender,
    new_topic,
    safe_click,
    valid_topic,
)
from app.notify.secrets import MemoryStore, SecretStoreError

TOPIC = "sja-abcd-efgh-ijkl-mnop"


# ---------------------------------------------------------------- the topic
def test_a_new_topic_is_long_random_and_easy_to_type():
    topics = {new_topic() for _ in range(200)}
    assert len(topics) == 200
    assert all(re.fullmatch(r"sja(-[a-z0-9]{4}){4}", t) for t in topics)
    assert all(valid_topic(t) for t in topics)


@pytest.mark.parametrize("bad", [None, "", "short", "a b c d e f g h", "../etc/passwd" * 2,
                                 "topic/with/slash", "x" * 65, "ünïcode-topic-name"])
def test_a_topic_that_is_not_plain_letters_digits_dashes_is_refused(bad):
    assert not valid_topic(bad)


# ---------------------------------------------------------------- what is sent
@respx.mock
def test_one_json_message_goes_to_the_ntfy_root_with_the_expected_fields():
    route = respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(200, json={}))
    NtfySender(TOPIC).publish(title="Novi posao", message="[87] Konobar – Osijek – 8 min – e-mail",
                              priority=4, click="https://www.studentski-poslovi.hr/oglas/1")
    assert route.call_count == 1
    request = route.calls[0].request
    assert str(request.url) == "https://ntfy.sh/" and request.method == "POST"
    assert json.loads(request.content) == {
        "topic": TOPIC, "title": "Novi posao", "message": "[87] Konobar – Osijek – 8 min – e-mail",
        "priority": 4, "tags": ["briefcase"], "click": "https://www.studentski-poslovi.hr/oglas/1"}


@respx.mock
def test_long_text_is_cut_line_breaks_are_flattened_and_an_empty_message_gets_a_word():
    route = respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(200))
    NtfySender(TOPIC).publish(title="T" * 300, message="a\n\nb\r\n" + "c" * 600, priority=99)
    NtfySender(TOPIC).publish(title="x", message="   ")
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert len(first["title"]) <= 100 and len(first["message"]) <= 250
    assert "\n" not in first["message"] and first["message"].startswith("a b c")
    assert first["priority"] == 3  # an impossible priority falls back to the normal one
    assert second["message"] == "obavijest"


@pytest.mark.parametrize("url, kept", [
    ("https://example.hr/oglas?id=1", True), ("http://example.hr/a", True),
    ("HTTPS://EXAMPLE.HR/A", True), ("javascript:alert(1)", False), ("data:text/html,x", False),
    ("ftp://example.hr/a", False), ("https://exa mple.hr/a", False), ("", False), (None, False),
    ("https://example.hr/" + "a" * 600, False)])
def test_only_an_ordinary_web_address_may_be_opened_by_a_tap(url, kept):
    assert (safe_click(url) is not None) is kept


@respx.mock
def test_an_unsafe_click_address_is_left_out_of_the_message():
    route = respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(200))
    NtfySender(TOPIC).publish(title="t", message="m", click="javascript:alert(1)")
    assert "click" not in json.loads(route.calls[0].request.content)


@respx.mock
@pytest.mark.parametrize("topic", ["", "a b", "../x", "x" * 65])
def test_a_bad_topic_sends_nothing(topic):
    route = respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(200))
    with pytest.raises(NtfyError) as caught:
        NtfySender(topic).publish(title="t", message="m")
    assert caught.value.code == "bad_message" and route.call_count == 0


# ---------------------------------------------------------------- failures
@respx.mock
@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"),
                                     httpx.ConnectTimeout("slow")])
def test_a_network_failure_is_a_plain_network_error(failure):
    respx.post("https://ntfy.sh/").mock(side_effect=failure)
    with pytest.raises(NtfyError) as caught:
        NtfySender(TOPIC).publish(title="t", message="m")
    assert caught.value.code == "network" and str(caught.value) == NETWORK_MESSAGE


@respx.mock
@pytest.mark.parametrize("status, code", [(429, "refused"), (403, "refused"), (404, "refused"),
                                          (500, "network"), (503, "network")])
def test_a_refusal_or_a_server_error_is_reported_in_plain_words(status, code):
    respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(status))
    with pytest.raises(NtfyError) as caught:
        NtfySender(TOPIC).publish(title="t", message="m")
    assert caught.value.code == code
    assert str(caught.value) == (REFUSED_MESSAGE if code == "refused" else NETWORK_MESSAGE)


@respx.mock
def test_a_redirect_is_never_followed_so_the_message_cannot_be_sent_elsewhere():
    respx.post("https://ntfy.sh/").mock(
        return_value=httpx.Response(301, headers={"location": "https://evil.example/"}))
    evil = respx.post("https://evil.example/").mock(return_value=httpx.Response(200))
    with pytest.raises(NtfyError):
        NtfySender(TOPIC).publish(title="t", message="m")
    assert evil.call_count == 0


@respx.mock
def test_the_topic_never_appears_in_an_error_or_a_repr():
    respx.post("https://ntfy.sh/").mock(return_value=httpx.Response(500))
    sender = NtfySender(TOPIC)
    with pytest.raises(NtfyError) as caught:
        sender.publish(title="t", message="m")
    assert TOPIC not in str(caught.value) and TOPIC not in repr(sender)
    assert caught.value.__cause__ is None


def test_a_client_given_to_the_sender_is_used_and_left_open():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    NtfySender(TOPIC, client=client).publish(title="t", message="m")
    NtfySender(TOPIC, client=client).publish(title="t", message="m")
    assert len(seen) == 2 and not client.is_closed


# ---------------------------------------------------------------- finding the stored topic
def test_the_sender_is_built_only_from_a_valid_stored_topic():
    store = MemoryStore()
    assert build_ntfy_sender(store) is None
    store.set(NTFY_ACCOUNT, "bad topic!")
    assert build_ntfy_sender(store) is None
    store.set(NTFY_ACCOUNT, TOPIC)
    assert build_ntfy_sender(store).topic == TOPIC

    class Broken(MemoryStore):
        def get(self, account):
            raise SecretStoreError("x")

    assert build_ntfy_sender(Broken()) is None
