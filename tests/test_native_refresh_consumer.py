"""Pinned Codex protocol with synthetic auth in a network-isolated namespace."""

from __future__ import annotations

import os

import pytest
from native_consumer_support import native_modules
from test_native_refresh import native

from tetrabench.auth_operations import cli_operation_lifetime
from tetrabench.auth_sessions import (
    AuthError,
    private_directory,
    read_private,
    write_private,
)
from tetrabench.native_refresh import require_renewed
from tetrabench.nativeauth import (
    isolated_auth_environment,
    native_auth_path,
    run_native,
)


@pytest.mark.native
def test_pinned_codex_refresh_protocol_offline_is_not_renewal(tmp_path):
    modules = native_modules(required=True)
    assert modules is not None
    root = private_directory(tmp_path / "native", create=True)
    environment = isolated_auth_environment(
        "codex", root, base={"PATH": os.environ["PATH"]}
    )
    path = native_auth_path("codex", root)
    before = native("codex")
    write_private(path, before)
    argv = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        str(modules / ".bin/codex"),
        "-c",
        'cli_auth_credentials_store="file"',
        "app-server",
    ]
    # No endpoint override, no HTTP test double, and no egress even with synthetic auth.
    with cli_operation_lifetime(tmp_path / "lifetime", operation="native-metadata"):
        result = run_native(
            argv,
            environment=environment,
            cwd=root,
            codex_refresh=True,
            require_natural_completion=True,
            timeout=45,
        )
    assert result.returncode == 0
    assert read_private(path) == before
    with pytest.raises(AuthError, match="inconclusive"):
        require_renewed("codex", before, read_private(path))
