"""Retry with exponential backoff and circuit breaker utilities."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Tuple, Type

from src.l1_agent.utils.logging import get_logger

logger = get_logger("retry")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Simple circuit breaker for external service calls."""

    failure_threshold: int = 5
    reset_timeout_seconds: int = 60
    _failure_count: int = field(default=0, init=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.reset_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning("Circuit breaker OPEN after %d failures", self._failure_count)

    def allow_request(self) -> bool:
        current = self.state
        return current in (CircuitState.CLOSED, CircuitState.HALF_OPEN)


async def retry_with_backoff(
    func: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    circuit_breaker: Optional[CircuitBreaker] = None,
    **kwargs: Any,
) -> Any:
    """Execute an async function with exponential backoff retry logic."""
    last_exception: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        if circuit_breaker and not circuit_breaker.allow_request():
            raise RuntimeError("Circuit breaker is OPEN; request blocked")

        try:
            result = await func(*args, **kwargs)
            if circuit_breaker:
                circuit_breaker.record_success()
            return result
        except retryable_exceptions as exc:
            last_exception = exc
            if circuit_breaker:
                circuit_breaker.record_failure()
            if attempt == max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning(
                "Attempt %d/%d failed: %s. Retrying in %.1fs",
                attempt,
                max_attempts,
                str(exc),
                delay,
            )
            await asyncio.sleep(delay)

    raise last_exception  # type: ignore[misc]
