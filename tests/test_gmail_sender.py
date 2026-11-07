"""Gmail round 1 (Q2): the sender builds correct mails, refuses header injection, uses TLS and a
timeout, and turns every failure into a plain-Croatian GmailError. A fake server; nothing real."""

import smtplib
from types import SimpleNamespace

import pytest

from app.notify.gmail import (
    AUTH_MESSAGE,
    NETWORK_MESSAGE,
    GmailError,
    GmailSender,
    build_sender,
    clean_app_password,
    valid_address,
)
from app.notify.secrets import MemoryStore, SecretStoreError
from tests.gmail_fakes import ADDRESS, PASSWORD, FakeMail, auth_error


def sender(mail=None, **kw):
    return GmailSender(address=ADDRESS, password=PASSWORD, smtp_factory=mail or FakeMail(), **kw)


# ---------------------------------------------------------------- the message
def test_the_message_has_plain_and_html_parts_and_correct_croatian_headers():
    mail = FakeMail()
    s = sender(mail, display_name="Ivan Šimić")
    message_id = s.send_message(to=ADDRESS, subject="[87] Konobar – Đakovo – 8 min – e-mail",
                                text="Poštovani,\nčšćžđ", html="<p>Poštovani, čšćžđ</p>")
    msg = mail.sent[0]
    assert msg["Subject"] == "[87] Konobar – Đakovo – 8 min – e-mail"
    assert msg["To"] == ADDRESS and ADDRESS in msg["From"] and "Šimić" in msg["From"]
    assert msg["Message-ID"] == message_id and message_id.endswith("@gmail.com>")
    assert msg.get_body(("plain",)).get_content().strip() == "Poštovani,\nčšćžđ"
    assert "<p>" in msg.get_body(("html",)).get_content()
    assert msg["Date"]


def test_an_attachment_is_added_with_its_name_and_type(tmp_path):
    cv = tmp_path / "zivotopis.pdf"
    cv.write_bytes(b"%PDF-1.4 fake")
    mail = FakeMail()
    sender(mail).send(to="posao@firma.example", subject="Prijava", body="Tekst", attachment=cv)
    part = next(mail.sent[0].iter_attachments())
    assert part.get_filename() == "zivotopis.pdf" and part.get_content_type() == "application/pdf"


def test_an_attachment_over_ten_megabytes_is_refused(tmp_path):
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    mail = FakeMail()
    with pytest.raises(GmailError) as caught:
        sender(mail).send(to=ADDRESS, subject="s", body="b", attachment=big)
    assert caught.value.code == "bad_message" and mail.connects == []


@pytest.mark.parametrize("field", ["subject", "to", "name"])
@pytest.mark.parametrize("bad", ["x\r\nBcc: evil@example.com", "x\nBcc: evil@example.com", "x\x00"])
def test_a_line_break_in_any_header_is_refused_before_anything_is_sent(field, bad):
    mail = FakeMail()
    kw = {"display_name": bad} if field == "name" else {}
    to = bad + "@gmail.com" if field == "to" else ADDRESS
    subject = bad if field == "subject" else "Naslov"
    with pytest.raises(GmailError) as caught:
        sender(mail, **kw).send_message(to=to, subject=subject, text="t")
    assert caught.value.code in ("bad_message", "recipient")
    assert mail.connects == [] and mail.sent == []


@pytest.mark.parametrize("to", ["", "nobody", "a@b", "a,b@c.hr", "a b@c.hr", "a@c.hr, b@c.hr"])
def test_a_bad_recipient_is_refused_before_any_connection(to):
    mail = FakeMail()
    with pytest.raises(GmailError) as caught:
        sender(mail).send_message(to=to, subject="s", text="t")
    assert caught.value.code == "recipient" and mail.connects == []


# ---------------------------------------------------------------- the connection
def test_it_uses_gmail_with_tls_a_timeout_then_logs_in_and_logs_out():
    mail = FakeMail()
    sender(mail).send_message(to=ADDRESS, subject="s", text="t")
    assert mail.connects == [("smtp.gmail.com", 587, 20)]
    assert mail.tls == 1 and mail.logins == [(ADDRESS, PASSWORD)] and mail.quits == 1


