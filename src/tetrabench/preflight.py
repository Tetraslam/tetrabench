"""Offline execution compatibility checks; importing this module does no I/O."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version

from tetrabench.diagnostics import PreflightError

CONTROLLER_PYTHON_VERSION = "3.12"


def check_runtime(
    operation: str = "run",
    *,
    python_version: tuple[int, ...] | None = None,
    platform_name: str | None = None,
    implementation: str | None = None,
) -> dict[str, object]:
    """Check before any provider client, image build or execution-side mutation.

    Explicit runtime facts support doctor/testing of an external interpreter.
    Execution callers must omit them to check their own process. This is not an
    import-time guard: version/configuration inspection stays available offline.
    """
    python_version = (
        tuple(sys.version_info[:3]) if python_version is None else python_version
    )
    platform_name = sys.platform if platform_name is None else platform_name
    implementation = (
        sys.implementation.name if implementation is None else implementation
    )
    if platform_name != "linux":
        raise PreflightError("unsupported_platform", operation=operation)
    if python_version[:2] != (3, 12) or implementation != "cpython":
        raise PreflightError("unsupported_python", operation=operation)
    try:
        compatible = version("harbor") == "0.22.0" and version("modal") == "1.5.4"
    except PackageNotFoundError:
        compatible = False
    if not compatible:
        raise PreflightError("runtime_dependency_mismatch", operation=operation)
    return {
        "status": "ok",
        "platform": "linux",
        "python_version": CONTROLLER_PYTHON_VERSION,
        "implementation": "cpython",
        "controller_python_version": CONTROLLER_PYTHON_VERSION,
        "harbor_version": "0.22.0",
        "modal_version": "1.5.4",
    }
