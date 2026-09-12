from __future__ import annotations

import json
import multiprocessing
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from test_nativeauth import native_bytes

from tetrabench.auth_bootstrap import bootstrap_profile, config_lock
from tetrabench.auth_config import NATIVE_AUTH_PINS
from tetrabench.auth_profiles import (
    S3AuthBackend,
    auth_profile_command,
    load_auth_config_file,
    onboard_login,
    profile_store,
    resolve_profile_reference,
)
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    LocalSessionStore,
    claim_session,
    private_directory,
    read_private,
    write_private,
)
from tetrabench.nativeauth import NativeResult, verify_native_version


@pytest.fixture
def onboarding(tmp_path, monkeypatch):
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": os.defpath,
    }
    config = tmp_path / "config/tetrabench/auth.toml"
    module = tmp_path / "pi/dist/index.js"
    module.parent.mkdir(parents=True)
    module.write_text("")
    (module.parent.parent / "package.json").write_text(
        json.dumps(
            {
                "name": "@earendil-works/pi-coding-agent",
                "version": NATIVE_AUTH_PINS["pi"],
            }
        )
    )
    events = []
    controls: dict[str, Any] = {"fail": False, "version": None, "during_login": None}

    def native(argv, *, environment, cwd, **kwargs):
        harness = (
            "codex"
            if "CODEX_HOME" in environment
            else "pi"
            if "PI_CODING_AGENT_DIR" in environment
            else "opencode"
        )
        if argv[-1] == "--version":
            events.append("version")
            version = controls["version"] or (
                "v24.21.0" if harness == "pi" else NATIVE_AUTH_PINS[harness]
            )
            return NativeResult(0, version.encode())
        path = cwd / (
            "data/opencode/auth.json" if harness == "opencode" else "native/auth.json"
        )
        if "logout" in argv:
            events.append("logout")
            path.unlink()
            return NativeResult(0)
        if "login" in argv and "status" not in argv:
            events.append("login")
            assert kwargs["interactive"] is True
            assert not any(key.startswith("TETRABENCH_AUTH_") for key in environment)
            if controls["during_login"]:
                controls["during_login"]()
            if controls["fail"]:
                return NativeResult(1, b"SYNTHETIC_PRIVATE_ERROR")
            write_private(path, native_bytes(harness, "SYNTHETIC_" + uuid.uuid4().hex))
            return NativeResult(0)
        events.append("status")
        return NativeResult(
            0,
            {
                "codex": b"Logged in using ChatGPT",
                "opencode": b"OpenAI oauth",
                "pi": b'{"type":"oauth"}',
            }[harness],
        )

    monkeypatch.setattr("tetrabench.auth.run_native", native)
    monkeypatch.setattr("tetrabench.nativeauth.run_native", native)

    def login(name="eval", agent=None, **kwargs):
        return onboard_login(
            name,
            agent=agent,
            config_path=config,
            environment=env,
            executable="synthetic-native",
            pi_module=module,
            **kwargs,
        )

    def authority(name="eval"):
        profile = load_auth_config_file(config, environment=env).profiles[name]
        return profile_store(
            profile, engine="docker", environment=env, artifact_buckets=[]
        )

    return login, config, env, controls, events, authority


@pytest.mark.parametrize("agent", ["codex", "opencode", "pi"])
def test_one_call_bootstrap_and_managed_logout_relogin(onboarding, agent):
    login, config, env, _, events, authority = onboarding
    result = login(agent=agent)
    assert result.state == "ready" and result.generation == 1
    original_config = read_private(config)
    profile = load_auth_config_file(config, environment=env).profiles["eval"]
    assert profile.generation is None and b"generation" not in original_config
    assert profile.backend.kind == "local"
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700
    old = resolve_profile_reference(
        "eval", harness=agent, config_path=config, environment=env
    )
    assert old.generation == 1
    before = events.count("login")
    with pytest.raises(AuthError, match="--replace"):
        login()
    assert events.count("login") == before
    auth_profile_command(
        "logout",
        name="eval",
        executable="synthetic-native",
        config_path=config,
        environment=env,
        pi_module=config.parents[2] / "pi/dist/index.js",
    )
    assert authority().read("eval").state.phase == "logged_out"
    assert login().generation == 2
    assert read_private(config) == original_config
    current = resolve_profile_reference("eval", config_path=config, environment=env)
    assert current.generation == 2 and current.binding == old.binding
    assert old.generation == 1
    with pytest.raises(AuthError, match="stale"):
        claim_session(authority(), old, agent)
    assert login(replace=True).generation == 3
    assert read_private(config) == original_config


