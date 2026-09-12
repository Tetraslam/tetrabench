from __future__ import annotations

import asyncio
import importlib.util
import json
import multiprocessing
from pathlib import Path

import pytest
from test_native_refresh import setup as refresh_setup
from test_runtime_auth import FakeHarborEnvironment, native, native_trial, new_scope

from tetrabench.auth_sessions import (
    AuthError,
    AuthStateError,
    private_json,
    read_private,
)

setup = refresh_setup

_spec = importlib.util.spec_from_file_location(
    "native_refresh_proof", Path(__file__).parents[1] / "tools/native_refresh_proof.py"
)
assert _spec is not None and _spec.loader is not None
proof_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proof_module)
RenewalProof = proof_module.RenewalProof
SuccessorProof = proof_module.SuccessorProof


@pytest.mark.parametrize("harness", ["codex", "opencode", "pi"])
def test_proof_injection_only_unsigned_expiry_and_release(setup, harness):
    with setup(harness) as (runtime, store, ref):
        proof = RenewalProof(runtime)
        before = private_json(read_private(runtime.credential_path))
        if harness != "codex":
            proof.expire_unsigned_cache()
            expected = json.loads(json.dumps(before))
            expected["openai" if harness == "opencode" else "openai-codex"][
                "expires"
            ] = 1
            assert private_json(read_private(runtime.credential_path)) == expected
            assert store.read(ref.profile).state.phase == "claimed"
        else:
            with pytest.raises(AuthError, match="unsigned expiry"):
                proof.expire_unsigned_cache()
        proof.renew(prompt="Reply OK without tools.")
    assert proof.verify_release() >= 2
    assert proof.report() == {
        "renewed": True,
        "refresh_token_changed": True,
        "release_verified": True,
    }
    with pytest.raises(AuthError, match="single-use"):
        proof.renew()
    assert "SYNTHETIC" not in repr(proof) + json.dumps(proof.report())


def test_proof_injection_lost_checkpoint_stops_without_replay(setup, monkeypatch):
    writes = []
    with pytest.raises(AuthError):
        with setup("pi") as (runtime, store, ref):
            proof = RenewalProof(runtime)
            original = store.compare_and_swap

            def lost(*args):
                writes.append(True)
                original(*args)
                raise AuthStateError("lost response")

            monkeypatch.setattr(store, "compare_and_swap", lost)
            proof.expire_unsigned_cache()
    assert runtime._ambiguous and runtime.claim.closed
    assert writes == [True]
    assert store.read(ref.profile).state.phase == "claimed"
    assert proof.report()["renewed"] is False


def test_failed_proof_comparison_never_reports_renewed(setup, monkeypatch):
    with pytest.raises(AuthStateError, match="ambiguous"):
        with setup("codex") as (runtime, _, _):
            proof = RenewalProof(runtime)

            def inconclusive(*args):
                raise AuthError("inconclusive proof")

            monkeypatch.setattr(proof_module, "require_renewed", inconclusive)
            with pytest.raises(AuthError, match="inconclusive proof"):
                proof.renew()
            assert proof.report()["renewed"] is False


def test_proof_preserves_unlaunched_preflight_failure(setup, monkeypatch):
    with setup("opencode") as (runtime, store, ref):
        proof = RenewalProof(runtime)
        monkeypatch.setattr(
            "tetrabench.native_refresh.shutil.which", lambda *a, **k: None
        )
        with pytest.raises(AuthError, match="containment"):
            proof.renew(prompt="Reply OK without tools.")
        assert not runtime._ambiguous
        assert not proof.report()["renewed"]
    assert store.read(ref.profile).state.phase == "ready"


@pytest.mark.parametrize("committed", [False, True])
def test_proof_reconciles_release_by_read_only(setup, monkeypatch, committed):
    writes = []
    with pytest.raises(AuthError, match="may already be ready"):
        with setup("codex") as (runtime, store, _):
            proof = RenewalProof(runtime)
            proof.renew()
            original = store.compare_and_swap

            def lost(*args):
                writes.append(True)
                if committed:
                    original(*args)
                raise AuthStateError("lost response")

            monkeypatch.setattr(store, "compare_and_swap", lost)
    assert not proof.report()["release_verified"]
    if committed:
        assert proof.verify_release() == 2
    else:
        with pytest.raises(AuthError, match="ready authority"):
            proof.verify_release()
    assert writes == [True]


@pytest.mark.parametrize("corrupt", ["none", "scope", "readback", "revision"])
def test_successor_uses_actual_claim_and_staged_readback(
    tmp_path, corrupt, monkeypatch
):
    previous, store, ref = new_scope(tmp_path, "pi", engine="modal")
    previous.claim.finish(native("pi", "SYNTHETIC_RENEWED"), consumer_stopped=True)
    revision = store.read(ref.profile).state.revision
    if corrupt == "revision":
        with pytest.raises(AuthError, match="revision changed"):
            SuccessorProof(
                store,
                ref,
                "pi",
                released_revision=revision + 1,
                consumer_id="fc-SYNTHETIC-OWNER",
            )
        return
    proof = SuccessorProof(
        store, ref, "pi", released_revision=revision, consumer_id="fc-SYNTHETIC-OWNER"
    )
    scope, _, _ = new_scope(tmp_path, "pi", engine="modal")
    environment = FakeHarborEnvironment("pi")
    if corrupt == "scope":
        scope.native = native("pi", "SYNTHETIC_WRONG")
    if corrupt == "readback":

        async def wrong_download(source, target):
            target.write_bytes(native("pi", "SYNTHETIC_WRONG"))

        monkeypatch.setattr(environment, "download_file", wrong_download)
    with proof.observe():
        if corrupt == "none":
            asyncio.run(native_trial(scope, environment))
            scope.finalize(require_consumer=True)
            assert proof.report() == {"claimed_and_staged_identity_verified": True}
        else:
            with pytest.raises(AuthError):
                asyncio.run(native_trial(scope, environment))
            assert scope.poisoned
            assert not environment.delivered
            assert proof.report() == {"claimed_and_staged_identity_verified": False}
            asyncio.run(environment.stop(delete=True))
    assert "SYNTHETIC" not in repr(proof) + json.dumps(proof.report())
    assert not list(scope.directory.glob("tmp*/native-readback"))


def _independent_controller(root, revision, results):
    from tetrabench.auth_config import NativeAuthReference
    from tetrabench.auth_sessions import LocalSessionStore

    store = LocalSessionStore(
        root / "authority", binding="test-runtime", local_filesystem=True
    )
    ref = NativeAuthReference(profile="profile", generation=1, binding="test-runtime")
    proof = SuccessorProof(
        store,
        ref,
        "codex",
        released_revision=revision,
        consumer_id="fc-SYNTHETIC-OWNER",
    )
    with proof.observe():
        scope, _, _ = new_scope(root, "codex", engine="modal")
        asyncio.run(native_trial(scope, FakeHarborEnvironment("codex")))
        scope.finalize(require_consumer=True)
    results.put(proof.report())


def test_fresh_spawned_controller_receives_only_revision_not_credentials(tmp_path):
    previous, store, ref = new_scope(tmp_path, "codex", engine="modal")
    previous.claim.finish(native("codex", "SYNTHETIC_RENEWED"), consumer_stopped=True)
    revision = store.read(ref.profile).state.revision
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_independent_controller, args=(tmp_path, revision, results)
    )
    process.start()
    try:
        assert results.get(timeout=20) == {"claimed_and_staged_identity_verified": True}
    finally:
        process.join(timeout=20)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    assert process.exitcode == 0
    assert store.read(ref.profile).state.phase == "ready"
