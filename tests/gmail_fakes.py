"""A pretend Gmail server for tests: nothing real is ever contacted (D37)."""

import smtplib


class FakeMail:
    """Pass it where the app expects `smtplib.SMTP`: it records what the app does."""

    def __init__(self, *, connect_error=None, login_error=None, send_error=None):
        self.connect_error = connect_error
        self.login_error = login_error
        self.send_error = send_error
        self.connects: list[tuple] = []
        self.logins: list[tuple[str, str]] = []
        self.sent: list = []
        self.tls = 0
        self.quits = 0

    def __call__(self, host, port, timeout=None):
        self.connects.append((host, port, timeout))
        if self.connect_error is not None:
            raise self.connect_error
        return _Connection(self)


class _Connection:
    def __init__(self, mail: FakeMail):
        self.mail = mail

    def starttls(self, context=None):
        assert context is not None, "STARTTLS must come with a TLS context"
        self.mail.tls += 1

    def login(self, user, password):
        if self.mail.login_error is not None:
            raise self.mail.login_error
        self.mail.logins.append((user, password))

    def send_message(self, msg):
        if self.mail.send_error is not None:
            raise self.mail.send_error
        self.mail.sent.append(msg)

    def quit(self):
        self.mail.quits += 1

    def close(self):  # pragma: no cover - only used when quit fails
        pass


def auth_error() -> smtplib.SMTPAuthenticationError:
    return smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")


PASSWORD = "abcdefghijklmnop"  # a made-up 16-letter app password
PASSWORD_SHOWN = "abcd efgh ijkl mnop"  # how Google shows it
ADDRESS = "ivan.test@gmail.com"
