"""Stable interface shared by all remote filesystem protocols."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO


class Protocol(StrEnum):
    SFTP = "SFTP"
    FTPS = "FTPS"
    FTP = "FTP"


@dataclass(frozen=True, slots=True)
class RuntimeCredentials:
    """Ephemeral authentication material; never serialize this object."""

    username: str
    password: str = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    protocol: Protocol
    host: str
    port: int
    remote_root: str = "/"
    timeout_seconds: float = 20.0
    verify_tls: bool = True
    verify_host_key: bool = True


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    path: str
    name: str
    is_directory: bool
    size: int | None = None
    modified_at: datetime | None = None
    mime_type: str | None = None
    is_symlink: bool = False


class RemoteServer(ABC):
    """No protocol-specific objects escape this boundary."""

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def list(self, path: str) -> list[RemoteEntry]: ...

    @abstractmethod
    def stat(self, path: str) -> RemoteEntry: ...

    @abstractmethod
    def download(self, remote_path: str, destination: Path | BinaryIO) -> None: ...

    @abstractmethod
    def upload(self, source: Path | BinaryIO, remote_path: str) -> None: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def rename(self, source: str, destination: str) -> None: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def mkdir(self, path: str) -> None: ...

    @abstractmethod
    def checksum(self, path: str, algorithm: str = "sha256") -> str | None: ...

    @abstractmethod
    def read_prefix(self, path: str, maximum_bytes: int) -> bytes:
        """Read only enough leading bytes for metadata inspection."""
        ...

    def forget_credentials(self) -> None:
        """Release runtime authentication material after a UI operation."""
