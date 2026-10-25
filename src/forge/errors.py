"""Failure taxonomy. The class decides retry safety and what the repair engine may do."""
from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    TRANSIENT = "transient"                    # timeouts, 5xx, 429, node down -> retry with backoff
    INVALID_INPUT = "invalid_input"            # schema/argument validation -> fix args, never blind retry
    PERMISSION_DENIED = "permission_denied"    # policy said no -> never retry, never auto-repair
    NEEDS_APPROVAL = "needs_approval"          # pause run until a human decides
    NEEDS_USER = "needs_user"                  # CAPTCHA, login wall, locked desktop -> hand off to user
    MISSING_CREDENTIAL = "missing_credential"  # secret absent/expired -> notify, no repair
    INTERFACE_CHANGED = "interface_changed"    # selector/page/API shape drifted -> repair candidate
    RESOURCE_UNAVAILABLE = "resource_unavailable"  # GPU busy, desktop leased, file locked -> defer
    DEFECT = "defect"                          # bug in workflow/tool logic -> repair candidate
    CANCELLED = "cancelled"


RETRYABLE = {ErrorClass.TRANSIENT, ErrorClass.RESOURCE_UNAVAILABLE}
REPAIRABLE = {ErrorClass.INTERFACE_CHANGED, ErrorClass.DEFECT, ErrorClass.INVALID_INPUT}


class ForgeError(Exception):
    error_class: ErrorClass = ErrorClass.DEFECT

    def __init__(self, message: str, error_class: ErrorClass | None = None, **details):
        super().__init__(message)
        if error_class is not None:
            self.error_class = error_class
        self.details = details


class TransientError(ForgeError):
    error_class = ErrorClass.TRANSIENT


class PermissionDenied(ForgeError):
    error_class = ErrorClass.PERMISSION_DENIED


class NeedsApproval(ForgeError):
    error_class = ErrorClass.NEEDS_APPROVAL

    def __init__(self, message: str, approval_id: str):
        super().__init__(message, approval_id=approval_id)
        self.approval_id = approval_id


class NeedsUser(ForgeError):
    error_class = ErrorClass.NEEDS_USER


class InterfaceChanged(ForgeError):
    error_class = ErrorClass.INTERFACE_CHANGED


class ResourceUnavailable(ForgeError):
    error_class = ErrorClass.RESOURCE_UNAVAILABLE


class InvalidInput(ForgeError):
    error_class = ErrorClass.INVALID_INPUT


class EmergencyStop(ForgeError):
    error_class = ErrorClass.CANCELLED


def classify(exc: BaseException) -> ErrorClass:
    """Map arbitrary exceptions onto the taxonomy."""
    if isinstance(exc, ForgeError):
        return exc.error_class
    import httpx
    from pydantic import ValidationError
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, TimeoutError, ConnectionError)):
        return ErrorClass.TRANSIENT
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429 or code >= 500:
            return ErrorClass.TRANSIENT
        if code in (401, 403):
            return ErrorClass.MISSING_CREDENTIAL
        if code in (404, 410):
            return ErrorClass.INTERFACE_CHANGED
        return ErrorClass.INVALID_INPUT
    if isinstance(exc, ValidationError):
        return ErrorClass.INVALID_INPUT
    if isinstance(exc, PermissionError):
        return ErrorClass.PERMISSION_DENIED
    if exc.__class__.__name__ in ("ElementNotFoundError", "MatchError"):  # pywinauto
        return ErrorClass.INTERFACE_CHANGED
    return ErrorClass.DEFECT
