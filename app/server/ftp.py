"""FTP and explicit-TLS FTPS adapter using Python's standard library."""

from __future__ import annotations

import ftplib
import hashlib
import logging
import mimetypes
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from app.server.base import ConnectionConfig, Protocol, RemoteEntry, RemoteServer, RuntimeCredentials
from app.server.errors import AuthenticationError, ConnectionFailed, PermissionDenied
from app.server.paths import normalize_remote_path

logger = logging.getLogger(__name__)
FTP_TRANSFER_BLOCK_SIZE = 256 * 1024


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

    def forget_credentials(self) -> None:
        self._credentials = RuntimeCredentials("", "")

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
        entry = next((entry for entry in self.list(parent or "/") if entry.name == name), None)
        if entry is None:
            # StopIteration has an empty message and used to escape preflight,
            # turning a harmless missing fallback path into an opaque discovery
            # failure. RemoteServer.stat follows normal filesystem semantics.
            raise FileNotFoundError(normalized)
        return entry

    def download(self, remote_path: str, destination: Path | BinaryIO) -> None:
        stream: BinaryIO
        owns_stream = isinstance(destination, Path)
        stream = destination.open("wb") if owns_stream else destination
        path = normalize_remote_path(remote_path)
        started = time.monotonic()
        transferred = 0

        def write(block: bytes):
            nonlocal transferred
            transferred += len(block)
            return stream.write(block)

        logger.info("FTP download started path=%s block_size=%d", path, FTP_TRANSFER_BLOCK_SIZE)
        try:
            self._require().retrbinary(
                f"RETR {path}", write, blocksize=FTP_TRANSFER_BLOCK_SIZE
            )
        finally:
            if owns_stream:
                stream.close()
        elapsed = max(time.monotonic() - started, 0.001)
        logger.info(
            "FTP download completed path=%s bytes=%d elapsed_seconds=%.2f rate_mbps=%.2f",
            path, transferred, elapsed, transferred * 8 / elapsed / 1_000_000,
        )

    def upload(self, source: Path | BinaryIO, remote_path: str) -> None:
        stream: BinaryIO
        owns_stream = isinstance(source, Path)
        stream = source.open("rb") if owns_stream else source
        path = normalize_remote_path(remote_path)
        size = source.stat().st_size if isinstance(source, Path) else None
        started = time.monotonic()
        logger.info("FTP upload started path=%s bytes=%s block_size=%d",
                    path, size if size is not None else "unknown", FTP_TRANSFER_BLOCK_SIZE)
        try:
            self._require().storbinary(
                f"STOR {path}", stream, blocksize=FTP_TRANSFER_BLOCK_SIZE
            )
        finally:
            if owns_stream:
                stream.close()
        elapsed = max(time.monotonic() - started, 0.001)
        rate = size * 8 / elapsed / 1_000_000 if size is not None else 0
        logger.info(
            "FTP upload completed path=%s bytes=%s elapsed_seconds=%.2f rate_mbps=%.2f",
            path, size if size is not None else "unknown", elapsed, rate,
        )

    def delete(self, path: str) -> None:
        self._require().delete(normalize_remote_path(path))

    def rename(self, source: str, destination: str) -> None:
        self._require().rename(normalize_remote_path(source), normalize_remote_path(destination))

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except (FileNotFoundError, PermissionDenied):
            return False

    def mkdir(self, path: str) -> None:
        self._require().mkd(normalize_remote_path(path))

    def checksum(self, path: str, algorithm: str = "sha256") -> str | None:
        if algorithm not in hashlib.algorithms_available:
            raise ValueError("Unsupported checksum algorithm.")
        # Standard FTP has no portable remote checksum command. Downloading the
        # complete file here duplicated every initial transfer. Returning None
        # activates the workflow's existing local/fallback verification path.
        return None

    def read_prefix(self, path: str, maximum_bytes: int) -> bytes:
        """Use a data socket directly so only a bounded header is transferred."""
        client = self._require()
        # transfercmd(), unlike retrbinary(), does not select binary mode.
        # Image headers must never be transferred through FTP ASCII conversion.
        client.voidcmd("TYPE I")
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
            self._finish_partial_transfer(client)
        return bytes(chunks)

    @staticmethod
    def _finish_partial_transfer(client: ftplib.FTP) -> None:
        """Synchronize the control channel after closing a bounded RETR early.

        Some hosting FTP proxies emit more than one preliminary 1xx response
        (for example a multiline ASCII-mode warning followed by transfer
        statistics).  ``ftplib.voidresp`` rejects that extra response even
        though the transfer is valid, leaving the final 226 queued and causing
        the website scan to fail.  Drain those preliminary replies before the
        next FTP command while still surfacing unexpected server responses.
        """
        for _ in range(4):
            try:
                client.voidresp()
                return
            except ftplib.error_reply as exc:
                if str(exc).lstrip().startswith("1"):
                    continue
                raise
            except ftplib.error_temp as exc:
                # A server may report an intentionally interrupted partial
                # transfer as 426. The control channel is synchronized then.
                if str(exc).lstrip().startswith("426"):
                    return
                raise
        raise ftplib.error_reply("Too many preliminary FTP transfer responses")
