"""Read-only parsing of the standard database constants in wp-config.php."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class WordPressDatabaseSettings:
    host: str
    database: str
    username: str
    password: str = field(repr=False, compare=False)
    port: int = 3306


DEFINE = re.compile(
    r"define\s*\(\s*(['\"])(DB_(?:NAME|USER|PASSWORD|HOST))\1\s*,\s*(['\"])(.*?)\3\s*\)",
    re.IGNORECASE,
)


def parse_wordpress_database_settings(source: bytes) -> WordPressDatabaseSettings:
    """Extract literal DB_* defines without executing or evaluating PHP."""
    text = source.decode("utf-8", errors="replace")
    values = {name.upper(): value for _, name, _, value in DEFINE.findall(text)}
    missing = [name for name in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if name not in values]
    if missing:
        raise ValueError("wp-config.php is missing literal " + ", ".join(missing))
    host, port = values["DB_HOST"], 3306
    if ":" in host and host.rsplit(":", 1)[1].isdigit():
        host, port_text = host.rsplit(":", 1)
        port = int(port_text)
    return WordPressDatabaseSettings(host, values["DB_NAME"], values["DB_USER"], values["DB_PASSWORD"], port)