def test_failed_first_login_leaves_uninitialized_profile(onboarding):
    login, config, env, controls, _, authority = onboarding
    controls["fail"] = True
    with pytest.raises(AuthError, match="no credentials published") as error:
        login(agent="codex")
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
    assert config.is_file() and authority().read("eval") is None
    status = auth_profile_command(
        "status", name="eval", executable="unused", config_path=config, environment=env
    )
    assert status.state == "absent"
    with pytest.raises(AuthError, match="not ready"):
        resolve_profile_reference("eval", config_path=config, environment=env)
    controls["fail"] = False
    assert login().generation == 1


def test_failed_replace_preserves_old_ready_generation(onboarding):
    login, _, _, controls, _, authority = onboarding
    login(agent="codex")
    before = authority().read("eval")
    controls["fail"] = True
    with pytest.raises(AuthError, match="no credentials published"):
        login(replace=True)
    after = authority().read("eval")
    assert after.version == before.version
    assert after.state.native == before.state.native
    assert after.state.phase == "ready" and after.state.generation == 1


def test_read_only_selection_never_creates_missing_authority(onboarding):
    login, config, env, controls, _, _ = onboarding
    controls["fail"] = True
    with pytest.raises(AuthError):
        login(agent="codex")
    profile = load_auth_config_file(config, environment=env).profiles["eval"]
    assert profile.backend.kind == "local"
    state_root = Path(profile.backend.state_directory)
    (state_root / ".lock").unlink()
    with pytest.raises(AuthError, match="not ready"):
        resolve_profile_reference("eval", config_path=config, environment=env)
    assert list(state_root.iterdir()) == []


def test_seed_commit_lost_reply_never_retries_or_claims_uninitialized(
    onboarding, monkeypatch
):
    login, _, _, _, events, authority = onboarding
    original = profile_store
    writes = []

    def store_with_lost_reply(*args, **kwargs):
        store = original(*args, **kwargs)
        cas = store.compare_and_swap

        def lost(*args):
            writes.append(True)
            cas(*args)
            raise AuthError("synthetic lost seed reply")

        monkeypatch.setattr(store, "compare_and_swap", lost)
        return store

    monkeypatch.setattr("tetrabench.auth_profiles.profile_store", store_with_lost_reply)
    with pytest.raises(AuthError, match="lost seed reply"):
        login(agent="codex")
    assert writes == [True] and events.count("login") == 1
    assert authority().read("eval").state.phase == "ready"
    with pytest.raises(AuthError, match="--replace"):
        login()
    assert writes == [True] and events.count("login") == 1


def test_prerequisite_failure_precedes_any_durable_profile_or_backend(
    onboarding, monkeypatch
):
    login, config, env, controls, events, _ = onboarding
    controls["version"] = "9.99.0"
    monkeypatch.setattr(
        "tetrabench.auth_profiles.profile_store",
        lambda *a, **k: pytest.fail("backend constructed before prerequisite check"),
    )
    with pytest.raises(AuthError, match=r"npm install -g @openai/codex@0\.154\.0"):
        login(agent="codex")
    assert not config.exists()
    assert not os.path.exists(env["XDG_STATE_HOME"])
    assert events == ["version"]


