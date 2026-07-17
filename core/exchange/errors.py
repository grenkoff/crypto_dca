"""Typed exceptions for Bybit client errors."""

from __future__ import annotations


class BybitError(Exception):
    """Base error carrying the Bybit return code and message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class OrderRejectedError(BybitError):
    """Order was rejected by the exchange."""


class InsufficientBalanceError(BybitError):
    """Not enough balance to place the order."""


class RateLimitedError(BybitError):
    """Request was rate-limited by the exchange."""
