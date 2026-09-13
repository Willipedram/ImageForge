# Security Policy

## Supported version

Security fixes are provided for the current `1.x` release line.

## Reporting

Report vulnerabilities privately to the project maintainers. Do not include production credentials, database dumps, private keys, customer images, or ProjectData in an issue.

## Security model

- Passwords are runtime-only or stored by Windows Credential Manager/DPAPI; application JSON, SQLite, logs, manifests, and backup metadata do not store them.
- SFTP host-key and FTPS certificate verification default to enabled.
- Remote paths reject parent traversal and null bytes.
- SQL values are parameterized and identifiers are allow-list validated.
- PHP serialization and JSON are parsed structurally; unsafe records require review.
- SVG active/external content, XML entities, malformed images, decompression bombs, oversized images, and uncertain color/animation conversions are rejected or retained.
- Destructive cleanup requires independent backup, filesystem, database, metadata, and HTTP gates.

Never upload a ProjectData directory when reporting a problem. Use exported redacted job summaries.

