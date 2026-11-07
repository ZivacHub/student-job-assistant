"""The phone push through ntfy.sh (plan 3.5, D38): Gmail does not buzz the phone for mail Ivan sends
to himself, so each alert also goes out as a one-line push to a secret topic.

What leaves this PC: one JSON message to https://ntfy.sh/ holding the topic, a short title, the
one-line subject of the ad, a priority and (only when it is a web address) the ad's own public link.
Never a phone number, a description, a draft, a name or the Gmail password. The topic is made up by
the app, kept only in Windows Credential Manager and works like a password: anyone who knows it can
read the pushes, so it is long and random. The server address is fixed (no setting, no redirects).
"""

import re
import secrets
from dataclasses import dataclass

import httpx

from app.notify.secrets import SecretStore, SecretStoreError

NTFY_URL = "https://ntfy.sh/"
NTFY_ACCOUNT = "ntfy-topic"  # the name under which the topic sits in the secret store
TIMEOUT_S = 10
MAX_TITLE = 100
MAX_MESSAGE = 250
MAX_CLICK = 500

_TOPIC = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

NETWORK_MESSAGE = ("Nije moguće spojiti se na ntfy. Provjerite internetsku vezu i pokušajte "
                   "ponovno.")
REFUSED_MESSAGE = "ntfy je odbio poruku (možda je dosegnuta granica slanja). Pokušajte kasnije."


class NtfyError(Exception):
    """`code`: "network", "refused", "bad_message". `str(error)` is plain Croatian."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def new_topic() -> str:
    """A long random topic such as `sja-k3m9-x2qa-p7we-4hzd` (about 82 bits): easy to type on a
    phone, impossible to guess."""
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(4)]
    return "sja-" + "-".join(groups)


def valid_topic(topic: str | None) -> bool:
    return bool(topic) and bool(_TOPIC.match(topic))


def _one_line(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 2].rstrip() + " …"


def safe_click(url: str | None) -> str | None:
    """Only an ordinary web address may be opened by a tap."""
    if not url or len(url) > MAX_CLICK or any(ch.isspace() for ch in url):
        return None
    return url if url.lower().startswith(("http://", "https://")) else None


@dataclass
class NtfySender:
    topic: str
    client: httpx.Client | None = None  # tests pass a client with a pretend transport
    url: str = NTFY_URL

    def __repr__(self) -> str:  # the topic is a secret: never in a log through a repr
        return "NtfySender()"

    def publish(self, *, title: str, message: str, priority: int = 3, click: str | None = None,
                tags: tuple[str, ...] = ("briefcase",)) -> None:
        """Send one push. Raises NtfyError."""
        if not valid_topic(self.topic):
            raise NtfyError("bad_message", "Naziv teme za ntfy nije ispravan.")
        body: dict = {
            "topic": self.topic,
            "title": _one_line(title, MAX_TITLE),
            "message": _one_line(message, MAX_MESSAGE) or "obavijest",
            "priority": priority if priority in (1, 2, 3, 4, 5) else 3,
            "tags": list(tags),
        }
        link = safe_click(click)
        if link:
            body["click"] = link
        owns_client = self.client is None
        client = self.client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        try:
            response = client.post(self.url, json=body)
        except httpx.HTTPError:
            raise NtfyError("network", NETWORK_MESSAGE) from None
        finally:
            if owns_client:
                client.close()
        if response.status_code == 429 or 400 <= response.status_code < 500:
            raise NtfyError("refused", REFUSED_MESSAGE)
        if response.status_code >= 300:  # 5xx, and any redirect is refused, not followed
            raise NtfyError("network", NETWORK_MESSAGE)


def build_ntfy_sender(store: SecretStore, client: httpx.Client | None = None) -> NtfySender | None:
    """The sender for the stored topic, or None when there is none."""
    try:
        topic = store.get(NTFY_ACCOUNT)
    except SecretStoreError:
        return None
    if not valid_topic(topic):
        return None
    return NtfySender(topic=topic, client=client)
