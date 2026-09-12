from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tetrabench.auth_config import AuthSpec, EnvAuthReference, NativeAuthReference
from tetrabench.auth_diagnostics import (
    check_controller_metadata,
    check_native_prerequisites,
    doctor_auth_report,
)
from tetrabench.auth_sessions import (
    LocalSessionStore,
    SessionState,
    StateRead,
    write_private,
)
from tetrabench.harness_config import HarnessConfig
from tetrabench.native_discovery import NativeInstallation
from tetrabench.nativeauth import NativeResult

SENTINEL = "SYNTHETIC_SECRET_MUST_NEVER_APPEAR_731"


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail(
            "diagnostics attempted an unexpected provider/native/login operation"
        )

    monkeypatch.setattr("boto3.client", forbidden)
    monkeypatch.setattr("tetrabench.nativeauth.run_native", forbidden)
    monkeypatch.setattr("tetrabench.auth.auth_login", forbidden)
    monkeypatch.setattr("tetrabench.discovery_auth.metadata_auth_context", forbidden)


def harness(*, oauth=False):
    return HarnessConfig(
        name="codex",
        version="0.154.0",
        model="openai/gpt-5",
        auth=AuthSpec(
            mode="chatgpt_oauth" if oauth else "api_key",
            reference=(
                NativeAuthReference(profile="eval", binding="dedicated", generation=1)
                if oauth
                else EnvAuthReference(name="EVAL_KEY")
            ),
        ),
    )


def local_config(tmp_path):
    path = tmp_path / "auth.toml"
    write_private(
        path,
        f'''runtime_directory = "{tmp_path}/runtime"
[profiles.eval]
harness = "codex"
binding = "dedicated"
generation = 1
[profiles.eval.backend]
kind = "local"
approved_private_backend = true
state_directory = "{tmp_path}/state"
'''.encode(),
    )
    return path


def state(*, phase="ready", generation=1):
    return SessionState(
        profile="eval",
        harness="codex",
        binding="dedicated",
        generation=generation,
        revision=1,
        phase=phase,
        owner="cli-" + "a" * 32 if phase == "claimed" else None,
        native=(
            b""
            if phase == "logged_out"
            else json.dumps(
                {
                    "tokens": dict.fromkeys(
                        ("access_token", "refresh_token", "id_token"), SENTINEL
                    ),
                }
            ).encode()
        ),
    )


def test_offline_api_eval_does_not_need_native_or_claim_auth(monkeypatch, capsys):
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.check_native_prerequisites",
        lambda *a, **kw: pytest.fail("API eval needs no host CLI"),
    )
    result = doctor_auth_report(harness(), environment={"EVAL_KEY": SENTINEL})
    assert result["billing_mode"] == "api_key"
    assert result["reference"]["name"] == "EVAL_KEY"
    assert result["configured"]["status"] == "ok"
    assert result["native_ready"]["status"] == "not_required"
    assert result["provider_checked"]["status"] == "not_attempted"
    assert result["account_verified"]["status"] == "unproven"
    assert result["remote_runtime_checked"]["status"] == "not_attempted"
    assert "network" in result["task_guidance"]
    assert "time" in result["task_guidance"]
    assert SENTINEL not in json.dumps(result) + str(capsys.readouterr())


def test_missing_and_conflicting_names_not_values():
    result = doctor_auth_report(harness(), environment={"OPENAI_API_KEY": SENTINEL})
    assert result["configured"]["status"] == "blocked"
    assert result["missing_env_names"] == ["EVAL_KEY"]
    assert result["conflicting_env_names"] == ["OPENAI_API_KEY"]
    assert SENTINEL not in json.dumps(result)


def test_modal_submitter_environment_is_not_controller_evidence():
    result = doctor_auth_report(
        harness(), engine="modal", online=True, environment={"EVAL_KEY": SENTINEL}
    )
    assert "controller Secret values are unobserved" in result["credential_scope"]
    assert result["provider_checked"]["status"] == "not_attempted"
    assert result["remote_runtime_checked"]["status"] == "not_attempted"


