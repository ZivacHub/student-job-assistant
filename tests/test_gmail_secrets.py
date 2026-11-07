"""Gmail round 1 (Q1): the app password lives only in the secret store, and only after a login
check passed. Nothing here touches the real Windows Credential Manager or the internet."""

import keyring
import pytest
from sqlalchemy import select

from app.core.profile import get_or_create_profile
from app.models import Event, Profile
from app.notify import secrets, setup
from app.notify.secrets import KeyringStore, MemoryStore, SecretStoreError
from tests.gmail_fakes import ADDRESS, PASSWORD, PASSWORD_SHOWN, FakeMail, auth_error


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    db.commit()
    return p


def test_tests_never_get_the_real_password_store():
    assert isinstance(secrets.get_secret_store(), MemoryStore)


# ---------------------------------------------------------------- the real store (faked keyring)
class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, user):
        return self.data.get((service, user))

    def set_password(self, service, user, secret):
        self.data[(service, user)] = secret

    def delete_password(self, service, user):
        if (service, user) not in self.data:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.data[(service, user)]


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, getattr(fake, name))
    return fake


def test_the_keyring_store_saves_reads_and_deletes_under_its_own_service_name(fake_keyring):
    store = KeyringStore()
    assert store.get(ADDRESS) is None
    store.set(ADDRESS, PASSWORD)
    assert store.get(ADDRESS) == PASSWORD
    assert list(fake_keyring.data) == [("StudentJobAssistant-Gmail", ADDRESS)]
    store.delete(ADDRESS)
    assert store.get(ADDRESS) is None
    store.delete(ADDRESS)  # deleting what is not there is fine


def test_a_broken_store_gives_a_plain_error_that_never_contains_the_secret(monkeypatch):
    def boom(*args):
        raise RuntimeError(f"vault exploded with {PASSWORD}")

    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, boom)
    store = KeyringStore()
    for call in (lambda: store.get(ADDRESS), lambda: store.set(ADDRESS, PASSWORD),
                 lambda: store.delete(ADDRESS)):
        with pytest.raises(SecretStoreError) as caught:
            call()
        assert PASSWORD not in str(caught.value) and caught.value.__cause__ is None


# ---------------------------------------------------------------- saving only after a login
def test_the_password_is_saved_only_after_a_real_login_worked(db, profile):
    store, mail = MemoryStore(), FakeMail()
    errors = setup.save_credentials(db, profile, store, address=ADDRESS,
                                    raw_password=PASSWORD_SHOWN, smtp_factory=mail)
    assert errors == {}
    assert mail.logins == [(ADDRESS, PASSWORD)] and mail.tls == 1 and mail.quits == 1
    assert store.get(ADDRESS) == PASSWORD  # spaces taken out
    assert profile.gmail_address == ADDRESS and profile.gmail_checked_at is not None
    assert [e.kind for e in db.scalars(select(Event))] == ["gmail_saved"]


def test_a_refused_password_stores_nothing_and_says_why_in_plain_words(db, profile):
    store, mail = MemoryStore(), FakeMail(login_error=auth_error())
    errors = setup.save_credentials(db, profile, store, address=ADDRESS,
                                    raw_password=PASSWORD, smtp_factory=mail)
    assert list(errors) == ["password"] and "16 slova" in errors["password"]
    assert store.get(ADDRESS) is None and profile.gmail_address is None
    assert db.scalar(select(Event.id)) is None


def test_a_network_problem_stores_nothing_and_blames_the_connection_not_the_password(db, profile):
    store, mail = MemoryStore(), FakeMail(connect_error=OSError("no route"))
    errors = setup.save_credentials(db, profile, store, address=ADDRESS,
                                    raw_password=PASSWORD, smtp_factory=mail)
    assert list(errors) == ["general"] and "internetsku vezu" in errors["general"]
    assert store.get(ADDRESS) is None


@pytest.mark.parametrize("raw", ["", "abc", "abcdefghijklmno", "abcdefghijklmnopq",
                                 "abcdefghijklmno1", "abcd-efgh-ijkl-mnop"])
def test_a_password_that_does_not_look_like_an_app_password_is_not_even_tried(db, profile, raw):
    store, mail = MemoryStore(), FakeMail()
    errors = setup.save_credentials(db, profile, store, address=ADDRESS, raw_password=raw,
                                    smtp_factory=mail)
    assert "password" in errors and mail.connects == [] and store.get(ADDRESS) is None


