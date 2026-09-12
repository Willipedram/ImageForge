"""SFTP adapter backed by Paramiko, imported only when SFTP is used."""

from __future__ import annotations

import hashlib
import mimetypes
import stat as stat_module
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from app.server.base import ConnectionConfig, Protocol, RemoteEntry, RemoteServer, RuntimeCredentials
from app.server.errors import AuthenticationError, ConnectionFailed, PermissionDenied
from app.server.paths import normalize_remote_path


class SFTPServer(RemoteServer):
    def __init__(self, config: ConnectionConfig, credentials: RuntimeCredentials) -> None:
        if config.protocol is not Protocol.SFTP:
            raise ValueError("SFTPServer requires SFTP configuration.")
        self.config = config
        self._credentials = credentials
        self._ssh: Any = None
        self._client: Any = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self) -> None:
        import paramiko

        try:
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(
                paramiko.RejectPolicy() if self.config.verify_host_key else paramiko.WarningPolicy()
            )
            client.connect(
                self.config.host, port=self.config.port, username=self._credentials.username,
                password=self._credentials.password, timeout=self.config.timeout_seconds,
                banner_timeout=self.config.timeout_seconds, auth_timeout=self.config.timeout_seconds,
                allow_agent=False, look_for_keys=False,
            )
            self._ssh = client
            self._client = client.open_sftp()
        except paramiko.AuthenticationException:
            self.disconnect()
            raise AuthenticationError() from None
        except (OSError, paramiko.SSHException):
            self.disconnect()
            raise ConnectionFailed() from None

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
        if self._ssh:
            self._ssh.close()
        self._client = self._ssh = None

    def _require(self) -> Any:
        if self._client is None:
            raise ConnectionFailed("The remote connection is not open.")
        return self._client

    def list(self, path: str) -> list[RemoteEntry]:
        directory = normalize_remote_path(path)
        result = []
        try:
            values = self._require().listdir_attr(directory)
        except PermissionError:
            raise PermissionDenied("list this remote directory") from None
        for value in values:
            child = normalize_remote_path(f"{directory}/{value.filename}")
            result.append(RemoteEntry(
                child, value.filename, stat_module.S_ISDIR(value.st_mode), value.st_size,
                datetime.fromtimestamp(value.st_mtime, UTC) if value.st_mtime else None,
                mimetypes.guess_type(value.filename)[0], stat_module.S_ISLNK(value.st_mode),
            ))
        return result

    def stat(self, path: str) -> RemoteEntry:
        normalized = normalize_remote_path(path)
        value = self._require().lstat(normalized)
        return RemoteEntry(normalized, normalized.rsplit("/", 1)[-1], stat_module.S_ISDIR(value.st_mode),
                           value.st_size, datetime.fromtimestamp(value.st_mtime, UTC),
                           mimetypes.guess_type(normalized)[0], stat_module.S_ISLNK(value.st_mode))

    def download(self, remote_path: str, destination: Path | BinaryIO) -> None:
        if isinstance(destination, Path):
            self._require().get(normalize_remote_path(remote_path), str(destination))
        else:
            self._require().getfo(normalize_remote_path(remote_path), destination)

    def upload(self, source: Path | BinaryIO, remote_path: str) -> None:
        if isinstance(source, Path):
            self._require().put(str(source), normalize_remote_path(remote_path))
        else:
            self._require().putfo(source, normalize_remote_path(remote_path))

    def delete(self, path: str) -> None:
        self._require().remove(normalize_remote_path(path))

    def rename(self, source: str, destination: str) -> None:
        self._require().rename(normalize_remote_path(source), normalize_remote_path(destination))

    def exists(self, path: str) -> bool:
        try:
            self._require().lstat(normalize_remote_path(path))
            return True
        except OSError:
            return False

    def mkdir(self, path: str) -> None:
        self._require().mkdir(normalize_remote_path(path))

    def checksum(self, path: str, algorithm: str = "sha256") -> str | None:
        if algorithm not in hashlib.algorithms_available:
            raise ValueError("Unsupported checksum algorithm.")
        digest = hashlib.new(algorithm)
        with self._require().open(normalize_remote_path(path), "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def read_prefix(self, path: str, maximum_bytes: int) -> bytes:
        with self._require().open(normalize_remote_path(path), "rb") as stream:
            return stream.read(maximum_bytes)