def test_no_lock_over_browser_and_preserve_other_profiles_and_comments(onboarding):
    login, config, env, controls, _, _ = onboarding
    login("first", agent="codex")
    write_private(config, b"# KEEP THIS OPERATOR COMMENT\n" + read_private(config))
    before = load_auth_config_file(config, environment=env).profiles["first"]

    def other_writer():
        bootstrap_profile(config, "other", "pi", None, env)

    controls["during_login"] = other_writer
    login("second", agent="opencode")
    loaded = load_auth_config_file(config, environment=env)
    assert set(loaded.profiles) == {"first", "other", "second"}
    assert loaded.profiles["first"] == before
    assert len({profile.binding for profile in loaded.profiles.values()}) == 3
    assert read_private(config).startswith(b"# KEEP THIS OPERATOR COMMENT\n")


@pytest.mark.parametrize("existing_ready", [False, True])
def test_simultaneous_native_seed_has_one_winner_without_generation_retry(
    onboarding, existing_ready
):
    login, config, env, controls, events, authority = onboarding
    if existing_ready:
        login(agent="codex")
    barrier = threading.Barrier(2)
    controls["during_login"] = lambda: barrier.wait(timeout=5)

    def attempt():
        try:
            return login(agent="codex", replace=existing_ready).state
        except AuthError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["ready", "refused"]
    assert events.count("login") == 2 + existing_ready
    assert authority().read("eval").state.generation == 1 + existing_ready
    assert len(load_auth_config_file(config, environment=env).profiles) == 1


def _write_profile(config, env, name, start, results):
    start.wait()
    try:
        bootstrap_profile(config, name, "codex", None, env)
        results.put("written")
    except AuthError:
        results.put("refused")


def test_simultaneous_process_config_writers_preserve_both(onboarding):
    _, config, env, _, _, _ = onboarding
    context = multiprocessing.get_context("fork")
    start, results = context.Event(), context.Queue()
    children = [
        context.Process(target=_write_profile, args=(config, env, name, start, results))
        for name in ("one", "two")
    ]
    for child in children:
        child.start()
    start.set()
    try:
        assert [results.get(timeout=10) for _ in children] == ["written", "written"]
    finally:
        for child in children:
            child.join(timeout=10)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
    assert all(child.exitcode == 0 for child in children)
    assert set(load_auth_config_file(config, environment=env).profiles) == {
        "one",
        "two",
    }


def test_existing_profile_cannot_rebind_even_with_replace(onboarding):
    login, config, _, _, events, _ = onboarding
    login(agent="codex")
    before = read_private(config)
    with pytest.raises(AuthError, match="cannot be rebound"):
        login(agent="opencode", replace=True)
    assert read_private(config) == before and events.count("login") == 1


def test_claimed_login_refused_reseed_requires_stopped_evidence(
    onboarding, monkeypatch
):
    login, config, env, _, events, authority = onboarding
    login(agent="codex")
    ref = resolve_profile_reference("eval", config_path=config, environment=env)
    old = claim_session(authority(), ref, "codex", consumer_id="controller-synthetic")
    with pytest.raises(AuthBusyError, match="explicit reseed"):
        login(replace=True)
    assert events.count("login") == 1
    monkeypatch.setattr(
        "tetrabench.auth_recovery.prove_previous_consumer_stopped",
        lambda *a, **k: False,
    )
    options: dict[str, Any] = dict(
        name="eval", executable="synthetic-native", config_path=config, environment=env
    )
    with pytest.raises(AuthBusyError, match="not proven stopped"):
        auth_profile_command("reseed", **options)
    assert events.count("login") == 1
    monkeypatch.setattr(
        "tetrabench.auth_recovery.prove_previous_consumer_stopped", lambda *a, **k: True
    )
    assert auth_profile_command("reseed", **options).generation == 2
    with pytest.raises(AuthBusyError):
        old.finish(native_bytes(), consumer_stopped=True)


