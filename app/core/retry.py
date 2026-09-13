"""Retry classification and exponential-backoff policy."""

from dataclasses import dataclass
from enum import StrEnum


class ErrorClassification(StrEnum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay_seconds < 0 or self.maximum_delay_seconds < 0:
            raise ValueError("Retry policy values must be non-negative and include at least one attempt.")

    def delay_for(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError("attempt is one-based")
        return min(self.maximum_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))

    def should_retry(self, classification: ErrorClassification, attempt: int) -> bool:
        return classification is ErrorClassification.RETRYABLE and attempt < self.max_attempts
