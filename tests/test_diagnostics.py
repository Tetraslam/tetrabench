from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import pytest
from botocore import exceptions as boto_errors
from modal import exception as modal_errors

from tetrabench.canonical_json import dumps_canonical_json, loads_canonical_json
from tetrabench.diagnostics import (
    DiagnosticError,
    PreflightError,
    missing_credentials,
    sanitize_error,
)
from tetrabench.preflight import check_runtime

PRIVATE = "AWS_SECRET_ACCESS_KEY=value https://private.invalid/?token=private-secret"


@pytest.mark.parametrize(
    "error, operation, code, exception_type",
    [
        (
            modal_errors.AuthError(PRIVATE),
            "controller_deploy",
            "authentication_required",
            "AuthError",
        ),
        (
            modal_errors.NotFoundError(PRIVATE),
            "modal_environment",
            "modal_environment_missing",
            "NotFoundError",
        ),
        (
            modal_errors.NotFoundError(PRIVATE),
            "modal_secret",
            "modal_secret_missing",
            "NotFoundError",
        ),
        (
            modal_errors.NotFoundError(PRIVATE),
            "run",
            "modal_resource_missing",
            "NotFoundError",
        ),
        (
            modal_errors.PermissionDeniedError(PRIVATE),
            "run",
            "provider_permission_denied",
            "PermissionDeniedError",
        ),
        (
            modal_errors.InvalidError(PRIVATE),
            "controller_deploy",
            "provider_request_failed",
            "InvalidError",
        ),
        (
            modal_errors.VersionError(PRIVATE),
            "run",
            "provider_request_failed",
            "VersionError",
        ),
        (
            modal_errors.OutputExpiredError(PRIVATE),
            "result",
            "provider_request_failed",
            "OutputExpiredError",
        ),
        (
            boto_errors.NoCredentialsError(),
            "submit",
            "missing_credentials",
            "NoCredentialsError",
        ),
        (
            boto_errors.PartialCredentialsError(provider=PRIVATE, cred_var=PRIVATE),
            "submit",
            "missing_credentials",
            "PartialCredentialsError",
        ),
        (
            boto_errors.UnauthorizedSSOTokenError(),
            "run",
            "authentication_expired",
            "UnauthorizedSSOTokenError",
        ),
        (
            boto_errors.LoginRefreshRequired(),
            "run",
            "authentication_expired",
            "LoginRefreshRequired",
        ),
        (
            boto_errors.EndpointConnectionError(endpoint_url=PRIVATE),
            "status",
            "provider_request_failed",
            "EndpointConnectionError",
        ),
        (RuntimeError(PRIVATE), "doctor", "provider_request_failed", "RuntimeError"),
    ],
)
def test_native_errors_never_reflect_provider_strings(
    error, operation, code, exception_type
):
    safe = sanitize_error(error, operation=operation)
    report = safe.as_dict()
    assert report["code"] == code
    assert report["operation"] == operation
    assert report["exception_type"] == exception_type
    assert report["error_type"] == "provider_error"
    data = dumps_canonical_json(report)
    assert loads_canonical_json(data) == report
    assert b"private" not in data
    assert b"AWS_SECRET_ACCESS_KEY=value" not in data
    assert sanitize_error(safe, operation="doctor") is safe


@pytest.mark.parametrize(
    "native_code, code",
    [
        ("ExpiredToken", "authentication_expired"),
        ("ExpiredTokenException", "authentication_expired"),
        ("TokenRefreshRequired", "authentication_expired"),
        ("AccessDenied", "provider_permission_denied"),
        ("AccessDeniedException", "provider_permission_denied"),
        (PRIVATE, "provider_request_failed"),
        ("InvalidToken", "provider_request_failed"),
    ],
)
def test_structured_native_codes_are_allowlisted_not_echoed(native_code, code):
    error = boto_errors.ClientError(
        {
            "Error": {"Code": native_code, "Message": PRIVATE},
            "ResponseMetadata": {"HTTPHeaders": {"authorization": PRIVATE}},
        },
        PRIVATE,
    )
    safe = sanitize_error(error, operation="run")
    assert safe.code == code
    assert safe.exception_type == "ClientError"
    assert "private" not in str(safe.as_dict())


def test_unknown_messages_are_not_parsed_or_formatted():
    class Unprintable(modal_errors.InvalidError):
        def __str__(self):
            pytest.fail("formatted native message")

    safe = sanitize_error(Unprintable(PRIVATE), operation=PRIVATE)
    assert safe.code == "provider_request_failed"
    assert safe.operation == "provider_request"
    assert safe.exception_type == "InvalidError"


def test_dynamic_exception_name_and_chained_causes_are_not_reflected():
    error = type("private_secret", (modal_errors.Error,), {})(PRIVATE)
    error.__cause__ = RuntimeError(PRIVATE)
    safe = sanitize_error(error, operation="run")
    assert safe.exception_type == "Error"
    assert "private" not in str(safe.as_dict())