@pytest.mark.parametrize(
    "phase,generation,expected",
    [("ready", 1, "ok"), ("claimed", 1, "blocked"), ("ready", 2, "blocked")],
)
def test_local_authority_state_and_stale_generation(
    tmp_path, phase, generation, expected
):
    path = local_config(tmp_path)
    store = LocalSessionStore(
        tmp_path / "state", binding="dedicated", local_filesystem=True
    )
    store.compare_and_swap("eval", None, state(phase=phase, generation=generation))
    result = doctor_auth_report(harness(oauth=True), auth_config=path, environment={})
    profile = result["auth_profile"]
    assert profile["source"] == "local auth authority"
    assert profile["backend"] == "local"
    assert profile["state"] == phase
    assert profile["observed_generation"] == generation
    assert profile["status"] == expected
    assert SENTINEL not in json.dumps(result)


def test_absent_local_profile_has_login_prerequisite_without_creating_store(
    tmp_path, monkeypatch
):
    path = local_config(tmp_path)
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.check_native_prerequisites",
        lambda *a, **kw: {
            "status": "blocked",
            "install_command": "npm install --global @openai/codex@0.154.0",
        },
    )
    result = doctor_auth_report(harness(oauth=True), auth_config=path, environment={})
    assert result["auth_profile"]["state"] == "absent"
    assert result["native_ready"]["status"] == "blocked"
    assert not (tmp_path / "state").exists()


def test_local_backend_refused_for_modal_and_parallel_oauth(tmp_path):
    result = doctor_auth_report(
        harness(oauth=True),
        engine="modal",
        concurrency=2,
        auth_config=local_config(tmp_path),
        environment={},
    )
    assert result["configured"]["status"] == "blocked"
    assert result["oauth_concurrency"]["status"] == "blocked"
    assert "private S3" in result["auth_profile"]["action"]


def test_auth_profile_is_not_run_profile_and_cannot_silently_replace_reference():
    result = doctor_auth_report(
        harness(oauth=True), auth_profile="other", environment={}
    )
    assert result["configured"]["status"] == "blocked"
    assert "auth_profile" not in result
    result = doctor_auth_report(
        harness(), auth_profile="eval", environment={"EVAL_KEY": SENTINEL}
    )
    assert result["configured"]["status"] == "blocked"


def s3_config(tmp_path):
    path = tmp_path / "auth.toml"
    write_private(
        path,
        f'''runtime_directory = "{tmp_path}/runtime"
[profiles.eval]
harness = "codex"
binding = "dedicated"
generation = 1
[profiles.eval.backend]
kind = "s3"
approved_private_backend = true
storage = {{provider="tigris", bucket="auth-only"}}
access_key = {{kind="env", name="TETRABENCH_AUTH_ACCESS_KEY"}}
secret_key = {{kind="env", name="TETRABENCH_AUTH_SECRET_KEY"}}
'''.encode(),
    )
    return path


def test_offline_s3_profile_never_constructs_provider_client(tmp_path):
    result = doctor_auth_report(
        harness(oauth=True),
        auth_config=s3_config(tmp_path),
        engine="modal",
        environment={},
    )
    assert result["auth_profile"]["backend"] == "s3"
    assert result["auth_profile"]["state"] == "unproven"
    assert result["auth_profile"]["missing_env_names"] == [
        "TETRABENCH_AUTH_ACCESS_KEY",
        "TETRABENCH_AUTH_SECRET_KEY",
    ]
    assert result["configured"]["status"] == "unproven"


