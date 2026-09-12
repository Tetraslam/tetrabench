"""Synthetic native processes exercise custody without any provider connection."""

from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager

import pytest

from tetrabench.auth import auth_session
from tetrabench.auth_config import AuthSpec, NativeAuthReference
from tetrabench.auth_operations import CliLifetime, cli_operation_lifetime
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthError,
    AuthStateError,
    LocalSessionStore,
    claim_session,
    read_private,
    seed_session,
    write_private,
)
from tetrabench.native_control import ControlError, ControlProcess
from tetrabench.native_refresh import CodexRefreshProtocol, access_token


def native(harness):
    if harness == "codex":
        payload = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "exp": 4102444800,
                        "email": "synthetic@example.invalid",
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "SYNTHETIC_ACCOUNT",
                            "chatgpt_plan_type": "plus",
                        },
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        return json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": "e30." + payload + ".SYNTHETIC_OLD",
                    "id_token": "e30." + payload + ".SYNTHETIC_ID",
                    "refresh_token": "SYNTHETIC_REFRESH",
                    "account_id": "SYNTHETIC_ACCOUNT",
                },
            }
        ).encode()
    return json.dumps(
        {
            "openai" if harness == "opencode" else "openai-codex": {
                "type": "oauth",
                "access": "SYNTHETIC_OLD",
                "refresh": "SYNTHETIC_REFRESH",
                "expires": 1,
                "accountId": "SYNTHETIC_ACCOUNT",
            }
        }
    ).encode()


STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
args = sys.argv[1:]
codex = "CODEX_HOME" in os.environ
pi = "PI_CODING_AGENT_DIR" in os.environ
if args == ["--version"]:
    print("codex-cli 0.154.0" if codex else "1.18.30")
    sys.exit(0)
if codex or pi:
    home = os.environ["CODEX_HOME" if codex else "PI_CODING_AGENT_DIR"]
    path = pathlib.Path(home) / "auth.json"
else:
    path = pathlib.Path(os.environ["XDG_DATA_HOME"]) / "opencode/auth.json"
if ((codex and args[-2:] == ["login", "status"])
    or (pi and args[-1] == "status")
    or (not codex and not pi and "run" not in args)):
    if codex: print("Logged in using ChatGPT", file=sys.stderr)
    elif pi: print('{"type":"oauth"}')
    else: print("OpenAI oauth")
    sys.exit(0)
mode = pathlib.Path("mode").read_text()
def reply(i, result):
    print(json.dumps({"id": i, "result": result}), flush=True)
if codex:
    assert args == ["-c", 'cli_auth_credentials_store="file"', "app-server"]
    assert json.loads(sys.stdin.readline())["method"] == "initialize"
    reply(1, {"userAgent": "synthetic"})
    assert json.loads(sys.stdin.readline())["method"] == "initialized"
    request = json.loads(sys.stdin.readline())
    assert request == {
        "id": 2, "method": "account/read", "params": {"refreshToken": True}
    }
    if mode == "protocol-error":
        print('{"id":2,"error":{"message":"SYNTHETIC_SECRET"}}', flush=True)
        sys.exit(0)
if mode == "interrupted":
    time.sleep(30)  # Server rotation could precede the native file write.
value = json.loads(path.read_bytes())
if mode != "unchanged":
    if codex:
        value["tokens"]["access_token"] += "_RENEWED"
        value["tokens"]["refresh_token"] += "_RENEWED"
    else:
        record = value["openai-codex" if pi else "openai"]
        record["access"] += "_RENEWED"
        record["refresh"] += "_RENEWED"
        record["expires"] = 4102444800000
    path.write_text(json.dumps(value))
if mode == "nonzero":
    print("SYNTHETIC_SECRET_ERROR", file=sys.stderr)
    sys.exit(1)
if mode == "killed": os.kill(os.getpid(), signal.SIGKILL)
if mode == "hang": time.sleep(30)
if mode == "orphan":
    if os.fork() == 0:
        os.close(0); os.close(1); os.close(2)
        time.sleep(30)
        os._exit(0)
