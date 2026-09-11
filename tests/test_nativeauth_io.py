from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

import pytest

from tetrabench.nativeauth_io import (
    read_auth,
    regular_codex_auth,
    snapshot,
    verify_metadata_helper,
    write_auth,
)


@pytest.fixture
def native_root():
    root = Path("/tmp") / ("tetrabench-auth-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    try:
        (root / "codex").mkdir(mode=0o700)
        (root / "secrets").mkdir(mode=0o700)
        yield root
    finally:
        shutil.rmtree(root)


def test_exact_harbor_codex_symlink_becomes_private_native_regular_file(native_root):
    source = native_root / "secrets/auth.json"
    source.write_bytes(b"SYNTHETIC_NATIVE_AUTH")
    source.chmod(0o644)
    target = native_root / "codex/auth.json"
    target.symlink_to(source)
    regular_codex_auth(native_root)
    assert not target.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o600
    assert read_auth(native_root, target) == b"SYNTHETIC_NATIVE_AUTH"
    output = native_root / "snapshot"
    snapshot(native_root, target, output)
    target.unlink()
    assert read_auth(native_root, output) == b"SYNTHETIC_NATIVE_AUTH"


def test_symlink_elsewhere_hardlink_and_large_native_file_refused(native_root):
    source = native_root / "secrets/auth.json"
    write_auth(source, b"SYNTHETIC_NATIVE_AUTH")
    target = native_root / "codex/auth.json"
    target.symlink_to(native_root / "other")
    with pytest.raises(ValueError):
        regular_codex_auth(native_root)
    target.unlink()
    os.link(source, target)
    with pytest.raises(ValueError):
        read_auth(native_root, target)
    target.unlink()
    source.unlink()
    write_auth(source, b"S" * (128 * 1024 + 1))
    with pytest.raises(ValueError):
        read_auth(native_root, source)


def test_snapshot_never_follows_existing_destination_link(native_root):
    source = native_root / "secrets/auth.json"
    write_auth(source, b"SYNTHETIC_NATIVE_AUTH")
    destination = native_root / "snapshot"
    destination.symlink_to(native_root / "outside-target")
    with pytest.raises(FileExistsError):
        snapshot(native_root, source, destination)
    assert not (native_root / "outside-target").exists()


def test_metadata_helper_verifies_owned_source_bytes_before_execution():
    root = Path("/tmp") / ("tetrabench-capability-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    try:
        path = root / "runtime_metadata.py"
        source = b"# synthetic metadata-only fixture\n"
        path.write_bytes(source)
        path.chmod(0o600)
        digest = hashlib.sha256(source).hexdigest()
        verify_metadata_helper(path, digest)
        path.write_bytes(b"# changed helper\n")
        with pytest.raises(ValueError):
            verify_metadata_helper(path, digest)
        path.unlink()
        path.symlink_to(root / "other.py")
        with pytest.raises(OSError):
            verify_metadata_helper(path, digest)
    finally:
        shutil.rmtree(root)
