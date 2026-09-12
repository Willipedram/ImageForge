"""Credential-provider contract for a future Windows Credential Manager backend."""

from __future__ import annotations

from typing import Protocol as TypingProtocol

from app.server.base import RuntimeCredentials


class CredentialProvider(TypingProtocol):
    def load(self, credential_id: str) -> RuntimeCredentials | None: ...

    def save(self, credential_id: str, credentials: RuntimeCredentials) -> None: ...

    def delete(self, credential_id: str) -> None: ...


class RuntimeCredentialProvider:
    """Deliberately non-persistent provider used until DPAPI integration lands."""

    def load(self, credential_id: str) -> RuntimeCredentials | None:
        return None

    def save(self, credential_id: str, credentials: RuntimeCredentials) -> None:
        raise NotImplementedError("Persistent credentials require Windows Credential Manager / DPAPI.")

    def delete(self, credential_id: str) -> None:
        return None
