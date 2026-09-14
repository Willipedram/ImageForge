"""Runtime and Windows Credential Manager secret providers."""

from __future__ import annotations

import importlib
import re
import sys
from typing import Protocol as TypingProtocol

from app.server.base import RuntimeCredentials


class CredentialProvider(TypingProtocol):
    def load(self, credential_id: str) -> RuntimeCredentials | None: ...

    def save(self, credential_id: str, credentials: RuntimeCredentials) -> None: ...

    def delete(self, credential_id: str) -> None: ...


class RuntimeCredentialProvider:
    """Deliberately non-persistent provider for portable/runtime-only sessions."""

    def load(self, credential_id: str) -> RuntimeCredentials | None:
        return None

    def save(self, credential_id: str, credentials: RuntimeCredentials) -> None:
        raise NotImplementedError("Persistent credentials require Windows Credential Manager / DPAPI.")

    def delete(self, credential_id: str) -> None:
        return None


class WindowsCredentialProvider:
    """Stores secrets through Windows Credential Manager, which protects them with DPAPI."""

    PREFIX = "ImageForge/"

    def __init__(self, backend=None) -> None:
        if backend is None and sys.platform != "win32":
            raise OSError("Windows Credential Manager is available only on Windows.")
        self.backend = backend or importlib.import_module("win32cred")

    def load(self, credential_id: str) -> RuntimeCredentials | None:
        target = self._target(credential_id)
        try:
            record = self.backend.CredRead(target, self.backend.CRED_TYPE_GENERIC)
        except self.backend.error as exc:
            if getattr(exc, "winerror", None) == 1168:
                return None
            raise
        blob = record["CredentialBlob"]
        password = blob.decode("utf-16-le") if isinstance(blob, bytes) else str(blob)
        return RuntimeCredentials(str(record.get("UserName", "")), password)

    def save(self, credential_id: str, credentials: RuntimeCredentials) -> None:
        record = {
            "Type": self.backend.CRED_TYPE_GENERIC,
            "TargetName": self._target(credential_id),
            "UserName": credentials.username,
            # pywin32's CredWrite wrapper accepts Unicode text and performs
            # the native UTF-16 conversion itself. Passing bytes raises
            # "Objects of type 'bytes' can not be converted to Unicode".
            "CredentialBlob": credentials.password,
            "Persist": self.backend.CRED_PERSIST_LOCAL_MACHINE,
            "Comment": "ImageForge secure runtime credential",
        }
        self.backend.CredWrite(record, 0)

    def delete(self, credential_id: str) -> None:
        try:
            self.backend.CredDelete(self._target(credential_id), self.backend.CRED_TYPE_GENERIC)
        except self.backend.error as exc:
            if getattr(exc, "winerror", None) != 1168:
                raise

    @classmethod
    def _target(cls, credential_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,180}", credential_id):
            raise ValueError("Credential ID contains unsupported characters.")
        return cls.PREFIX + credential_id