@pytest.mark.parametrize(
    "exception_class",
    [
        cls
        for module, base in (
            (boto_errors, boto_errors.BotoCoreError),
            (modal_errors, modal_errors.Error),
        )
        for cls in vars(module).values()
        if isinstance(cls, type) and issubclass(cls, base)
    ],
)
def test_every_installed_provider_exception_family_is_redacted(exception_class):
    # Bypass native formatting constructors to poison every family uniformly.
    # Sanitization must not depend on constructor-specific private attributes.
    error = exception_class.__new__(exception_class)
    Exception.__init__(error, PRIVATE)
    safe = sanitize_error(error, operation="run")
    assert safe.exception_type == exception_class.__name__
    assert "private" not in str(safe.as_dict())


def test_missing_credentials_reports_names_only(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", PRIVATE)
    safe = missing_credentials(
        ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    )
    assert safe.credential_names == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    assert safe.as_dict()["error_type"] == "preflight_error"
    assert "OPENAI_API_KEY" in str(safe)
    assert "private" not in str(safe.as_dict())


@pytest.mark.parametrize(
    "name", [PRIVATE, "API_KEY=value", "SECRET\nTOKEN", "a" * 200, ""]
)
def test_missing_credentials_rejects_values_and_non_names(name):
    with pytest.raises(ValueError, match="environment variable names") as caught:
        missing_credentials([name])
    assert name not in str(caught.value) if name else True


def test_diagnostic_codes_cannot_carry_freeform_messages():
    with pytest.raises(ValueError, match="unknown diagnostic code"):
        DiagnosticError(PRIVATE, operation="run")


def test_supported_runtime_is_offline(monkeypatch):
    calls = []

    def installed(name):
        calls.append(name)
        return {"harbor": "0.22.0", "modal": "1.5.4"}[name]

    monkeypatch.setattr("tetrabench.preflight.version", installed)
    report = check_runtime(
        "doctor",
        python_version=(3, 12, 13),
        platform_name="linux",
        implementation="cpython",
    )
    assert report["status"] == "ok"
    assert report["python_version"] == report["controller_python_version"] == "3.12"
    assert calls == ["harbor", "modal"]


@pytest.mark.parametrize("python_version", [(3, 11), (3, 13), (3, 14), (4, 0)])
def test_python_mismatch_precedes_dependency_inspection(monkeypatch, python_version):
    monkeypatch.setattr(
        "tetrabench.preflight.version", lambda _: pytest.fail("dependency inspection")
    )
    with pytest.raises(PreflightError) as caught:
        check_runtime(
            "run",
            python_version=python_version,
            platform_name="linux",
            implementation="cpython",
        )
    assert caught.value.code == "unsupported_python"
    assert caught.value.operation == "run"
    assert f"uv tool install --python 3.12 tetrabench=={version('tetrabench')}" in str(
        caught.value
    )


@pytest.mark.parametrize(
    "code",
    ["unsupported_python", "unsupported_platform", "runtime_dependency_mismatch"],
)
@pytest.mark.parametrize("installed", ["0.2.0", "0.3.0", "0.4.0rc1"])
def test_runtime_advice_uses_installed_distribution_version(
    monkeypatch, code, installed
):
    monkeypatch.setattr("tetrabench.diagnostics.version", lambda name: installed)
    error = PreflightError(code, operation="run")
    assert f"tetrabench=={installed}" in str(error)
    if code == "unsupported_python":
        assert f"Tetrabench {installed} requires CPython 3.12" in str(error)


def test_runtime_advice_without_package_metadata_does_not_guess_a_version(monkeypatch):
    def missing(name):
        raise PackageNotFoundError(PRIVATE)

    monkeypatch.setattr("tetrabench.diagnostics.version", missing)
    error = PreflightError("unsupported_python", operation="run")
    assert "Tetrabench requires CPython 3.12" in str(error)
    assert "uv tool install --python 3.12 tetrabench" in str(error)
    assert "tetrabench==" not in str(error)
    assert PRIVATE not in str(error)


@pytest.mark.parametrize("platform_name", ["win32", "darwin", "freebsd", PRIVATE])
def test_platform_mismatch_does_not_echo_platform(platform_name):
    with pytest.raises(PreflightError) as caught:
        check_runtime("doctor", platform_name=platform_name)
    assert caught.value.code == "unsupported_platform"
    assert "private" not in str(caught.value.as_dict())


def test_non_cpython_execution_is_unsupported():
    with pytest.raises(PreflightError, match=r"CPython 3\.12"):
        check_runtime(
            "run", python_version=(3, 12), platform_name="linux", implementation="pypy"
        )


@pytest.mark.parametrize("installed", ["0.21.0", "1.5.3", "99.0.0"])
def test_runtime_dependency_drift_fails_closed(monkeypatch, installed):
    monkeypatch.setattr("tetrabench.preflight.version", lambda _: installed)
    with pytest.raises(PreflightError) as caught:
        check_runtime(
            "run",
            python_version=(3, 12),
            platform_name="linux",
            implementation="cpython",
        )
    assert caught.value.code == "runtime_dependency_mismatch"


def test_missing_runtime_dependency_is_actionable(monkeypatch):
    def missing(_name):
        raise PackageNotFoundError(PRIVATE)

    monkeypatch.setattr("tetrabench.preflight.version", missing)
    with pytest.raises(PreflightError) as caught:
        check_runtime(
            "run",
            python_version=(3, 12),
            platform_name="linux",
            implementation="cpython",
        )
    assert caught.value.code == "runtime_dependency_mismatch"
    assert "private" not in str(caught.value)
