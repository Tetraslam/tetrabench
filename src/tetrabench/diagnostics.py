"""Fixed, public diagnostics. Provider messages and chained errors stay private."""

from __future__ import annotations

import builtins
import re
from collections.abc import Iterable

from botocore import exceptions as boto_errors
from modal import exception as modal_errors

INSTALL_COMMAND = "uv tool install --python 3.12 tetrabench==0.2.0"

_MISSING_RESOURCE_ADVICE = (
    "The controller Secret is unavailable in the selected Modal environment. "
    "Run tetrabench controller info with the same profile for environment_name "
    "and secret_name, then create that Secret in that environment."
)
_MESSAGES = {
    "unsupported_python": (
        "Tetrabench 0.2.0 requires CPython 3.12; its serialized Modal controller "
        f"also uses Python 3.12. Reinstall with: {INSTALL_COMMAND}"
    ),
    "unsupported_platform": (
        "Tetrabench execution requires Linux. Run from a Linux host with Python 3.12; "
        f"install with: {INSTALL_COMMAND}"
    ),
    "runtime_dependency_mismatch": (
        "This release requires Harbor 0.22.0 and Modal 1.5.4. Restore the locked "
        f"installation with: {INSTALL_COMMAND} --reinstall"
    ),
    "missing_credentials": (
        "Required credentials are unavailable. Configure the named environment "
        "variables or the provider's standard credential chain, then retry."
    ),
    "authentication_required": (
        "Modal authentication is missing or invalid. Authenticate with "
        "uvx --from modal==1.5.4 modal setup, then retry."
    ),
    "authentication_expired": (
        "Provider authentication has expired. Renew the credentials through your "
        "provider's login or secret manager, then retry."
    ),
    "modal_environment_missing": (
        "The selected Modal environment does not exist. Run tetrabench controller "
        "info with the same profile for its exact versioned environment_name; "
        "create it with uvx --from modal==1.5.4 modal environment create NAME."
    ),
    "modal_secret_missing": _MISSING_RESOURCE_ADVICE,
    "modal_resource_missing": (
        "A required Modal resource is unavailable. Check the app, function, Secret "
        "and versioned environment printed by tetrabench controller info with the "
        "same profile; deploy that controller before submitting a new run."
    ),
    "provider_permission_denied": (
        "The provider denied access. Check the selected account, environment and "
        "credential permissions for this operation."
    ),
    "provider_request_failed": (
        "Provider request failed. Check provider availability and configuration; "
        "inspect run status before retrying a mutation."
    ),
}
_OPERATIONS = frozenset(
    {
        "run",
        "submit",
        "doctor",
        "controller_deploy",
        "controller_build",
        "modal_auth",
        "modal_environment",
        "modal_secret",
        "status",
        "result",
        "cancel",
        "recover",
        "runs",
        "artifacts_pull",
        "storage_read",
        "provider_request",
    }
)


def _operation(value: str) -> str:
    # Call sites supply operation labels, never native URLs or request descriptions.
    return value if value in _OPERATIONS else "provider_request"


def _exception_type(error: Exception) -> str:
    # SDK exception classes are local code; dynamic provider-generated classes and
    # subclass names are not. Report their nearest known base instead.
    for cls in type(error).__mro__:
        for module in (boto_errors, modal_errors, builtins):
            if (
                cls.__module__ == module.__name__
                and getattr(module, cls.__name__, None) is cls
            ):
                return cls.__name__
    return "Exception"


class DiagnosticError(ValueError):
    """A safe error usable by both human and canonical-JSON renderers."""

    def __init__(
        self,
        code: str,
        *,
        operation: str,
        cause: Exception | None = None,
        credential_names: Iterable[str] = (),
    ) -> None:
        if code not in _MESSAGES:
            raise ValueError("unknown diagnostic code")
        names = tuple(sorted(set(credential_names)))
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) for name in names):
            raise ValueError("credential references must be environment variable names")
        self.code = code
        self.operation = _operation(operation)
        self.exception_type = (
            _exception_type(cause) if cause is not None else type(self).__name__
        )
        self.credential_names = names
        message = _MESSAGES[code]
        if names:
            message += " Environment variables: " + ", ".join(names) + "."
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "error": str(self),
            "error_type": "preflight_error"
            if isinstance(self, PreflightError)
            else "provider_error",
            "code": self.code,
            "operation": self.operation,
            "exception_type": self.exception_type,
        }
        if self.credential_names:
            result["credential_names"] = list(self.credential_names)
        return result


class PreflightError(DiagnosticError):
    """A local compatibility or credential check failed before execution."""


def missing_credentials(
    names: Iterable[str], *, operation: str = "run"
) -> PreflightError:
    """Build a safe missing-credential error from configured names, not values."""
    return PreflightError(
        "missing_credentials", operation=operation, credential_names=names
    )


def sanitize_error(
    error: Exception, *, operation: str = "provider_request"
) -> DiagnosticError:
    """Map native types/codes to fixed advice without inspecting message text.

    Use only at provider boundaries; ordinary local validation errors should keep
    their existing renderer. A DiagnosticError survives repeated sanitization.
    """
    if isinstance(error, DiagnosticError):
        return error
    code = "provider_request_failed"
    if isinstance(
        error, (boto_errors.NoCredentialsError, boto_errors.PartialCredentialsError)
    ):
        code = "missing_credentials"
    elif isinstance(
        error, (boto_errors.UnauthorizedSSOTokenError, boto_errors.LoginRefreshRequired)
    ):
        code = "authentication_expired"
    elif isinstance(error, modal_errors.AuthError):
        code = "authentication_required"
    elif isinstance(error, modal_errors.PermissionDeniedError):
        code = "provider_permission_denied"
    elif isinstance(error, modal_errors.NotFoundError):
        if operation == "modal_environment":
            code = "modal_environment_missing"
        elif operation == "modal_secret":
            code = "modal_secret_missing"
        else:
            code = "modal_resource_missing"
    elif isinstance(error, boto_errors.ClientError):
        # Error.Code is the typed service discriminator, never Error.Message.
        response = error.response
        detail = response.get("Error") if isinstance(response, dict) else None
        native_code = detail.get("Code") if isinstance(detail, dict) else None
        if isinstance(native_code, str):
            if native_code in {
                "ExpiredToken",
                "ExpiredTokenException",
                "TokenRefreshRequired",
            }:
                code = "authentication_expired"
            elif native_code in {"AccessDenied", "AccessDeniedException"}:
                code = "provider_permission_denied"
    return DiagnosticError(code, operation=operation, cause=error)
