from __future__ import annotations


class _DomainError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ValidationError(_DomainError):
    pass


class ConflictError(_DomainError):
    pass


class NotFound(_DomainError):
    pass


class Forbidden(_DomainError):
    pass
