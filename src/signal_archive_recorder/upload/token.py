# SPDX-License-Identifier: Apache-2.0
"""The contributor's Hugging Face token: kept in the OS keyring, never on disk.

`Token` hides its value from repr, str, logs and tracebacks; the only way to get
the value is `reveal()`, used only when calling Hugging Face.
"""

from __future__ import annotations

import contextlib
from typing import Any, Protocol

SERVICE = "signal-archive-recorder"
USERNAME = "huggingface-token"


class Token:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        value = value.strip()
        if not value.startswith("hf_") or len(value) < 10:
            raise ValueError("That doesn't look like a Hugging Face access token (hf_...)")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Token(hf_****)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Token) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Token", self._value))

    def __reduce__(self) -> Any:
        raise TypeError("tokens can't be pickled")


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class _SystemKeyring:
    def __init__(self) -> None:
        import keyring

        self._keyring: Any = keyring

    def get_password(self, service: str, username: str) -> str | None:
        value: str | None = self._keyring.get_password(service, username)
        return value

    def set_password(self, service: str, username: str, password: str) -> None:
        self._keyring.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        self._keyring.delete_password(service, username)


class TokenStore:
    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._backend = backend or _SystemKeyring()

    def get(self) -> Token | None:
        value = self._backend.get_password(SERVICE, USERNAME)
        return Token(value) if value else None

    def set(self, token: Token) -> None:
        self._backend.set_password(SERVICE, USERNAME, token.reveal())

    def delete(self) -> None:
        with contextlib.suppress(Exception):  # nothing was stored
            self._backend.delete_password(SERVICE, USERNAME)


def scrub_token(text: str, token: Token | None) -> str:
    """Remove a token from text that might be shown or saved (e.g. an error message)."""
    return text.replace(token.reveal(), "hf_****") if token else text
