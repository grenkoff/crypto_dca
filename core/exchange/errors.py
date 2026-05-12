from __future__ import annotations


class BybitError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class OrderRejectedError(BybitError):
    pass


class InsufficientBalanceError(BybitError):
    pass


class RateLimitedError(BybitError):
    pass