def test_online_backend_checks_only_selected_authority(tmp_path, monkeypatch):
    calls = []

    def profile_store(profile, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(read=lambda name: StateRead(state(), "synthetic"))

    monkeypatch.setattr("tetrabench.auth_profiles.profile_store", profile_store)
    result = doctor_auth_report(
        harness(oauth=True),
        auth_config=s3_config(tmp_path),
        engine="modal",
        online=True,
        environment={
            "TETRABENCH_AUTH_ACCESS_KEY": SENTINEL,
            "TETRABENCH_AUTH_SECRET_KEY": SENTINEL,
        },
        artifact_buckets=["artifacts"],
    )
    assert len(calls) == 1
    assert calls[0]["artifact_buckets"] == ["artifacts"]
    assert result["auth_profile"]["status"] == "ok"
    assert "submitter credentials" in result["auth_profile"]["source"]
    assert result["provider_checked"]["status"] == "not_attempted"
    assert SENTINEL not in json.dumps(result)


def test_backend_error_does_not_disclose_provider_payload(
    tmp_path, monkeypatch, capsys
):
    def fail(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr("tetrabench.auth_profiles.profile_store", fail)
    result = doctor_auth_report(
        harness(oauth=True),
        auth_config=s3_config(tmp_path),
        online=True,
        environment={},
    )
    assert result["auth_profile"]["status"] == "blocked"
    assert SENTINEL not in json.dumps(result) + str(capsys.readouterr())


@pytest.mark.parametrize(
    "version,observed",
    [("2.1.269", "2.1.267"), ("2.1.267", "2.1.269"), ("2.1.269", "2.1.269")],
)
def test_native_exact_requested_pin_and_isolated_version_probe(
    monkeypatch, version, observed
):
    monkeypatch.setattr(
        "tetrabench.native_discovery.native_installation",
        lambda *a, **kw: NativeInstallation(("/test/claude",), "/test/node"),
    )
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.shutil.which", lambda name, **kw: "/test/" + name
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[-1] == "--version" and "--net" in argv
        assert "--pid" in argv and "--kill-child" in argv
        assert SENTINEL not in json.dumps(kwargs["environment"])
        assert kwargs["environment"]["HOME"] != "/interactive"
        assert kwargs["timeout"] == 15
        return NativeResult(0, f"{observed} (Claude Code)\n".encode())

    monkeypatch.setattr("tetrabench.nativeauth.run_native", run)
    result = check_native_prerequisites(
        "claude-code",
        version,
        environment={"HOME": "/interactive", "ANTHROPIC_API_KEY": SENTINEL},
    )
    assert result["observed_version"] == observed
    assert (
        result["install_command"]
        == f"npm install --global @anthropic-ai/claude-code@{version}"
    )
    assert result["status"] == ("ok" if version == observed else "blocked")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["output", "exception", "namespace"])
def test_native_failures_redact_secret_sentinel(monkeypatch, failure, capsys):
    monkeypatch.setattr(
        "tetrabench.native_discovery.native_installation",
        lambda *a, **kw: NativeInstallation(("/test/codex",), "/test/node"),
    )
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.shutil.which",
        lambda name, **kw: (
            None if failure == "namespace" and name == "unshare" else "/test/" + name
        ),
    )

    def run(*args, **kwargs):
        if failure == "exception":
            raise RuntimeError(SENTINEL)
        return NativeResult(0, f"0.154.0 {SENTINEL}".encode())

    monkeypatch.setattr("tetrabench.nativeauth.run_native", run)
    result = check_native_prerequisites("codex", "0.154.0", environment={})
    assert result["status"] == "unproven"
    assert result["observed_version"] is None
    assert SENTINEL not in json.dumps(result) + str(capsys.readouterr())


