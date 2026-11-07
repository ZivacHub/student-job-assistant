"""The Gmail sender (plan 3.2, D37): sends a message through smtp.gmail.com with an app password.

Used for the alerts to Ivan himself now; later (round 2) behind the approval gate for applications
(it satisfies `app.apply.gate.Sender`). It never logs or returns the password, refuses line breaks
in any header (an ad cannot inject headers), uses STARTTLS and a timeout, and turns every failure
into a `GmailError` with a plain-Croatian message and a short code.
"""

import mimetypes
import re
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from app.notify.secrets import SecretStore, SecretStoreError

HOST = "smtp.gmail.com"
PORT = 587
TIMEOUT_S = 20
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

_ADDRESS = re.compile(r"^[^@\s,;<>()\[\]\\\"']+@[^@\s,;<>()\[\]\\\"']+\.[^@\s,;<>()\[\]\\\"']+$")
_APP_PASSWORD = re.compile(r"^[a-z]{16}$")

AUTH_MESSAGE = ("Gmail je odbio lozinku. Provjerite da ste prepisali svih 16 slova aplikacijske "
                "lozinke (ne obične lozinke za Gmail) i da je u Google računu uključena potvrda "
                "u dva koraka.")
NETWORK_MESSAGE = ("Nije moguće spojiti se na Gmail. Provjerite internetsku vezu i pokušajte "
                   "ponovno.")


class GmailError(Exception):
    """`code`: "auth" (password refused), "network", "recipient", "refused" (message refused),
    "bad_message" (rejected before sending), "not_configured". `str(error)` is plain Croatian."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def valid_address(address: str | None) -> bool:
    return bool(address) and len(address) <= 254 and bool(_ADDRESS.match(address))


def clean_app_password(raw: str | None) -> str | None:
    """Google shows the password as four groups of four letters with spaces: take them out.
    Returns the 16 letters, or None when it does not look like an app password."""
    text = re.sub(r"\s+", "", raw or "").lower()
    return text if _APP_PASSWORD.match(text) else None


def _no_breaks(value: str, what: str) -> str:
    if any(ch in value for ch in "\r\n\x00"):
        raise GmailError("bad_message", f"{what} ne smije sadržavati prijelom retka.")
    return value


@dataclass
class GmailSender:
    address: str
    password: str
    display_name: str | None = None
    host: str = HOST
    port: int = PORT
    timeout: float = TIMEOUT_S
    # tests pass a fake; the default is looked up when the sender is made (so a test guard works)
    smtp_factory: Callable[..., smtplib.SMTP] = field(default_factory=lambda: smtplib.SMTP)

    def __repr__(self) -> str:  # the password must never reach a log through a repr
        return f"GmailSender(address={self.address!r})"

    # ---- connection
    def _connect(self) -> smtplib.SMTP:
        smtp = None
        try:
            smtp = self.smtp_factory(self.host, self.port, timeout=self.timeout)
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.address, self.password)
            return smtp
        except smtplib.SMTPAuthenticationError:
            self._quit(smtp)
            raise GmailError("auth", AUTH_MESSAGE) from None
        except (smtplib.SMTPException, OSError):  # includes timeouts and TLS problems
            self._quit(smtp)
            raise GmailError("network", NETWORK_MESSAGE) from None

    @staticmethod
    def _quit(smtp: smtplib.SMTP | None) -> None:
        if smtp is None:
            return
        try:
            smtp.quit()
        except Exception:  # closing must never hide the real error
            try:
                smtp.close()
            except Exception:
                pass

    def check_login(self) -> None:
        """Log in and log out, nothing sent. Raises GmailError."""
        self._quit(self._connect())

    # ---- sending
    def new_message_id(self) -> str:
        """A Message-ID made before the mail is sent, so a code can be tied to it (plan 5.3b)."""
        return make_msgid(domain=self.address.partition("@")[2])

    def build_message(self, *, to: str, subject: str, text: str, html: str | None = None,
                      attachment: Path | None = None,
                      message_id: str | None = None) -> EmailMessage:
        if not valid_address(to):
            raise GmailError("recipient", "Adresa primatelja nije ispravna.")
        _no_breaks(to, "Adresa")
        _no_breaks(subject, "Naslov")
        sender_name = _no_breaks(self.display_name, "Ime") if self.display_name else ""
        user, _, domain = self.address.partition("@")
        msg = EmailMessage()
        try:
            msg["From"] = str(Address(sender_name, user, domain))
        except ValueError:  # a name the header rules refuse: send from the bare address
            msg["From"] = self.address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = _no_breaks(message_id, "Message-ID") if message_id else make_msgid(
            domain=domain)
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        if attachment is not None:
            data = attachment.read_bytes()
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise GmailError("bad_message", "Privitak je prevelik.")
            kind, _ = mimetypes.guess_type(attachment.name)
            maintype, _, subtype = (kind or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype,
                               filename=_no_breaks(attachment.name, "Naziv privitka"))
        return msg

    def send_message(self, *, to: str, subject: str, text: str, html: str | None = None,
                     attachment: Path | None = None, message_id: str | None = None) -> str:
        """Send one message and return its Message-ID. Raises GmailError."""
        msg = self.build_message(to=to, subject=subject, text=text, html=html,
                                 attachment=attachment, message_id=message_id)
        smtp = self._connect()
        try:
            smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused:
            raise GmailError("recipient", "Gmail je odbio adresu primatelja.") from None
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise GmailError("refused", "Gmail je odbio poruku (možda je dosegnuta dnevna "
                                        "granica slanja).") from None
        except (smtplib.SMTPException, OSError):
            raise GmailError("network", NETWORK_MESSAGE) from None
        finally:
            self._quit(smtp)
        return str(msg["Message-ID"])

    def send(self, *, to: str, subject: str, body: str, attachment: Path | None) -> str:
        """The interface of the approval gate (`app.apply.gate.Sender`): a plain-text message."""
        return self.send_message(to=to, subject=subject, text=body, attachment=attachment)


def build_sender(profile, store: SecretStore, **kwargs) -> GmailSender | None:
    """The sender for Ivan's saved address, or None when no address or no password is saved."""
    address = (profile.gmail_address or "").strip()
    if not valid_address(address):
        return None
    try:
        password = store.get(address)
    except SecretStoreError:
        return None
    if not password:
        return None
    return GmailSender(address=address, password=password, display_name=profile.full_name,
                       **kwargs)