if mode in {"setsid", "double-fork", "late-double-fork", "orphan-natural"}:
    if os.fork() == 0:
        os.setsid()
        if mode == "late-double-fork": time.sleep(0.15)
        if mode in {"double-fork", "late-double-fork"} and os.fork() != 0:
            os._exit(0)
        credential = os.open(path, os.O_RDONLY)
        for fd in (0, 1, 2): os.close(fd)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        evidence = {
            "host_pid": int(os.readlink("/proc/self")),
            "credential_readable": bool(os.read(credential, 4096)),
        }
        pathlib.Path("descendant.json").write_text(json.dumps(evidence))
        if mode == "orphan-natural":
            time.sleep(0.15)
            pathlib.Path("descendant-done").touch()
            os._exit(0)
        while True: time.sleep(30)
if codex:
    reply(2, {"account": {"type": "chatgpt", "email": "synthetic@example.invalid"}})
    assert sys.stdin.read() == ""
    if mode == "after-response-hang": time.sleep(30)
"""


@pytest.fixture
def setup(tmp_path):
    executable = tmp_path / "native-stub"
    executable.write_text(STUB)
    executable.chmod(0o700)
    package = tmp_path / "pi-package"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@earendil-works/pi-coding-agent",
                "version": "0.85.1",
            }
        )
    )
    (package / "dist/index.js").write_text("")

    @contextmanager
    def session(harness, mode="ok", store=None):
        store = store or LocalSessionStore(
            tmp_path / "authority", binding="test", local_filesystem=True
        )
        ref = NativeAuthReference(profile=harness, binding="test", generation=1)
        seed_session(store, ref, harness, native(harness))
        parent = tmp_path / "runtime"
        with cli_operation_lifetime(parent, operation="native-metadata") as owner:
            with auth_session(
                harness,
                AuthSpec(mode="chatgpt_oauth", reference=ref),
                executable=str(executable),
                runtime_parent=parent,
                environment={"PATH": os.defpath},
                artifact_roots=[],
                store=store,
                pi_module=package / "dist/index.js",
                consumer_id=owner,
                model="openai-codex/gpt-5" if harness == "pi" else "openai/gpt-5",
            ) as runtime:
                write_private(runtime.root / "mode", mode.encode())
                yield runtime, store, ref

    return session


@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
def test_tracked_refresh_checkpoint_release_and_fresh_claim(setup, harness):
    with setup(harness) as (runtime, store, ref):
        with pytest.raises(AuthBusyError):
            claim_session(store, ref, harness)
        before = read_private(runtime.credential_path)
        metadata = runtime.refresh(prompt="Reply OK without tools.")
        assert metadata.expires_at_ms == 4102444800000
        after = read_private(runtime.credential_path)
        assert access_token(harness, before) != access_token(harness, after)
        assert store.read(ref.profile).state.phase == "claimed"
        assert store.read(ref.profile).state.native == after
        assert store.read(ref.profile).state.revision == 1
        owner = runtime.claim.snapshot.state.owner
        evidence = runtime.root.parent / "operations" / (owner + ".json")
        record = CliLifetime.model_validate_json(read_private(evidence))
        assert record.children[-1].reaped and record.children[-1].return_code == 0
        assert len(record.children) == (3 if harness == "pi" else 4)
    state = store.read(ref.profile).state
    assert state.phase == "ready" and state.owner is None and state.revision == 2
    successor = claim_session(store, ref, harness)
    assert successor.snapshot.state.native == after
    successor.finish(after, consumer_stopped=True)


@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
@pytest.mark.parametrize(
    "mode", ["nonzero", "unchanged", "killed", "hang", "interrupted", "orphan"]
)
def test_inconclusive_refresh_blocks_even_when_caller_catches(setup, harness, mode):
    with pytest.raises(AuthStateError, match="ambiguous"):
        with setup(harness, mode) as (runtime, store, ref):
            with pytest.raises(AuthError):
                runtime.refresh(timeout=0.6, prompt="Reply OK without tools.")
            assert runtime._ambiguous
            with pytest.raises(AuthError):
                runtime.run([runtime.executable, "--version"])
            with pytest.raises(AuthError):
                runtime.handoff()
            after = read_private(runtime.credential_path)
    state = store.read(ref.profile).state
    assert state.phase == "claimed" and state.owner is not None
    if runtime._stopped:
        assert state.native == after
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, harness)


@pytest.mark.parametrize("mode", ["protocol-error", "after-response-hang"])
def test_codex_response_is_not_completion(setup, mode):
    with pytest.raises(AuthStateError, match="ambiguous"):
        with setup("codex", mode) as (runtime, _, _):
            runtime.refresh(timeout=0.6)


@pytest.mark.parametrize("phase", ["claimed", "ready"])
@pytest.mark.parametrize("committed", [False, True])
def test_cas_lost_reply_never_replays_and_ready_is_not_claimed_blocked(
    setup,
    phase,
    committed,
    monkeypatch,
):
    writes = []
    with pytest.raises(AuthError) as error:
        with setup("codex") as (runtime, store, ref):
            original = store.compare_and_swap

            def lose(profile, version, state):
                writes.append(state.phase)
                if state.phase == phase:
                    if committed:
                        original(profile, version, state)
                    raise AuthStateError("SYNTHETIC_LOST_REPLY")
                return original(profile, version, state)

            monkeypatch.setattr(store, "compare_and_swap", lose)
            runtime.refresh()
    assert writes == (["claimed"] if phase == "claimed" else ["claimed", "ready"])
    assert runtime.claim.closed
    state = store.read(ref.profile).state
    assert state.phase == ("ready" if phase == "ready" and committed else "claimed")
    if phase == "ready":
        assert "may already be ready" in str(error.value)
        assert "without replay" in str(error.value)
    with pytest.raises(AuthError):
        runtime.claim.finish(native("codex"), consumer_stopped=True)
    assert len(writes) == (1 if phase == "claimed" else 2)


def test_metadata_guard_still_refuses_refresh():
    metadata = ControlProcess.__new__(ControlProcess)
    with pytest.raises(ControlError, match="disable token refresh"):
        metadata.rpc("account/read", {"refreshToken": True}, 1)


def test_fixed_protocol_fragmentation_and_no_provider_error_output():
    protocol = CodexRefreshProtocol()
    assert json.loads(protocol.start())["method"] == "initialize"
    assert protocol.receive(b'{"id":1,') == b""
    requests = protocol.receive(b'"result":{}}\n').splitlines()
    assert [json.loads(item)["method"] for item in requests] == [
        "initialized",
        "account/read",
    ]
    with pytest.raises(AuthError) as error:
        protocol.receive(b'{"id":2,"error":{"message":"SYNTHETIC_SECRET"}}\n')
    assert "SYNTHETIC" not in str(error.value)


def test_refresh_requires_lifecycle_owner_before_execution(setup, monkeypatch):
    with setup("codex") as (runtime, _, _):
        monkeypatch.setattr(
            "tetrabench.native_refresh.current_cli_operation", lambda: None
        )
        with pytest.raises(AuthError, match="owning CLI lifetime"):
            runtime.refresh()
        assert not runtime._ambiguous


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_refresh_rejects_unbounded_timeout_before_launch(setup, timeout):
    with setup("codex") as (runtime, _, _):
        with pytest.raises(AuthError, match="finite positive timeout"):
            runtime.refresh(timeout=timeout)
        assert not runtime._ambiguous


def test_unproven_stop_does_not_write_back(setup, monkeypatch):
    from tetrabench.nativeauth import NativeStopError

    with pytest.raises(AuthStateError, match="ambiguous"):
        with setup("codex") as (runtime, store, ref):
            from tetrabench.native_refresh import run_native

            def lost_stop(*args, **kwargs):
                if not kwargs.get("require_natural_completion"):
                    return run_native(*args, **kwargs)
                write_private(
                    runtime.credential_path,
                    native("codex").replace(b"SYNTHETIC_OLD", b"SYNTHETIC_NEW"),
                )
                raise NativeStopError("stop unproven")

            monkeypatch.setattr("tetrabench.native_refresh.run_native", lost_stop)
            with pytest.raises(NativeStopError):
                runtime.refresh()
            assert not runtime._stopped
    assert store.read(ref.profile).state.native == native("codex")


@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("mode", ["setsid", "double-fork", "late-double-fork"])
def test_namespace_kills_detached_closed_pipe_credential_holders(setup, harness, mode):
    with pytest.raises(AuthStateError, match="ambiguous"):
        with setup(harness, mode) as (runtime, store, ref):
            with pytest.raises(AuthError):
                runtime.refresh(timeout=0.6, prompt="Reply OK without tools.")
            evidence = json.loads(read_private(runtime.root / "descendant.json"))
            assert evidence["credential_readable"]
            assert runtime._stopped and runtime._ambiguous
            with pytest.raises(ProcessLookupError):
                os.kill(evidence["host_pid"], 0)
            after = read_private(runtime.credential_path)
    assert store.read(ref.profile).state.phase == "claimed"
    assert store.read(ref.profile).state.native == after
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, harness)


def test_namespace_waits_for_naturally_finishing_detached_orphan(setup):
    with setup("opencode", "orphan-natural") as (runtime, store, ref):
        runtime.refresh(timeout=3, prompt="Reply OK without tools.")
        assert (runtime.root / "descendant-done").exists()
        evidence = json.loads(read_private(runtime.root / "descendant.json"))
        with pytest.raises(ProcessLookupError):
            os.kill(evidence["host_pid"], 0)
    assert store.read(ref.profile).state.phase == "ready"


@pytest.mark.parametrize("unavailable", ["missing", "denied"])
def test_containment_preflight_failure_does_not_poison_unlaunched_renewal(
    setup, monkeypatch, unavailable
):
    import sys

    with setup("opencode") as (runtime, store, ref):
        before = read_private(runtime.credential_path)
        if unavailable == "missing":
            monkeypatch.setattr(
                "tetrabench.native_refresh.shutil.which", lambda *a, **k: None
            )
        else:
            monkeypatch.setattr(
                "tetrabench.native_refresh.containment_command",
                lambda _: [sys.executable, "-I", "-c", "raise SystemExit(1)"],
            )
        with pytest.raises(AuthError, match="containment"):
            runtime.refresh(prompt="Reply OK without tools.")
        assert runtime._stopped and not runtime._ambiguous
        assert read_private(runtime.credential_path) == before
    assert store.read(ref.profile).state.phase == "ready"
    assert store.read(ref.profile).state.native == before


def test_interrupted_wrapper_cannot_release_or_leave_detached_consumer(
    setup, monkeypatch
):
    import time

    clock = time.monotonic
    interrupted = False
    with pytest.raises(AuthStateError, match="ambiguous"):
        with setup("opencode", "double-fork") as (runtime, store, ref):
            marker = runtime.root / "descendant.json"

            def interrupt_after_detachment():
                nonlocal interrupted
                if marker.exists() and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt
                return clock()

            monkeypatch.setattr(
                "tetrabench.nativeauth.time.monotonic", interrupt_after_detachment
            )
            with pytest.raises(AuthError):
                runtime.refresh(timeout=5, prompt="Reply OK without tools.")
            assert interrupted and runtime._ambiguous and not runtime._stopped
            evidence = json.loads(read_private(marker))
    assert store.read(ref.profile).state.phase == "claimed"
    # Cancellation lost the init exit proof, so custody remains blocked. Check
    # the exact synthetic detached child independently, not a process-tree sweep.
    deadline = clock() + 2
    while True:
        try:
            os.kill(evidence["host_pid"], 0)
        except ProcessLookupError:
            break
        assert clock() < deadline, "detached child survived namespace teardown"
        time.sleep(0.01)


@pytest.mark.parametrize("expiry", [None, 1])
def test_changed_token_without_fresh_expiry_is_inconclusive(expiry):
    from tetrabench.native_refresh import require_renewed

    before = native("codex")
    after = json.loads(before)
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({} if expiry is None else {"exp": expiry}).encode()
        )
        .decode()
        .rstrip("=")
    )
    after["tokens"]["access_token"] = "e30." + payload + ".SYNTHETIC_NEW"
    with pytest.raises(AuthError, match="inconclusive"):
        require_renewed("codex", before, json.dumps(after).encode())
