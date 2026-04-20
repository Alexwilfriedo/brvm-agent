"""Politique de retry commune pour les appels Anthropic.

On retry uniquement sur les erreurs transient (rate limit, timeout, 5xx).
Les erreurs 4xx non-rate-limit (ex: prompt mal formé, auth invalide) remontent
immédiatement — pas de sens à réessayer.
"""
from __future__ import annotations

import logging

from anthropic import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

_RETRYABLE = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

anthropic_retry = retry(
    retry=retry_if_exception_type(_RETRYABLE),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
