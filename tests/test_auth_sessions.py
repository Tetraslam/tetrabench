from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import replace

import pytest

from tetrabench.auth_config import NativeAuthReference
from tetrabench.auth_sessions import (
    AuthBusyError,
    AuthStateError,
    LocalSessionStore,
    SessionState,
    claim_session,
    private_directory,
    read_private,
    seed_session,
    write_private,
)


def native_bytes(marker="SYNTHETIC_ACCESS_ONE", harness="codex"):
    if harness == "codex":
        return json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": marker,
                    "refresh_token": "SYNTHETIC_REFRESH_ONLY",
                    "id_token": "SYNTHETIC_ID_ONLY",
                },
            }
        ).encode()
    provider = "openai" if harness == "opencode" else "openai-codex"
    return json.dumps(
        {
            provider: {
                "type": "oauth",
                "access": marker,
                "refresh": "SYNTHETIC_REFRESH_ONLY",
                "expires": 4000000000000,
            }
        }
    ).encode()


@pytest.fixture
def authority(tmp_path):
    store = LocalSessionStore(
        tmp_path / "authority", binding="local-test", local_filesystem=True
    )
    ref = NativeAuthReference(profile="test", generation=1, binding="local-test")
    seed_session(store, ref, "codex", native_bytes())
    return store, ref


def test_refresh_persists_for_future_consumer(authority):
    store, ref = authority
    claim = claim_session(store, ref, "codex")
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, "codex")
    refreshed = native_bytes("SYNTHETIC_ACCESS_TWO")
    claim.finish(refreshed, consumer_stopped=True)
    successor = claim_session(store, ref, "codex")
    assert successor.snapshot.state.native == refreshed
    assert successor.snapshot.state.revision == 1
    assert "SYNTHETIC" not in repr(successor)


def _compete(root, ready, results):
    store = LocalSessionStore(root, binding="local-test", local_filesystem=True)
    ref = NativeAuthReference(profile="test", generation=1, binding="local-test")
    ready.wait()
    try:
        claim_session(store, ref, "codex")
    except AuthBusyError:
        results.put("blocked")
    else:
        results.put("claimed")


def test_real_processes_only_one_consumer_and_crash_no_reuse(authority):
    store, ref = authority
    ctx = multiprocessing.get_context("fork")
    ready, results = ctx.Event(), ctx.Queue()
    processes = [
        ctx.Process(target=_compete, args=(store.root, ready, results))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    ready.set()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert outcomes.count("claimed") == 1
    assert outcomes.count("blocked") == 3
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, "codex")


def test_stale_generation_and_other_harness_rejected(authority):
    store, ref = authority
    claim = claim_session(store, ref, "codex")
    claim.logout(consumer_stopped=True)
    next_ref = ref.model_copy(update={"generation": 2})
    seed_session(
        store,
        next_ref,
        "codex",
        native_bytes("SYNTHETIC_NEW_LOGIN"),
        previous_generation=1,
    )
    with pytest.raises(AuthStateError, match="stale"):
        claim_session(store, ref, "codex")
    with pytest.raises(AuthStateError, match="harness"):
        claim_session(store, next_ref, "pi")
    with pytest.raises(AuthStateError, match="binding"):
        claim_session(store, next_ref.model_copy(update={"binding": "other"}), "codex")


def test_reseed_after_crash_requires_stop_proof_and_new_lineage(authority):
    store, ref = authority
    claimed = claim_session(store, ref, "codex")
    next_ref = ref.model_copy(update={"generation": 2})
    with pytest.raises(AuthBusyError, match="stopped"):
        seed_session(
            store,
            next_ref,
            "codex",
            native_bytes("SYNTHETIC_NEW_LOGIN"),
            previous_generation=1,
        )
    with pytest.raises(AuthStateError, match="fresh"):
        seed_session(
            store,
            next_ref,
            "codex",
            native_bytes(),
            previous_generation=1,
            stopped_owner=lambda _: True,
        )
    seed_session(
        store,
        next_ref,
        "codex",
        native_bytes("SYNTHETIC_NEW_LOGIN"),
        previous_generation=1,
        stopped_owner=lambda owner: owner == claimed.snapshot.state.owner,
    )
    with pytest.raises(AuthBusyError):
        claimed.finish(native_bytes("SYNTHETIC_STALE_WRITE"), consumer_stopped=True)
    assert claim_session(
        store, next_ref, "codex"
    ).snapshot.state.native == native_bytes("SYNTHETIC_NEW_LOGIN")


def test_lost_write_reply_never_replays_old_claim(authority, monkeypatch):
    store, ref = authority
    claim = claim_session(store, ref, "codex")
    original = store.compare_and_swap

    def lost_reply(*args):
        original(*args)
        raise AuthStateError("ambiguous write")

    monkeypatch.setattr(store, "compare_and_swap", lost_reply)
    refreshed = native_bytes("SYNTHETIC_REFRESHED")
    with pytest.raises(AuthStateError):
        claim.finish(refreshed, consumer_stopped=True)
    with pytest.raises(AuthStateError):
        claim.finish(native_bytes(), consumer_stopped=True)
    monkeypatch.setattr(store, "compare_and_swap", original)
    assert claim_session(store, ref, "codex").snapshot.state.native == refreshed


def test_ambiguous_refresh_preserves_bytes_but_blocks_reuse(authority):
    store, ref = authority
    claim = claim_session(store, ref, "codex")
    refreshed = native_bytes("SYNTHETIC_UNCERTAIN")
    claim.preserve_blocked(refreshed)
    assert store.read(ref.profile).state.native == refreshed
    with pytest.raises(AuthBusyError):
        claim_session(store, ref, "codex")


def test_file_modes_symlinks_hardlinks_and_size(tmp_path):
    root = private_directory(tmp_path / "private", create=True)
    path = root / "auth.json"
    write_private(path, b"{}")
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(AuthStateError, match="0600"):
        read_private(path)
    path.chmod(0o600)
    alias = root / "alias"
    alias.symlink_to(path)
    with pytest.raises(AuthStateError):
        read_private(alias)
    with pytest.raises(AuthStateError):
        write_private(alias, b"overwrite")
    os.link(path, root / "hardlink")
    with pytest.raises(AuthStateError, match="single-link"):
        read_private(path)
    with pytest.raises(AuthStateError, match="size"):
        write_private(root / "huge", bytes(200 * 1024))


def test_symlink_directory_and_distributed_filesystem_refused(tmp_path):
    real = private_directory(tmp_path / "real", create=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(AuthStateError):
        private_directory(alias / "child", create=True)
    with pytest.raises(AuthStateError, match="unproven"):
        LocalSessionStore(real, binding="modal-volume", local_filesystem=False)


def test_secret_state_codec_is_strict_and_not_repr(authority):
    store, ref = authority
    state = store.read(ref.profile).state
    assert SessionState.decode(state.encode()) == state
    assert "SYNTHETIC" not in repr(state)
    broken = replace(state, phase="claimed", owner=None)
    with pytest.raises(AuthStateError):
        SessionState.decode(broken.encode())
