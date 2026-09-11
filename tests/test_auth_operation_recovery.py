from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from typing import Any

from test_nativeauth_s3 import FakePrivateS3, native, reference, store

from tetrabench.auth import auth_logout
from tetrabench.auth_config import AuthSpec
from tetrabench.auth_operations import (
    CliLifetime,
    NativeChild,
    prove_cli_operation_stopped,
)
from tetrabench.auth_profiles import auth_profile_command
from tetrabench.auth_sessions import (
    AuthError,
    read_private,
    seed_session,
    write_private,
)
from tetrabench.nativeauth import NativeResult

STUB = """#!/usr/bin/env python3
import sys
if "--version" in sys.argv: print("1.18.30")
elif "logout" in sys.argv: sys.exit(1)
else: print("OpenAI oauth")
"""


def failed_logout(client, executable, parent, ready, exit_owner):
    authority = store(client)
    try:
        auth_logout(
            "opencode",
            AuthSpec(mode="chatgpt_oauth", reference=reference()),
            store=authority,
            executable=str(executable),
            runtime_parent=parent,
            environment={"PATH": os.defpath},
            artifact_roots=[],
        )
    except AuthError:
        current = authority.read(reference().profile)
        ready.put(current.state.owner)
        exit_owner.wait(timeout=20)


def test_failed_s3_logout_reseed_uses_real_cli_lifetime_evidence(tmp_path, monkeypatch):
    ctx = multiprocessing.get_context("fork")
    with ctx.Manager() as manager:
        client: Any = FakePrivateS3()
        client.objects, client.writes = manager.dict(), manager.list()
        client.lock = ctx.Lock()
        authority = store(client)
        seed_session(authority, reference(), "opencode", native())
        executable = tmp_path / "native-stub"
        executable.write_text(STUB)
        executable.chmod(0o700)
        parent = tmp_path / "runtime"
        ready, exit_owner = ctx.Queue(), ctx.Event()
        worker = ctx.Process(
            target=failed_logout, args=(client, executable, parent, ready, exit_owner)
        )
        worker.start()
        try:
            owner = ready.get(timeout=10)
            assert owner.startswith("cli-logout-")
            assert not prove_cli_operation_stopped(owner, parent)
        finally:
            exit_owner.set()
            worker.join(timeout=10)
        assert worker.exitcode == 0
        assert prove_cli_operation_stopped(owner, parent)
        path = parent / "operations" / (owner + ".json")
        record = CliLifetime.model_validate_json(read_private(path))
        assert record.state == "ambiguous"
        assert len(record.children) == 3
        assert all(child.reaped for child in record.children)
        assert record.children[-1].return_code == 1

        # PID reuse and a crash in the spawn-registration gap are not expiry.
        original_identity = __import__(
            "tetrabench.auth_operations", fromlist=["process_identity"]
        ).process_identity
        with monkeypatch.context() as patch:
            patch.setattr(
                "tetrabench.auth_operations.process_identity",
                lambda pid: (
                    record.process.model_copy(update={"start_ticks": "different"})
                    if pid == record.process.pid
                    else original_identity(pid)
                ),
            )
            assert not prove_cli_operation_stopped(owner, parent)
        write_private(
            path,
            record.model_copy(update={"launching": True}).model_dump_json().encode(),
        )
        assert not prove_cli_operation_stopped(owner, parent)
        write_private(path, record.model_dump_json().encode())

        live_child = subprocess.Popen(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            child = NativeChild(identity=original_identity(live_child.pid))
            write_private(
                path,
                record.model_copy(update={"children": (*record.children, child)})
                .model_dump_json()
                .encode(),
            )
            assert not prove_cli_operation_stopped(owner, parent)
        finally:
            live_child.terminate()
            live_child.wait(timeout=5)
            write_private(path, record.model_dump_json().encode())

        config_dir = tmp_path / "config"
        config_dir.mkdir(mode=0o700)
        config = config_dir / "auth.toml"
        config.write_text(f'''schema_version=1
runtime_directory="{parent}"
[profiles.openai-eval]
harness="opencode"
binding="dedicated-controller"
generation=2
[profiles.openai-eval.backend]
kind="s3"
approved_private_backend=true
access_key={{name="TETRABENCH_AUTH_ACCESS_KEY"}}
secret_key={{name="TETRABENCH_AUTH_SECRET_KEY"}}
storage={{provider="aws",bucket="private-auth",region="us-east-1"}}
''')
        config.chmod(0o600)
        monkeypatch.setattr(
            "tetrabench.auth_profiles.profile_store", lambda *args, **kwargs: authority
        )
        fresh = native("SYNTHETIC_FRESH_LOGIN")

        def login(argv, *, cwd, **kwargs):
            assert prove_cli_operation_stopped(owner, parent)
            if "--version" in argv:
                return NativeResult(0, b"1.18.30")
            if "login" in argv:
                assert kwargs["interactive"] is True
                write_private(cwd / "data/opencode/auth.json", fresh)
            return NativeResult(0, b"OpenAI oauth")

        monkeypatch.setattr("tetrabench.auth.run_native", login)
        result = auth_profile_command(
            "reseed",
            name="openai-eval",
            executable=str(executable),
            config_path=config,
            environment={},
        )
        assert result.generation == 2
        current = authority.read(reference().profile)
        assert current is not None and current.state.native == fresh
        assert current.state.phase == "ready"


def test_missing_legacy_or_unknown_cli_evidence_is_refused(tmp_path):
    assert not prove_cli_operation_stopped("cli-logout-" + "a" * 32, tmp_path)
    assert not prove_cli_operation_stopped("../escape", tmp_path)
