"""Credential-safe connectivity errors."""


class ServerError(RuntimeError):
    pass


class AuthenticationError(ServerError):
    def __init__(self) -> None:
        super().__init__("Authentication failed. Verify the username, credential, and server policy.")


class ConnectionFailed(ServerError):
    def __init__(self, reason: str = "The remote server could not be reached.") -> None:
        super().__init__(reason)


class PermissionDenied(ServerError):
    def __init__(self, operation: str) -> None:
        super().__init__(f"The account is not permitted to {operation}.")