@pytest.mark.parametrize(
    "dist,node,expected",
    [
        (False, b"v24.21.0", "blocked"),
        (True, b"v22.0.0", "blocked"),
        (True, b"v22.18.9", "blocked"),
        (True, b"v22.19.0", "ok"),
        (True, b"v23.0.0", "ok"),
        (True, b"v24.21.0", "ok"),
        (True, b"v25.0.0", "ok"),
    ],
)
def test_pi_node_engine_requirement_and_published_package_dist(
    tmp_path, monkeypatch, dist, node, expected
):
    package = tmp_path / "pi"
    package.mkdir()
    if dist:
        (package / "dist").mkdir()
        (package / "dist/index.js").write_text("// synthetic SDK")
    monkeypatch.setattr(
        "tetrabench.native_discovery.native_installation",
        lambda *a, **kw: NativeInstallation(("/test/pi",), "/test/node", package),
    )
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.shutil.which", lambda name, **kw: "/test/" + name
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return NativeResult(0, node if argv[-2] == "/test/node" else b"0.85.1\n")

    monkeypatch.setattr("tetrabench.nativeauth.run_native", run)
    result = check_native_prerequisites(
        "pi", "0.85.1", purpose="metadata", environment={}
    )
    assert result["status"] == expected
    assert result["node_required"] == ">=22.19.0"
    assert result["package_dist"] == ("present" if dist else "missing")
    if expected != "ok":
        assert "Node >=22.19.0" in result["action"]
        assert "--pi-module" in result["action"]
        assert "--native-modules" not in result["action"]
    assert len(calls) == (0 if not dist else 1 if expected == "blocked" else 2)


@pytest.mark.parametrize("failure", [None, "enter", "collect", "exit", "secret"])
def test_provider_check_uses_existing_custody_and_never_verifies_catalog_account(
    monkeypatch, failure, capsys
):
    calls = []
    runtime = object()
    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.check_native_prerequisites",
        lambda *a, **kw: {"status": "ok"},
    )

    @contextmanager
    def context(config, **kwargs):
        assert kwargs["allow_authenticated_read"] is True
        assert kwargs["environment"] == {"EVAL_KEY": SENTINEL}
        calls.append("enter")
        if failure == "enter":
            raise RuntimeError(SENTINEL)
        try:
            yield runtime
        finally:
            calls.append("exit")
            if failure == "exit":
                raise RuntimeError(SENTINEL)

    def inspect(config, **kwargs):
        assert kwargs["runtime"] is runtime
        assert kwargs["refresh"] is False
        assert kwargs["reuse_native_cache"] is False
        assert kwargs["allow_config_execution"] is False
        calls.append("collect")
        if failure == "collect":
            raise RuntimeError(SENTINEL)
        return {
            "capability": {"status": "supported"},
            "private": SENTINEL if failure == "secret" else "bundled catalog",
        }

    def safe(rt, value):
        assert rt is runtime
        calls.append("guard")
        if SENTINEL in json.dumps(value):
            raise RuntimeError(SENTINEL)

    monkeypatch.setattr("tetrabench.discovery_auth.metadata_auth_context", context)
    monkeypatch.setattr("tetrabench.discovery_auth.assert_safe_metadata", safe)
    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler", inspect
    )
    result = doctor_auth_report(
        harness(), check_provider=True, environment={"EVAL_KEY": SENTINEL}
    )
    provider = result["provider_checked"]
    assert provider["status"] == ("unproven" if failure is None else "failed")
    assert provider["provider_metadata_fetch"] == "not-observed"
    assert provider["metadata_status"] == ("supported" if failure is None else "failed")
    assert "declared harness.auth only" in provider["auth_scope"]
    assert result["account_verified"]["status"] == "unproven"
    if failure != "enter":
        assert calls[-1] == "exit"
    assert SENTINEL not in json.dumps(result) + str(capsys.readouterr())


def test_provider_check_requires_explicit_auth_and_does_not_fall_back():
    config = harness().model_copy(update={"auth": None})
    result = doctor_auth_report(config, check_provider=True, environment={})
    assert result["configured"]["status"] == "blocked"
    assert result["provider_checked"]["status"] == "not_attempted"


