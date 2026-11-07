"""A pretend Gmail inbox for tests: nothing real is ever contacted (D42).

Two levels:
- `FakeMailbox` stands in for `imap_tools.MailBox` (used by the reply-flow tests, which work on
  whole .eml fixtures);
- `FakeImapClient` stands in for Python's `imaplib` client under the REAL `imap_tools.MailBox`
  (used to prove which IMAP commands the app sends: nothing that could change a mailbox).
"""

from collections.abc import Iterator
from pathlib import Path

from imap_tools import MailBox, MailboxLoginError, MailMessage

FIXTURES = Path(__file__).parent / "fixtures" / "replies"


def eml(name: str, **subs: str) -> bytes:
    """A fixture mail with @@KEY@@ filled in (CODE, ALERT_ID, MSGID, BODY), CRLF line ends."""
    text = (FIXTURES / f"{name}.eml").read_text(encoding="utf-8")
    subs = {"BODY": "DA", **subs}
    for key, value in subs.items():
        text = text.replace(f"@@{key}@@", value)
    assert "@@" not in text, f"{name}.eml has a placeholder that was not filled in"
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def make_message(uid: str, raw: bytes, *, headers_only: bool) -> MailMessage:
    if headers_only:
        raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    item = f"{uid} (UID {uid} FLAGS () BODY[] {{{len(raw)}}}".encode()
    return MailMessage([(item, raw), b")"])


class FakeMailbox:
    """Pass it as `mailbox_factory`: it is both the factory and the mailbox."""

    def __init__(self, mails: dict[str, bytes] | None = None, *, login_error: bool = False,
                 fetch_error: Exception | None = None):
        self.mails: dict[str, bytes] = dict(mails or {})
        self.login_error = login_error
        self.fetch_error = fetch_error
        self.connects: list[tuple] = []
        self.logins: list[tuple] = []
        self.folders: list[tuple] = []
        self.fetches: list[dict] = []
        self.logouts = 0
        self.folder = self  # `box.folder.set(...)`

    def __call__(self, host, port, *, timeout=None, ssl_context=None):
        self.connects.append((host, port, timeout, ssl_context is not None))
        return self

    def login(self, user, password, initial_folder="INBOX"):
        if self.login_error:
            raise MailboxLoginError(command_result=("NO", [b"bad"]), expected="OK")
        self.logins.append((user, initial_folder))
        return self

    def set(self, folder, readonly=False):
        self.folders.append((folder, readonly))

    def fetch(self, criteria="ALL", *, headers_only=False, mark_seen=True, bulk=False,
              reverse=False, limit=None, uid_list=None) -> Iterator[MailMessage]:
        self.fetches.append({"criteria": str(criteria), "headers_only": headers_only,
                             "mark_seen": mark_seen, "uid_list": uid_list})
        if self.fetch_error is not None:
            raise self.fetch_error
        uids = list(uid_list) if uid_list else [
            uid for uid, raw in self.mails.items()  # Gmail searches the DECODED title
            if "SJA" in make_message(uid, raw, headers_only=True).subject.upper()]
        for uid in (reversed(uids) if reverse else uids):
            yield make_message(uid, self.mails[uid], headers_only=headers_only)

    def logout(self):
        self.logouts += 1


class FakeImapClient:
    """What `imaplib.IMAP4_SSL` would be: records every command, answers from `mails`."""

    capabilities = ("IMAP4REV1",)

    def __init__(self, mails: dict[str, bytes]):
        self.mails = mails
        self.commands: list[tuple] = []
        self.tagged_commands: dict = {}
        self.state = "NONAUTH"

    def _quote(self, value):
        return '"' + value + '"'

    def _simple_command(self, name, *args):
        self.commands.append((name.upper(),))  # the LOGIN line holds the password: never recorded
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        name = mailbox.decode() if isinstance(mailbox, bytes) else mailbox
        self.commands.append(("EXAMINE" if readonly else "SELECT", name.strip('"')))
        return "OK", [str(len(self.mails)).encode()]

    def uid(self, command, *args):
        name = command.upper()
        self.commands.append(("UID " + name, *[str(a) for a in args]))
        if name == "SEARCH":
            return "OK", [" ".join(self.mails).encode()]
        if name == "FETCH":
            uids, parts = str(args[0]).split(","), str(args[1])
            out: list = []
            for uid in sorted(uids, key=int):  # a real server answers in ascending order
                if uid not in self.mails:  # a mail that is gone: the server just says nothing
                    return "OK", [None]
                raw = self.mails[uid]
                if "[HEADER]" in parts:
                    raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                out += [(f"{uid} (UID {uid} FLAGS () BODY[] {{{len(raw)}}}".encode(), raw), b")"]
            return "OK", out
        raise AssertionError(f"the app sent an IMAP command it must never send: UID {name}")

    def logout(self):
        self.commands.append(("LOGOUT",))
        return "BYE", [b"bye"]


class FakeImapMailBox(MailBox):
    """The real imap-tools mailbox on top of the pretend client."""

    def __init__(self, client: FakeImapClient):
        self._fake_client = client
        super().__init__("imap.example")

    def _get_mailbox_client(self):
        return self._fake_client
