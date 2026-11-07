"""Reading the "reply DA" answers from Gmail (plan 5.3b, D42).

The app password opens the whole mailbox, so the limits are kept here and proven by a test:
- one connection per round, only while an alert waits for an answer (the caller decides);
- the folder INBOX, opened READ-ONLY (IMAP "EXAMINE"): nothing can be marked read, moved or
  deleted, whatever a bug might try;
- Gmail itself searches for the one-time markers of the waiting alerts in the title, so mail that
  is not about them is never even listed;
- of those, only the header lines are fetched (`BODY.PEEK[HEADER]`); the text is fetched only for
  a mail the caller has already found trustworthy.
"""

import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import imap_tools
from imap_tools import AND, MailBox

from app.notify.gmail import valid_address
from app.notify.secrets import SecretStore, SecretStoreError

HOST = "imap.gmail.com"
PORT = 993
TIMEOUT_S = 20
FOLDER = "INBOX"
# the app's marker word in a title (SJA-K7M2-Q9XA); the exact code is checked after the search
SEARCH_WORD = "SJA"
MAX_MAILS = 100  # one alert, its replies and its confirmation: a day holds a few dozen at most
_ID = re.compile(r"<[^<>\s]+>")

AUTH_MESSAGE = ("Gmail je odbio prijavu za čitanje pošte. Provjerite da je u Gmailu uključen "
                "IMAP (Postavke, Prosljeđivanje i POP/IMAP) i da aplikacijska lozinka vrijedi.")
NETWORK_MESSAGE = "Nije moguće spojiti se na Gmail radi čitanja odgovora."


class MailReadError(Exception):
    """`code`: "auth" (login refused, or IMAP is off in Gmail) or "network"."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MailHead:
    """What the header lines of one mail say (no text yet)."""

    uid: str
    message_id: str
    subject: str
    from_address: str
    replies_to: tuple[str, ...]  # the Message-IDs in In-Reply-To and References
    auth_results: tuple[str, ...]  # every Authentication-Results header, topmost first


class MailSession(Protocol):
    def find(self, since: date) -> list[MailHead]: ...
    def text(self, uid: str) -> str: ...


class ImapReader:
    """A context manager: `with ImapReader(...) as mail:` gives a `MailSession`."""

    def __init__(self, address: str, password: str, *, host: str = HOST, port: int = PORT,
                 timeout: float = TIMEOUT_S, mailbox_factory: Callable[..., MailBox] = MailBox):
        self.address = address
        self.password = password
        self.host, self.port, self.timeout = host, port, timeout
        self._factory = mailbox_factory
        self._box: MailBox | None = None

    def __repr__(self) -> str:  # the password must never reach a log through a repr
        return f"ImapReader(address={self.address!r})"

    def __enter__(self) -> "ImapReader":
        try:
            box = self._factory(self.host, self.port, timeout=self.timeout,
                                ssl_context=ssl.create_default_context())
            self._box = box
            box.login(self.address, self.password, initial_folder=None)
            box.folder.set(FOLDER, readonly=True)
        except imap_tools.MailboxLoginError:
            self._close()
            raise MailReadError("auth", AUTH_MESSAGE) from None
        except Exception:  # sockets, TLS, a refused folder, a server that hangs up
            self._close()
            raise MailReadError("network", NETWORK_MESSAGE) from None
        return self

    def __exit__(self, *exc_info) -> None:
        self._close()

    def _close(self) -> None:
        box, self._box = self._box, None
        if box is None:
            return
        try:
            box.logout()
        except Exception:  # closing must never hide the real error
            pass

    def find(self, since: date) -> list[MailHead]:
        """The header lines of the mails since that day whose title holds the app's marker word,
        newest first. Gmail does the searching; nothing else is listed."""
        try:
            found = self._box.fetch(  # type: ignore[union-attr]
                AND(subject=SEARCH_WORD, date_gte=since), headers_only=True, mark_seen=False,
                bulk=True, reverse=True, limit=MAX_MAILS)
            return [_head(message) for message in found]
        except Exception:
            raise MailReadError("network", NETWORK_MESSAGE) from None

    def text(self, uid: str) -> str:
        try:
            for message in self._box.fetch(  # type: ignore[union-attr]
                    uid_list=[uid], mark_seen=False):
                return message.text or ""
        except Exception:
            raise MailReadError("network", NETWORK_MESSAGE) from None
        return ""


def _first(headers: dict, name: str) -> str:
    values = headers.get(name) or ()
    return str(values[0]).strip() if values else ""


def _head(message) -> MailHead:
    headers = message.headers
    replies = tuple(_ID.findall(_first(headers, "in-reply-to"))) + tuple(
        i for value in headers.get("references", ()) for i in _ID.findall(str(value)))
    return MailHead(
        uid=str(message.uid), message_id=(_ID.findall(_first(headers, "message-id")) or [""])[0],
        subject=str(message.subject or ""), from_address=str(message.from_ or "").lower(),
        replies_to=replies,
        auth_results=tuple(str(v) for v in headers.get("authentication-results", ())))


def build_reader(profile, store: SecretStore, **kwargs) -> ImapReader | None:
    """The reader for Ivan's saved address, or None when no address or password is saved."""
    address = (profile.gmail_address or "").strip()
    if not valid_address(address):
        return None
    try:
        password = store.get(address)
    except SecretStoreError:
        return None
    return ImapReader(address, password, **kwargs) if password else None