def test_fixed_profiles_remain_fixed_and_unchanged(onboarding):
    login, config, env, _, _, authority = onboarding
    login(agent="codex")
    raw = read_private(config).replace(
        b"[profiles.eval]\n", b"[profiles.eval]\ngeneration = 1\n"
    )
    write_private(config, raw)
    selected = resolve_profile_reference("eval", config_path=config, environment=env)
    assert selected.generation == 1
    with pytest.raises(AuthError, match="omit generation"):
        login(replace=True)
    assert read_private(config) == raw
    assert authority().read("eval").state.generation == 1


@pytest.mark.parametrize(
    "unsafe", ["symlink", "file-mode", "parent-mode", "lock-symlink"]
)
def test_unsafe_private_config_is_not_repaired_or_overwritten(onboarding, unsafe):
    login, config, _, _, events, _ = onboarding
    login(agent="codex")
    before = read_private(config)
    if unsafe == "symlink":
        saved = config.with_name("saved.toml")
        config.rename(saved)
        config.symlink_to(saved)
    elif unsafe == "file-mode":
        config.chmod(0o644)
    elif unsafe == "parent-mode":
        config.parent.chmod(0o777)
    else:
        lock = config.with_name(config.name + ".lock")
        lock.unlink()
        lock.symlink_to(config)
    with pytest.raises(AuthError):
        login("second", agent="pi")
    assert config.read_bytes() == before and events.count("login") == 1


def test_config_lock_has_bounded_wait(onboarding):
    _, config, _, _, _, _ = onboarding
    with config_lock(config):
        with pytest.raises(AuthBusyError, match="configuration is busy"):
            with config_lock(config, timeout=0.02):
                pytest.fail("conflicting lock acquired")


def test_local_selection_is_read_only_and_does_not_construct_client(
    onboarding, monkeypatch
):
    login, config, env, _, _, _ = onboarding
    login(agent="codex")

    def forbidden(*args, **kwargs):
        pytest.fail("selection mutated state or constructed a backend")

    monkeypatch.setattr("tetrabench.auth_profiles.profile_store", forbidden)
    monkeypatch.setattr("tetrabench.auth_sessions.write_private", forbidden)
    monkeypatch.setattr("tetrabench.auth.run_native", forbidden)
    selected = resolve_profile_reference("eval", config_path=config, environment=env)
    assert selected.generation == 1


def test_s3_selection_is_explicit_online_and_backend_refs_are_strict(
    onboarding, monkeypatch
):
    _, config, env, _, _, _ = onboarding
    backend = S3AuthBackend.model_validate(
        {
            "kind": "s3",
            "approved_private_backend": True,
            "storage": {"provider": "tigris", "bucket": "synthetic-private-auth"},
            "access_key": {"name": "TETRABENCH_AUTH_ID"},
            "secret_key": {"name": "TETRABENCH_AUTH_SECRET"},
        }
    )
    bootstrap_profile(config, "eval", "codex", backend, env)
    calls = []

    def constructed(*args, **kwargs):
        calls.append(True)
        raise AuthError("synthetic online boundary")

    monkeypatch.setattr("tetrabench.auth_profiles.profile_store", constructed)
    with pytest.raises(AuthError, match="explicit online"):
        resolve_profile_reference("eval", config_path=config, environment=env)
    assert not calls
    with pytest.raises(AuthError, match="synthetic online boundary"):
        resolve_profile_reference(
            "eval", config_path=config, environment=env, allow_online=True
        )
    assert calls == [True]
    with pytest.raises(ValueError, match="env refs"):
        S3AuthBackend.model_validate(
            backend.model_dump() | {"secret_key": {"name": "AWS_SECRET_ACCESS_KEY"}}
        )


@pytest.mark.parametrize("version", ["2.1.267", "2.1.269"])
def test_requested_claude_version_is_exact_not_any_accepted_pin(version):
    verify_native_version("claude-code", version.encode(), requested_version=version)
    other = "2.1.269" if version == "2.1.267" else "2.1.267"
    with pytest.raises(AuthError, match="verified version"):
        verify_native_version("claude-code", other.encode(), requested_version=version)


