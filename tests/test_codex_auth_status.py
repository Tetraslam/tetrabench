"""Pinned Codex status grammar, not arbitrary substring extraction."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_runtime_auth import FakeHarborEnvironment, new_scope

from tetrabench.auth_sessions import AuthError
from tetrabench.nativeauth import NativeResult, parse_native_status

WARNING = (
    b"WARNING: proceeding, even though we could not create PATH aliases: "
    b'Refusing to create helper binaries under temporary dir "/SYNTHETIC_PRIVATE_TMP" '
    b'(codex_home: AbsolutePathBuf("/SYNTHETIC_PRIVATE_TMP/codex"))'
)
API = b"Logged in using an API key - SYNTHETI***I_KEY"
OAUTH = b"Logged in using ChatGPT"


@pytest.mark.parametrize("mode,line", [("api_key", API), ("chatgpt_oauth", OAUTH)])
def test_exact_pinned_warning_then_single_native_status(mode, line):
    result = NativeResult(0, stdout=b"", stderr=WARNING + b"\n" + line + b"\n")
    observed = parse_native_status("codex", result)
    assert observed.mode == mode and observed.validity == "present"
    assert "SYNTHETIC" not in repr(observed)
    assert "PRIVATE_TMP" not in repr(observed)


@pytest.mark.parametrize(
    "stderr",
    [
        WARNING,
        b"INFO: " + API,
        b'INFO: {"message":"' + API + b'"}',
        API + b" additional unrecognized content",
        b"Logged in using an API key - fake",
        b"Logged in using an API key",
        API + b"\n" + OAUTH,
        API + b"\n" + API,
        WARNING + b"\n" + WARNING + b"\n" + API,
        b"WARNING: arbitrary warning\n" + API,
        API + b"\n" + WARNING,
        b"\x0b" + API,
    ],
)
def test_unrecognized_duplicate_contradictory_or_fake_status_is_blocked(stderr):
    assert (
        parse_native_status("codex", NativeResult(0, stdout=b"", stderr=stderr)).mode
        == "unknown"
    )


@pytest.mark.parametrize(
    "stdout,stderr",
    [(API, b""), (API, OAUTH), (OAUTH, API), (b"fake information", API)],
)
def test_stdout_cannot_supply_or_override_codex_auth_status(stdout, stderr):
    assert (
        parse_native_status("codex", NativeResult(0, stdout=stdout, stderr=stderr)).mode
        == "unknown"
    )


def test_runtime_combines_channel_evidence_without_fallback_and_uses_safe_tmp(tmp_path):
    scope, _, _ = new_scope(tmp_path, "codex", mode="api_key")

    class Environment(FakeHarborEnvironment):
        def __init__(self):
            super().__init__("codex")
            self.conflict = False

        async def exec(self, command, env: dict[str, str] | None = None, **kwargs):
            if "login status" in command:
                assert env is not None
                assert env["CODEX_HOME"] != env["TMPDIR"]
                assert not env["CODEX_HOME"].startswith(env["TMPDIR"] + "/")
                return SimpleNamespace(
                    return_code=0,
                    stdout=OAUTH.decode() if self.conflict else "",
                    stderr=(WARNING + b"\n" + API + b"\n").decode(),
                )
            return await super().exec(command, env=env, **kwargs)

    async def run():
        environment = Environment()
        hook = scope.new_hook(scope.harness)
        await hook.bind(environment)
        effective = hook.execution_environment({})
        await hook._status(effective)
        assert hook.observed_auth_provenance()["observed"]["mode"] == "api_key"
        assert "SYNTHETIC" not in json.dumps(hook.observed_auth_provenance())
        environment.conflict = True
        with pytest.raises(AuthError, match="billing mode"):
            await hook._status(effective)

    asyncio.run(run())
