"""Connection adapter construction."""

from app.server.base import ConnectionConfig, Protocol, RemoteServer, RuntimeCredentials
from app.server.ftp import FTPServer
from app.server.sftp import SFTPServer


def create_server(config: ConnectionConfig, credentials: RuntimeCredentials) -> RemoteServer:
    if config.protocol is Protocol.SFTP:
        return SFTPServer(config, credentials)
    return FTPServer(config, credentials)