def test_backend_file_selects_only_declared_s3_without_provisioning(
    onboarding, monkeypatch
):
    login, config, env, _, events, _ = onboarding
    root = private_directory(config.parents[2] / "backend", create=True)
    backend_file = root / "s3.toml"
    write_private(
        backend_file,
        b"""kind = "s3"
approved_private_backend = true
access_key = { name = "TETRABENCH_AUTH_ID" }
secret_key = { name = "TETRABENCH_AUTH_SECRET" }
[storage]
provider = "tigris"
bucket = "synthetic-auth-only"
""",
    )
    selected = []

    def private_mock_authority(profile, **kwargs):
        assert profile.backend.kind == "s3"
        selected.append(profile.backend)
        return LocalSessionStore(
            root / "mock-s3", binding=profile.binding, local_filesystem=True
        )

    monkeypatch.setattr(
        "tetrabench.auth_profiles.profile_store", private_mock_authority
    )
    monkeypatch.setattr(
        "tetrabench.auth_profiles.boto3.client",
        lambda *a, **k: pytest.fail("provider provisioning or ambient client"),
    )
    assert login(agent="codex", backend_path=backend_file).state == "ready"
    assert selected and events[0] == "version"
    configured = load_auth_config_file(config, environment=env).profiles["eval"]
    assert configured.backend == selected[0]


def test_backend_rebind_refused_before_login(onboarding):
    login, config, _, _, events, _ = onboarding
    login(agent="codex")
    before = read_private(config)
    backend_file = config.with_name("s3.toml")
    write_private(
        backend_file,
        b"""kind = "s3"
approved_private_backend = true
access_key = { name = "TETRABENCH_AUTH_ID" }
secret_key = { name = "TETRABENCH_AUTH_SECRET" }
[storage]
provider = "tigris"
bucket = "synthetic-auth-only"
""",
    )
    with pytest.raises(AuthError, match="cannot be rebound"):
        login(backend_path=backend_file, replace=True)
    assert read_private(config) == before and events.count("login") == 1


def test_pi_default_resolution_handles_published_bundle_layout(onboarding, monkeypatch):
    from tetrabench.nativeauth import preflight_native_auth

    _, config, env, _, _, _ = onboarding
    package = config.parents[2] / "pi"
    binary = package / "dist/bundle/cli.js"
    binary.parent.mkdir()
    binary.write_text("// synthetic package entrypoint")
    monkeypatch.setattr(
        "tetrabench.nativeauth.shutil.which",
        lambda name, **kwargs: str(binary) if name == "pi" else "synthetic-node",
    )
    prerequisite = preflight_native_auth("pi", environment=env)
    assert prerequisite.pi_module == package / "dist/index.js"
    assert prerequisite.version == NATIVE_AUTH_PINS["pi"]
    assert not config.exists()


def test_runtime_omission_is_late_bound_not_submitter_home(monkeypatch):
    from tetrabench.auth_profiles import (
        parse_auth_config_file,
        profile_runtime_directory,
    )

    monkeypatch.setenv("HOME", "/synthetic-submitter")
    config = parse_auth_config_file(b'{"profiles":{}}', format="json")
    assert config.runtime_directory is None
    runtime = profile_runtime_directory(
        config, environment={"HOME": "/synthetic-consumer"}
    )
    assert str(runtime).startswith("/synthetic-consumer/")
    for path in ("relative", "/tmp/../unsafe"):
        with pytest.raises(AuthError):
            parse_auth_config_file(
                json.dumps({"profiles": {}, "runtime_directory": path}).encode(),
                format="json",
            )


def test_network_filesystem_rejected_before_config_or_browser(onboarding, monkeypatch):
    from types import SimpleNamespace

    login, config, _, _, events, _ = onboarding
    monkeypatch.setattr(
        "tetrabench.auth_bootstrap.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"nfs\n"),
    )
    with pytest.raises(AuthError, match="local Linux filesystem"):
        login(agent="codex")
    assert not config.exists() and events == ["version"]