@pytest.mark.parametrize(
    "name,version,model,mode",
    [
        ("codex", "0.154.0", "openai/gpt-5", "api_key"),
        ("opencode", "1.18.30", "openrouter/openai/gpt-5", "api_key"),
        ("pi", "0.85.1", "openrouter/openai/gpt-5", "api_key"),
        ("claude-code", "2.1.267", "anthropic/claude-opus-5", "api_key"),
        ("claude-code", "2.1.267", "anthropic/claude-opus-5", "claude_setup_token"),
    ],
)
def test_env_billing_modes_never_require_host_cli(name, version, model, mode):
    config = HarnessConfig(
        name=name,
        version=version,
        model=model,
        auth=AuthSpec(mode=mode, reference=EnvAuthReference(name="EVAL_KEY")),
    )
    result = doctor_auth_report(config, environment={"EVAL_KEY": SENTINEL})
    assert result["billing_mode"] == mode
    assert result["configured"]["status"] == "ok"
    assert result["native_ready"]["status"] == "not_required"
    assert result["account_verified"]["status"] == "unproven"


@pytest.mark.parametrize("status", ["unsupported", "unknown", "unavailable", SENTINEL])
def test_metadata_unknowns_are_not_success_or_serialized_payload(monkeypatch, status):
    @contextmanager
    def context(*args, **kwargs):
        yield object()

    monkeypatch.setattr(
        "tetrabench.auth_diagnostics.check_native_prerequisites",
        lambda *a, **kw: {"status": "ok"},
    )
    monkeypatch.setattr("tetrabench.discovery_auth.metadata_auth_context", context)
    monkeypatch.setattr(
        "tetrabench.discovery_auth.assert_safe_metadata", lambda *a: None
    )
    monkeypatch.setattr(
        "tetrabench.native_discovery.inspect_installed_handler",
        lambda *a, **kw: {"capability": {"status": status}},
    )
    result = doctor_auth_report(
        harness(), check_provider=True, environment={"EVAL_KEY": SENTINEL}
    )
    assert result["provider_checked"]["status"] == "unproven"
    assert result["provider_checked"]["metadata_status"] == (
        "not_attempted" if status == SENTINEL else status
    )
    assert SENTINEL not in json.dumps(result)


def test_controller_metadata_matches_pinned_public_sdk_without_network():
    import inspect

    import modal

    for resource in (modal.Secret, modal.Function):
        assert "environment_name" in inspect.signature(resource.from_name).parameters
        assert callable(resource.hydrate)
    assert (
        "create_if_missing" in inspect.signature(modal.Environment.from_name).parameters
    )


@pytest.mark.parametrize(
    "online,failure",
    [(False, None), (True, None), (True, "Secret"), (True, "Function")],
)
def test_controller_metadata_never_invokes_or_reads_secret_values(
    monkeypatch, online, failure, capsys
):
    from tetrabench.modal_app import ControllerDeploymentSpec

    calls = []
    spec = ControllerDeploymentSpec(
        profile="cloud",
        app_name="app",
        function_name="controller",
        environment_name="namespace",
        volume_name="volume",
        secret_name="secret",
    )
    monkeypatch.setattr(
        "tetrabench.modal_app.ensure_modal_environment",
        lambda *a, **kw: calls.append("Environment"),
    )

    def resource(name):
        def from_name(*args, **kwargs):
            assert kwargs["environment_name"] == "namespace"

            def hydrate():
                calls.append(name)
                if failure == name:
                    raise RuntimeError(SENTINEL)

            return SimpleNamespace(hydrate=hydrate)

        return SimpleNamespace(from_name=from_name)

    module = SimpleNamespace(Secret=resource("Secret"), Function=resource("Function"))
    result = check_controller_metadata(spec, online=online, modal_module=module)
    assert result["status"] == (
        "not_attempted" if not online else "blocked" if failure else "ok"
    )
    assert result["remote_runtime_checked"]["status"] == "unproven"
    assert result["credential_values_observed"] is False
    if not online:
        assert calls == []
    elif failure is None:
        assert calls == ["Environment", "Secret", "Function"]
    assert SENTINEL not in json.dumps(result) + str(capsys.readouterr())
