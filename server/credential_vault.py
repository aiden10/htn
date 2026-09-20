"""Encryption boundary for HTN OS app keys.

The badge's device token is deliberately not represented here: it is owned by
HTN OS and the badge proxy.  Shutterdex only stores the owner-shared app key,
and only as an encrypted value that it can decrypt immediately before opening
an app WebSocket.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from typing import Protocol


class CredentialError(RuntimeError):
    """The server cannot safely read or write a badge app key."""


class CredentialVault(Protocol):
    def seal(self, app_key: str) -> str:
        """Return an encrypted database-safe representation of ``app_key``."""

    def open(self, ciphertext: str) -> str:
        """Return an app key immediately before using it with the HTN service."""


def validate_app_key(app_key: str) -> str:
    """Apply the firmware's intentionally small, printable app-key contract."""

    # Do not trim: a key is an exact shared secret, and silently changing it
    # would make a correctly configured badge look like a bad-key failure.
    key = app_key
    if not 4 <= len(key) <= 32 or not key.isprintable():
        raise CredentialError("Badge app keys must contain 4–32 printable characters.")
    return key


def app_key_fingerprint(app_key: str) -> str:
    """A safe diagnostic identifier; never use it as an authenticator."""

    return sha256(app_key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class FernetCredentialVault:
    """Fernet-backed vault configured with ``SHUTTERDEX_CREDENTIAL_KEY``.

    A Fernet key is generated once and kept in the deployment's secret store;
    it is not the same thing as an HTN badge app key.
    """

    _fernet: object

    @classmethod
    def from_environment(cls, variable: str = "SHUTTERDEX_CREDENTIAL_KEY") -> "FernetCredentialVault":
        encoded_key = os.getenv(variable, "").strip()
        if not encoded_key:
            raise CredentialError(
                f"{variable} is required before pairing a Wi-Fi badge. "
                "Use a Fernet key stored in your deployment secret manager."
            )
        try:
            from cryptography.fernet import Fernet

            return cls(Fernet(encoded_key.encode("ascii")))
        except ImportError as exc:
            raise CredentialError(
                "The cryptography package is required for encrypted badge credentials."
            ) from exc
        except (TypeError, ValueError) as exc:
            raise CredentialError(f"{variable} is not a valid Fernet key.") from exc

    def seal(self, app_key: str) -> str:
        key = validate_app_key(app_key)
        return self._fernet.encrypt(key.encode("utf-8")).decode("ascii")  # type: ignore[attr-defined]

    def open(self, ciphertext: str) -> str:
        try:
            return validate_app_key(self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8"))  # type: ignore[attr-defined]
        except Exception as exc:  # InvalidToken deliberately does not reveal key details.
            raise CredentialError("Stored badge app key cannot be decrypted.") from exc


class UnavailableCredentialVault:
    """Safe default used until a deployment configures encryption."""

    def seal(self, app_key: str) -> str:
        del app_key
        raise CredentialError(
            "Pairing is disabled until SHUTTERDEX_CREDENTIAL_KEY is configured."
        )

    def open(self, ciphertext: str) -> str:
        del ciphertext
        raise CredentialError(
            "Badge credentials cannot be read until SHUTTERDEX_CREDENTIAL_KEY is configured."
        )