@pytest.mark.parametrize("address", ["", "ivan", "ivan@", "a b@gmail.com", "a@b", "a,b@gmail.com"])
def test_a_wrong_address_is_refused_before_any_connection(db, profile, address):
    mail = FakeMail()
    errors = setup.save_credentials(db, profile, MemoryStore(), address=address,
                                    raw_password=PASSWORD, smtp_factory=mail)
    assert "address" in errors and mail.connects == []


def test_a_store_that_cannot_save_is_reported_and_the_profile_stays_unchanged(db, profile):
    class Broken(MemoryStore):
        def set(self, account, secret):
            raise SecretStoreError("Lozinku nije moguće spremiti u Windows Credential Manager.")

    errors = setup.save_credentials(db, profile, Broken(), address=ADDRESS, raw_password=PASSWORD,
                                    smtp_factory=FakeMail())
    assert list(errors) == ["general"] and profile.gmail_address is None


# ---------------------------------------------------------------- the password goes nowhere else
def test_the_password_appears_nowhere_in_the_database(db, profile):
    setup.save_credentials(db, profile, MemoryStore(), address=ADDRESS,
                           raw_password=PASSWORD_SHOWN, smtp_factory=FakeMail())
    setup.send_test(db, profile, _stored(), smtp_factory=FakeMail())
    db.commit()
    everything = repr([(c.name, getattr(profile, c.name)) for c in Profile.__table__.columns])
    everything += repr([(e.kind, e.data) for e in db.scalars(select(Event))])
    assert PASSWORD not in everything and PASSWORD_SHOWN not in everything


def _stored():
    store = MemoryStore()
    store.set(ADDRESS, PASSWORD)
    return store


def test_removing_the_password_deletes_it_and_switches_alerts_off(db, profile):
    store = MemoryStore()
    setup.save_credentials(db, profile, store, address=ADDRESS, raw_password=PASSWORD,
                           smtp_factory=FakeMail())
    profile.alerts_enabled = True
    assert setup.remove_credentials(db, profile, store) is None
    assert store.get(ADDRESS) is None and profile.alerts_enabled is False
    assert profile.gmail_checked_at is None
    assert "gmail_removed" in [e.kind for e in db.scalars(select(Event))]


def test_a_new_address_replaces_the_old_password_and_resets_the_alert_switch(db, profile):
    store = MemoryStore()
    setup.save_credentials(db, profile, store, address=ADDRESS, raw_password=PASSWORD,
                           smtp_factory=FakeMail())
    profile.alerts_enabled = True
    profile.phone_push_result = "yes"
    setup.save_credentials(db, profile, store, address="other@gmail.com", raw_password=PASSWORD,
                           smtp_factory=FakeMail())
    assert store.get(ADDRESS) is None and store.get("other@gmail.com") == PASSWORD
    assert profile.alerts_enabled is False and profile.phone_push_result is None


def test_the_alert_switch_needs_a_test_message_to_the_same_address(db, profile):
    store = MemoryStore()
    setup.save_credentials(db, profile, store, address=ADDRESS, raw_password=PASSWORD,
                           smtp_factory=FakeMail())
    assert "probnu poruku" in setup.set_alerts(db, profile, store, True)  # no test yet
    assert setup.send_test(db, profile, store, smtp_factory=FakeMail()) is None
    assert setup.set_alerts(db, profile, store, True) is None
    assert profile.alerts_enabled is True and profile.alerts_enabled_at is not None
    # a different address starts again: its test message has not been sent
    setup.save_credentials(db, profile, store, address="other@gmail.com", raw_password=PASSWORD,
                           smtp_factory=FakeMail())
    assert setup.set_alerts(db, profile, store, True) is not None


def test_the_test_message_goes_to_ivan_himself_and_a_refused_password_switches_alerts_off(
        db, profile):
    store = MemoryStore()
    setup.save_credentials(db, profile, store, address=ADDRESS, raw_password=PASSWORD,
                           smtp_factory=FakeMail())
    mail = FakeMail()
    assert setup.send_test(db, profile, store, smtp_factory=mail) is None
    assert [m["To"] for m in mail.sent] == [ADDRESS]
    profile.alerts_enabled = True
    refused = FakeMail(login_error=auth_error())
    error = setup.send_test(db, profile, store, smtp_factory=refused)
    assert "16 slova" in error and profile.alerts_enabled is False and profile.gmail_problem


def test_a_test_message_without_a_saved_password_says_what_to_do_first(db, profile):
    assert "Najprije" in setup.send_test(db, profile, MemoryStore(), smtp_factory=FakeMail())
