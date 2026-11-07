import os
import tempfile

# Before anything imports the app: tests use a throwaway data folder and never start the
# background scheduler (it would poll real websites).
os.environ.setdefault("JOBS_DATA_DIR", tempfile.mkdtemp(prefix="jobs-test-"))
os.environ["JOBS_SCHEDULER_ENABLED"] = "false"
os.environ["JOBS_ALLOWED_HOSTS"] = '["testserver"]'  # the test client\'s host name

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import make_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path) -> Session:
    """A fresh SQLite DB built by the real Alembic migrations (so migrations are tested too)."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    with sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_real_password_store():
    """No test may ever touch the real Windows Credential Manager: every test gets an empty
    in-memory store instead (D37)."""
    from app.notify import secrets

    previous = secrets.get_secret_store()
    secrets.set_secret_store(secrets.MemoryStore())
    yield
    secrets.set_secret_store(previous)


class RealMailAttempt(BaseException):
    """Not an Exception on purpose: the reader's own `except Exception` must not hide it."""


class RealNetworkAttempt(BaseException):
    """A test tried to open a REAL connection to another computer (not an Exception, so nothing
    in the code under test can swallow it)."""


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """No test may reach another computer (plan 4.11, "no test run reaches the network"): a
    socket may only connect to this PC. Pretend sites (`respx`, fake clients) never get that far."""
    import socket

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def local(address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        return isinstance(host, str) and (host == "localhost" or host == "::1"
                                          or host.startswith("127."))

    def connect(self, address):
        if not local(address):
            raise RealNetworkAttempt(f"a test tried to reach {address!r}; use respx or a fake")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not local(address):
            raise RealNetworkAttempt(f"a test tried to reach {address!r}; use respx or a fake")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def _no_real_mail_connection(monkeypatch):
    """No test may ever open a real mail connection: anything that tries to use the real
    `smtplib.SMTP` fails loudly. Tests that need mail pass a pretend server (gmail_fakes.py)."""
    import smtplib

    class RefusedSMTP(smtplib.SMTP):  # a subclass, so tests may still patch its methods
        def __init__(self, *args, **kwargs):
            raise AssertionError("a test tried to open a REAL mail connection; pass a FakeMail")

    monkeypatch.setattr(smtplib, "SMTP", RefusedSMTP)

    import imaplib

    class RefusedIMAP(imaplib.IMAP4_SSL):  # the reply reader (D42) must be given a pretend inbox
        def __init__(self, *args, **kwargs):
            raise RealMailAttempt("a test tried to open a REAL mail connection; use FakeMailbox")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", RefusedIMAP)
    monkeypatch.setattr(imaplib, "IMAP4", RefusedIMAP)
