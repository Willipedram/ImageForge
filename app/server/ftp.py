"""FTP and explicit-TLS FTPS adapter using Python's standard library."""

from __future__ import annotations

import ftplib
import hashlib
import io
import mimetypes
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from app.server.base import ConnectionConfig, Protocol, RemoteEntry, RemoteServer, RuntimeCredentials
from app.server.errors import AuthenticationError, ConnectionFailed, PermissionDenied
from app.server.paths import normalize_remote_path


class FTPServer(RemoteServer):
    def __init__(self, config: ConnectionConfig, credentials: RuntimeCredentials) -> None:
        if config.protocol not in {Protocol.FTP, Protocol.FTPS}:
            raise ValueError("FTPServer requires FTP or FTPS configuration.")
        self.config = config
        self._credentials = credentials
        self._client: ftplib.FTP | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self) -> None:
        try:
            if self.config.protocol is Protocol.FTPS:
                context = ssl.create_default_context()
                if not self.config.verify_tls:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                client: ftplib.FTP = ftplib.FTP_TLS(context=context, timeout=self.config.timeout_seconds)
            else:
                client = ftplib.FTP(timeout=self.config.timeout_seconds)
            client.connect(self.config.host, self.config.port)
            client.login(self._credentials.username, self._credentials.password)
            if isinstance(client, ftplib.FTP_TLS):
                client.prot_p()
            self._client = client
        except ftplib.error_perm as exc:
            self.disconnect()
            if str(exc).startswith("530"):
                raise AuthenticationError() from None
            raise ConnectionFailed("The server rejected the connection request.") from None
        except (OSError, EOFError, ftplib.Error):
            self.disconnect()
            raise ConnectionFailed() from None

    def disconnect(self) -> None:
        client, self._client = self._client, None
        if client:
            try:
                client.quit()
            except (OSError, EOFError, ftplib.Error):
                client.close()

    def _require(self) -> ftplib.FTP:
        if not self._client:
            raise ConnectionFailed("The remote connection is not open.")
        return self._client

    def list(self, path: str) -> list[RemoteEntry]:
        directory = normalize_remote_path(path)
        entries: list[RemoteEntry] = []
        try:
            for name, facts in self._require().mlsd(directory, facts=["type", "size", "modify"]):
                if name in {".", ".."}:
                    continue
                kind = facts.get("type", "file")
                modified = None
                if facts.get("modify"):
                    modified = datetime.strptime(facts["modify"][:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
                child = normalize_remote_path(f"{directory}/{name}")
                entries.append(RemoteEntry(
                    child, name, kind in {"dir", "cdir", "pdir"},
                    int(facts["size"]) if facts.get("size", "").isdigit() else None,
                    modified, mimetypes.guess_type(name)[0], kind == "OS.unix=slink",
                ))
        except ftplib.error_perm:
            raise PermissionDenied("list this remote directory") from None
        return entries

    def stat(self, path: str) -> RemoteEntry:
        normalized = normalize_remote_path(path)
        if normalized == "/":
            return RemoteEntry("/", "/", True)
        parent, name = normalized.rsplit("/", 1)
        return next(entry for entry in self.list(parent or "/") if entry.name == name)

    def download(self, remote_path: str, destination: Path | BinaryIO) -> None:
        stream: BinaryIO
        owns_stream = isinstance(destination, Path)
        stream = destination.open("wb") if owns_stream else destination
        try:
            self._require().retrbinary(f"RETR {normalize_remote_path(remote_path)}", stream.write)
        finally:
            if owns_stream:
                stream.close()

    def upload(self, source: Path | BinaryIO, remote_path: str) -> None:
        stream: BinaryIO
        owns_stream = isinstance(source, Path)
        stream = source.open("rb") if owns_stream else source
        try:
            self._require().storbinary(f"STOR {normalize_remote_path(remote_path)}", stream)
        finally:
            if owns_stream:
                stream.close()

    def delete(self, path: str) -> None:
        self._require().delete(normalize_remote_path(path))

    def rename(self, source: str, destination: str) -> None:
        self._require().rename(normalize_remote_path(source), normalize_remote_path(destination))

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except (StopIteration, PermissionDenied):
            return False

    def mkdir(self, path: str) -> None:
        self._require().mkd(normalize_remote_path(path))

    def checksum(self, path: str, algorithm: str = "sha256") -> str | None:
        if algorithm not in hashlib.algorithms_available:
            raise ValueError("Unsupported checksum algorithm.")
        buffer = io.BytesIO()
        self.download(path, buffer)
        return hashlib.new(algorithm, buffer.getvalue()).hexdigest()

    def read_prefix(self, path: str, maximum_bytes: int) -> bytes:
        """Use a data socket directly so only a bounded header is transferred."""
        client = self._require()
        socket = client.transfercmd(f"RETR {normalize_remote_path(path)}")
        chunks = bytearray()
        try:
            while len(chunks) < maximum_bytes:
                chunk = socket.recv(min(8192, maximum_bytes - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
        finally:
            socket.close()
            # Closing a partial transfer can produce an expected 426 response.
            try:
                client.voidresp()
            except ftplib.error_temp:
                pass
        return bytes(chunks)
