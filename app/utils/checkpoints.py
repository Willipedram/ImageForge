"""Consistent, credential-safe operational keypoint logging."""

from __future__ import annotations

import logging


def log_keypoint(
    logger: logging.Logger,
    checkpoint: str,
    status: str,
    *,
    job_id: str | None = None,
    item_id: str | None = None,
    error: BaseException | None = None,
) -> None:
    """Log a searchable process boundary without serializing operation payloads."""
    fields = [f"status={status.upper()}", f"checkpoint={checkpoint}"]
    if job_id:
        fields.append(f"job={job_id}")
    if item_id:
        fields.append(f"item={item_id}")
    if error:
        fields.extend((f"error_type={type(error).__name__}", f"error={error}"))
    message = "[KEYPOINT] " + " ".join(fields)
    if status.casefold() in {"failed", "stopped"}:
        logger.error(message)
    elif status.casefold() in {"warning", "blocked"}:
        logger.warning(message)
    else:
        logger.info(message)
