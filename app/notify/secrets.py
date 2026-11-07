"""Where the Gmail app password lives: Windows Credential Manager, nowhere else (plan 3.2, D37).

The password is never written to the database, a file, a log line, an event, a page or an error
message. `SecretStore` is the small interface the rest of the app uses; `KeyringStore` is the real
one (the `keyring` package talks to Windows Credential Manager); tests use `MemoryStore`.
"""

from typing import Protocol

SERVICE = "StudentJobAssistant-Gmail"


class SecretStoreError(Exception):
    """The store could not be read or written (plain words for Ivan, never the secret)."""


class SecretStore(Protocol):
    def get(self, account: str) -> str | None: ...

    def set(self, account: str, secret: str) -> None: ...

    def delete(self, account: str) -> None: ...


class KeyringStore:
    """Windows Credential Manager through the `keyring` package."""

    def __init__(self, service: str = SERVICE):
        self.service = service

    def _keyring(self):
        try:
            import keyring
        except ImportError:  # pragma: no cover - keyring is a pinned dependency
            raise SecretStoreError("Windows Credential Manager nije dostupan.") from None
        return keyring

    def get(self, account: str) -> str | None:
        keyring = self._keyring()
        try:
            return keyring.get_password(self.service, account)
        except Exception:
            raise SecretStoreError("Lozinku nije moguće pročitati iz Windows Credential "
                                   "Managera.") from None

    def set(self, account: str, secret: str) -> None:
        keyring = self._keyring()
        try:
            keyring.set_password(self.service, account, secret)
        except Exception:
            raise SecretStoreError("Lozinku nije moguće spremiti u Windows Credential "
                                   "Manager.") from None

    def delete(self, account: str) -> None:
        keyring = self._keyring()
        try:
            keyring.delete_password(self.service, account)
        except Exception as exc:
            if type(exc).__name__ == "PasswordDeleteError":  # nothing was stored: fine
                return
            raise SecretStoreError("Lozinku nije moguće ukloniti iz Windows Credential "
                                   "Managera.") from None


class MemoryStore:
    """A stand-in for tests: keeps secrets in a dictionary for the life of the object."""

    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self._data.get(account)

    def set(self, account: str, secret: str) -> None:
        self._data[account] = secret

    def delete(self, account: str) -> None:
        self._data.pop(account, None)


_store: SecretStore = KeyringStore()


def get_secret_store() -> SecretStore:
    return _store


def set_secret_store(store: SecretStore) -> None:
    """Swap the store (tests). The real app never calls this."""
    global _store
    _store = store