def test_check_login_logs_in_and_sends_nothing():
    mail = FakeMail()
    sender(mail).check_login()
    assert mail.logins and mail.sent == [] and mail.quits == 1


# ---------------------------------------------------------------- the three kinds of failure
def test_a_refused_password_is_an_auth_error_with_the_plain_message():
    mail = FakeMail(login_error=auth_error())
    with pytest.raises(GmailError) as caught:
        sender(mail).send_message(to=ADDRESS, subject="s", text="t")
    assert caught.value.code == "auth" and str(caught.value) == AUTH_MESSAGE
    assert mail.sent == [] and mail.quits == 1


@pytest.mark.parametrize("failure", [OSError("unreachable"), TimeoutError("slow"),
                                     smtplib.SMTPConnectError(421, b"busy"),
                                     smtplib.SMTPServerDisconnected("gone")])
def test_a_network_failure_is_a_network_error(failure):
    with pytest.raises(GmailError) as caught:
        sender(FakeMail(connect_error=failure)).send_message(to=ADDRESS, subject="s", text="t")
    assert caught.value.code == "network" and str(caught.value) == NETWORK_MESSAGE


def test_a_failure_while_sending_is_reported_and_the_connection_is_closed():
    cases = [
        (smtplib.SMTPRecipientsRefused({ADDRESS: (550, b"no")}), "recipient"),
        (smtplib.SMTPSenderRefused(550, b"daily limit", ADDRESS), "refused"),
        (smtplib.SMTPDataError(552, b"too big"), "refused"),
        (smtplib.SMTPServerDisconnected("dropped"), "network"),
    ]
    for failure, code in cases:
        mail = FakeMail(send_error=failure)
        with pytest.raises(GmailError) as caught:
            sender(mail).send_message(to=ADDRESS, subject="s", text="t")
        assert caught.value.code == code and mail.quits == 1


def test_no_error_message_and_no_repr_ever_contains_the_password():
    s = sender(FakeMail(login_error=auth_error()))
    assert PASSWORD not in repr(s) and PASSWORD not in str(s)
    with pytest.raises(GmailError) as caught:
        s.check_login()
    assert PASSWORD not in str(caught.value) and caught.value.__cause__ is None


# ---------------------------------------------------------------- small helpers
@pytest.mark.parametrize("raw, expected", [
    ("abcd efgh ijkl mnop", "abcdefghijklmnop"),
    ("  ABCD EFGH IJKL MNOP\n", "abcdefghijklmnop"),
    ("abcdefghijklmnop", "abcdefghijklmnop"), ("abcd efgh ijkl", None),
    ("abc1 efgh ijkl mnop", None),
    (None, None), ("", None)])
def test_an_app_password_is_cleaned_or_refused(raw, expected):
    assert clean_app_password(raw) == expected


def test_address_checks():
    assert valid_address("ime.prezime@gmail.com") and valid_address("a+b@firma.hr")
    assert not valid_address(None) and not valid_address("x" * 250 + "@a.hr")
    assert not valid_address("a b@gmail.com") and not valid_address("<a@b.hr>")


def test_build_sender_needs_an_address_and_a_saved_password():
    store = MemoryStore()
    profile = SimpleNamespace(gmail_address=ADDRESS, full_name="Ivan")
    assert build_sender(profile, store) is None  # no password yet
    store.set(ADDRESS, PASSWORD)
    built = build_sender(profile, store)
    assert built.address == ADDRESS and built.display_name == "Ivan"
    assert build_sender(SimpleNamespace(gmail_address=None, full_name=None), store) is None
    assert build_sender(SimpleNamespace(gmail_address="not an address", full_name=None),
                        store) is None

    class Broken(MemoryStore):
        def get(self, account):
            raise SecretStoreError("x")

    assert build_sender(profile, Broken()) is None
