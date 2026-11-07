"""Plan 5.3b (D42): the IMAP reader may only look. These tests run the REAL imap-tools code over a
pretend IMAP client that records every command, and fail on any command that could change a
mailbox."""

from datetime import date

import pytest

from app.notify.imapbox import FOLDER, ImapReader, MailReadError, build_reader
from app.notify.secrets import MemoryStore
from tests.conftest import RealMailAttempt
from tests.gmail_fakes import ADDRESS, PASSWORD
from tests.imap_fakes import FakeImapClient, FakeImapMailBox, eml

ALERT_ID = "<alert1@gmail.com>"
SINCE = date(2026, 11, 2)


def mails():
    return {
        "101": eml("valid_da", CODE="K7M2-Q9XA", ALERT_ID=ALERT_ID, MSGID="<r101@mail.gmail.com>"),
        "102": eml("spoofed_dkim_fail", CODE="K7M2-Q9XA", ALERT_ID=ALERT_ID,
                   MSGID="<r102@mail.gmail.com>"),
    }


def reader(client):
    return ImapReader(ADDRESS, PASSWORD,
                      mailbox_factory=lambda host, port, **kw: FakeImapMailBox(client))


def test_only_these_commands_are_ever_sent_and_the_folder_is_opened_read_only():
    client = FakeImapClient(mails())
    with reader(client) as box:
        heads = box.find(SINCE)
        assert box.text("101").startswith("DA")
    names = [c[0] for c in client.commands]
    assert names[0] == "LOGIN" and names[-1] == "LOGOUT"
    assert ("EXAMINE", FOLDER) in client.commands and "SELECT" not in names
    assert set(names) <= {"LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
    assert [h.uid for h in heads] == ["102", "101"]  # newest first


def test_reading_never_marks_a_mail_as_read():
    client = FakeImapClient(mails())
    with reader(client) as box:
        box.find(SINCE)
        box.text("101")
    fetches = [c for c in client.commands if c[0] == "UID FETCH"]
    assert fetches and all("BODY.PEEK" in c[2] for c in fetches)
    assert "[HEADER]" in fetches[0][2]  # the first look is at the header lines only
    assert "[HEADER]" not in fetches[-1][2]  # the text only for the one mail asked for


def test_gmail_is_asked_to_search_by_the_marker_word_and_the_day():
    client = FakeImapClient(mails())
    with reader(client) as box:
        box.find(SINCE)
    search = next(c for c in client.commands if c[0] == "UID SEARCH")
    criteria = " ".join(search[1:])
    assert 'SUBJECT "SJA"' in criteria and "SINCE 2-Nov-2026" in criteria


def test_the_header_lines_become_a_head_with_everything_the_checks_need():
    client = FakeImapClient(mails())
    with reader(client) as box:
        heads = {h.uid: h for h in box.find(SINCE)}
    good = heads["101"]
    assert good.message_id == "<r101@mail.gmail.com>" and good.from_address == ADDRESS
    assert good.subject.endswith("[SJA-K7M2-Q9XA]") and good.replies_to == (ALERT_ID, ALERT_ID)
    assert len(good.auth_results) == 1 and good.auth_results[0].startswith("mx.google.com")
    assert "dkim=fail" in heads["102"].auth_results[0]


def test_a_refused_login_is_an_auth_error_and_the_password_is_not_in_the_message():
    class Refusing(FakeImapClient):
        def _simple_command(self, name, *args):
            self.commands.append((name.upper(),))
            return "NO", [b"[AUTHENTICATIONFAILED] Invalid credentials"]

    with pytest.raises(MailReadError) as caught, reader(Refusing({})):
        pass
    assert caught.value.code == "auth" and PASSWORD not in str(caught.value)
    assert "IMAP" in str(caught.value)


def test_any_other_trouble_is_a_network_error():
    def broken(host, port, **kw):
        raise OSError("unreachable")

    with pytest.raises(MailReadError) as caught, ImapReader(ADDRESS, PASSWORD,
                                                            mailbox_factory=broken):
        pass
    assert caught.value.code == "network"


def test_a_failure_while_searching_is_a_network_error_and_the_connection_is_closed():
    class Failing(FakeImapClient):
        def uid(self, command, *args):
            raise OSError("reset")

    client = Failing(mails())
    with pytest.raises(MailReadError) as caught, reader(client) as box:
        box.find(SINCE)
    assert caught.value.code == "network" and client.commands[-1] == ("LOGOUT",)


def test_the_password_never_shows_in_a_repr_or_in_the_recorded_commands():
    client = FakeImapClient(mails())
    r = reader(client)
    with r as box:
        box.find(SINCE)
    assert PASSWORD not in repr(r) and PASSWORD not in repr(client.commands)


def test_a_reader_is_built_only_from_a_saved_address_and_password():
    class P:
        gmail_address = ADDRESS

    store = MemoryStore()
    assert build_reader(P(), store) is None  # no password saved
    store.set(ADDRESS, PASSWORD)
    assert build_reader(P(), store).address == ADDRESS
    P.gmail_address = "not an address"
    assert build_reader(P(), store) is None
    P.gmail_address = None
    assert build_reader(P(), store) is None


def test_a_logout_that_fails_never_hides_the_result():
    class BadLogout(FakeImapClient):
        def logout(self):
            raise OSError("already closed")

    with reader(BadLogout(mails())) as box:
        assert len(box.find(SINCE)) == 2


def test_a_failure_while_reading_the_text_is_a_network_error_and_a_missing_mail_is_empty():
    class Failing(FakeImapClient):
        def uid(self, command, *args):
            if command.upper() == "FETCH" and "[HEADER]" not in str(args[1]):
                raise OSError("reset")
            return super().uid(command, *args)

    with pytest.raises(MailReadError) as caught, reader(Failing(mails())) as box:
        box.text("101")
    assert caught.value.code == "network"
    with reader(FakeImapClient(mails())) as box:
        assert box.text("999") == ""  # a mail that is gone: no text, no crash


def test_a_secret_store_that_cannot_be_read_means_no_reader():
    from app.notify.secrets import SecretStoreError

    class Broken:
        def get(self, account):
            raise SecretStoreError("locked")

    class P:
        gmail_address = ADDRESS

    assert build_reader(P(), Broken()) is None


def test_a_test_that_forgets_the_pretend_inbox_fails_loudly_instead_of_calling_gmail():
    with pytest.raises(RealMailAttempt), ImapReader(ADDRESS, PASSWORD):
        pass  # the default mailbox_factory would go to Gmail
