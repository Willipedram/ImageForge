"""Safe website-domain inference for FTP accounts and control-panel paths."""

from __future__ import annotations

import ipaddress
import re

DOMAIN = re.compile(r"(?i)^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def infer_website_domain(host: str, username: str = "", remote_root: str = "") -> str | None:
    """Infer a public hostname without treating an FTP IP as a website domain."""
    candidates: list[str] = []
    if "@" in username:
        candidates.append(username.rsplit("@", 1)[1])
    match = re.search(r"(?i)(?:^|/)domains/([^/]+)/public_html(?:/|$)", remote_root)
    if match:
        candidates.append(match.group(1))
    candidates.append(host)
    for candidate in candidates:
        value = candidate.strip().rstrip(".").casefold()
        for prefix in ("ftp.", "www."):
            if value.startswith(prefix):
                value = value[len(prefix):]
        try:
            ipaddress.ip_address(value)
            continue
        except ValueError:
            pass
        if DOMAIN.fullmatch(value):
            return value
    return None
