"""Consistent, credential-safe operational keypoint logging."""

from __future__ import annotations

import logging
import json
from collections.abc import Mapping

from app.utils.logging import redact


def log_keypoint(
    logger: logging.Logger,
    checkpoint: str,
    status: str,
    *,
    job_id: str | None = None,
    item_id: str | None = None,
    error: BaseException | None = None,
    context: Mapping[str, object] | None = None,
) -> None:
    """Log a searchable process boundary without serializing operation payloads."""
    fields = [f"status={status.upper()}", f"checkpoint={checkpoint}"]
    if job_id:
        fields.append(f"job={job_id}")
    if item_id:
        fields.append(f"item={item_id}")
    if error:
        fields.extend((f"error_type={type(error).__name__}", f"error={error}"))
    if context:
        safe_context = {
            str(key): redact(str(value).replace("\r", " ").replace("\n", " ")[:240])
            for key, value in context.items()
            if value is not None
        }
        fields.append("context=" + json.dumps(safe_context, ensure_ascii=False, sort_keys=True))
    message = "[KEYPOINT] " + " ".join(fields)
    if status.casefold() in {"failed", "stopped"}:
        logger.error(message)
    elif status.casefold() in {"warning", "blocked"}:
        logger.warning(message)
    else:
        logger.info(message)
