from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tetrabench.artifact_policy import ArtifactLimits
from tetrabench.artifacts import (
    ArtifactDestinationExistsError,
    ArtifactPullRefusedError,
    ArtifactPullService,
    _validate_inventory,
)
from tetrabench.canonical_json import sha256_hex
from tetrabench.models import ResolvedPlan, ResolvedStorageConfig
from tetrabench.plan import canonical_model_bytes, plan_digest
from tetrabench.records import (
    ArtifactBinding,
    ArtifactInventoryEntry,
    ContentObject,
    ContextManifest,
    RequestRecord,
    TerminalRecord,
    TerminalRunState,
)
from tetrabench.s3 import S3IntegrityError
from tetrabench.storage import content_object_key


def _request() -> RequestRecord:
    plan = ResolvedPlan.model_validate(
        {
            "schema_version": 1,
            "section": "systems-design",
            "controller": {"kind": "modal"},
            "execution": {"kind": "modal"},
            "storage": {
                "provider": "aws",
                "bucket": "bucket",
                "region": "us-west-2",
            },
            "selection": {},
            "harbor": {},
            "context": (),
            "trials": ({"task_id": "task", "harbor_task": "task"},),
            "runnable": True,
            "not_runnable_reasons": (),
        }
    )
    manifest = ContextManifest(schema_version=1, files=())
    return RequestRecord(
        schema_version=1,
        run_id="run-1",
        plan_sha256=plan_digest(plan),
        plan=plan,
        context_manifest_sha256=sha256_hex(canonical_model_bytes(manifest)),
        context_manifest=manifest,
    )


def _entry(path: str, data: bytes) -> ArtifactInventoryEntry:
    digest = sha256_hex(data)
    return ArtifactInventoryEntry(
        logical_path=path,
        content=ContentObject(
            sha256=digest,
            key=content_object_key(digest),
            size=len(data),
            media_type="application/octet-stream",
        ),
    )


def _identity_for_test(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _terminal(
    entries: tuple[ArtifactInventoryEntry, ...], outcome: str = "succeeded"
) -> TerminalRunState:
    bindings = tuple(
        ArtifactBinding(logical_path=item.logical_path, sha256=item.content.sha256)
        for item in entries
    )
    terminal = TerminalRecord.model_validate(
        {
            "schema_version": 1,
            "run_id": "run-1",
            "request_sha256": sha256_hex(canonical_model_bytes(_request())),
            "winning_attempt_id": "attempt-1",
            "outcome": outcome,
            "harbor_version": "0.22.0",
            "artifacts": entries,
            "harbor_config": bindings[0] if outcome == "succeeded" else None,
            "harbor_lock": bindings[1] if outcome == "succeeded" else None,
            "harbor_result": bindings[2] if outcome == "succeeded" else None,
            "evidence": (),
            "warnings": (),
        }
    )
    digest = sha256_hex(canonical_model_bytes(terminal))
    return TerminalRunState(run_id="run-1", terminal_sha256=digest, terminal=terminal)


class _Store:
    def __init__(self, state: TerminalRunState, data: dict[str, bytes]) -> None:
        self.state = state
        self.data = data
        storage = _request().plan.storage
        assert storage is not None
        self.storage: ResolvedStorageConfig = storage
        self.reads = 0
        self.on_read = None

    def read_run_state(self, run_id: str):
        _ = run_id
        return self.state

    def read_admission(self, run_id: str):
        _ = run_id
        return None

    def read_request(self, run_id: str, request_sha256: str, request_object_key: str):
        _ = run_id, request_sha256, request_object_key
        return _request()

    def read_content(self, descriptor: ContentObject) -> bytes:
        raise AssertionError(f"buffered artifact read used for {descriptor.key}")

    def stream_content_to_fd(self, descriptor: ContentObject, fd: int) -> None:
        self.reads += 1
        if self.on_read is not None:
            self.on_read(self.reads)
        data = self.data[descriptor.sha256]
        for offset in range(0, len(data), 2):
            os.write(fd, data[offset : offset + 2])
        if len(data) != descriptor.size or sha256_hex(data) != descriptor.sha256:
            raise S3IntegrityError("corrupt content")


def _fixture() -> tuple[tuple[ArtifactInventoryEntry, ...], dict[str, bytes]]:
    values = (
        ("job/config.json", b"config"),
        ("job/lock.json", b"lock"),
        ("job/result.json", b"result"),
    )
    entries = tuple(_entry(path, data) for path, data in values)
    return entries, {
        item.content.sha256: data
        for item, (_path, data) in zip(entries, values, strict=True)
    }


@pytest.mark.parametrize("process_umask", [0o000, 0o777])
def test_artifact_pull_creates_exact_private_tree(
    tmp_path: Path,
    process_umask: int,
) -> None:
    entries, data = _fixture()
    output = tmp_path / "pulled"

    previous_umask = os.umask(process_umask)
    try:
        report = ArtifactPullService(_Store(_terminal(entries), data)).pull(
            "run-1", output
        )
    finally:
        os.umask(previous_umask)

    assert report.output_directory == str(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "job").stat().st_mode) == 0o700
    for entry in entries:
        path = output / entry.logical_path
        assert path.read_bytes() == data[entry.content.sha256]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(str(path.relative_to(output)) for path in output.rglob("*")) == [
        "job",
        "job/config.json",
        "job/lock.json",
        "job/result.json",
    ]


def test_artifact_pull_reapplies_exact_modes_after_mid_pull_widening(
    tmp_path: Path,
) -> None:
    entries, data = _fixture()
    output = tmp_path / "output"
    store = _Store(_terminal(entries), data)

    def widen_after_first_file(reads: int) -> None:
        if reads == 2:
            os.chmod(output, 0o777)
            os.chmod(output / "job", 0o777)
            os.chmod(output / "job/config.json", 0o666)

    store.on_read = widen_after_first_file
    ArtifactPullService(store).pull("run-1", output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "job").stat().st_mode) == 0o700
    for entry in entries:
        assert stat.S_IMODE((output / entry.logical_path).stat().st_mode) == 0o600


def test_artifact_pull_best_effort_restores_partial_modes_without_masking_error(
    tmp_path: Path,
) -> None:
    entries, data = _fixture()
    output = tmp_path / "output"
    data[entries[1].content.sha256] = b"corrupt"
    store = _Store(_terminal(entries), data)

    def widen_before_corrupt_file(reads: int) -> None:
        if reads == 2:
            os.chmod(output, 0o777)
            os.chmod(output / "job", 0o777)
            os.chmod(output / "job/config.json", 0o666)

    store.on_read = widen_before_corrupt_file
    with pytest.raises(S3IntegrityError) as caught:
        ArtifactPullService(store).pull("run-1", output)

    assert str(caught.value) == "corrupt content"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "job").stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "job/config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "job/lock.json").stat().st_mode) == 0o600


def test_artifact_pull_fails_if_mode_changes_after_final_fchmod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entries, data = _fixture()
    regular_file_chmods = 0
    real_fchmod = os.fchmod

    def widen_after_final_fchmod(fd: int, mode: int) -> None:
        nonlocal regular_file_chmods
        real_fchmod(fd, mode)
        if stat.S_ISREG(os.fstat(fd).st_mode) and mode == 0o600:
            regular_file_chmods += 1
            if regular_file_chmods == len(entries) + 1:
                real_fchmod(fd, 0o640)

    monkeypatch.setattr(os, "fchmod", widen_after_final_fchmod)

    with pytest.raises(OSError, match="mode, type, or identity changed"):
        ArtifactPullService(_Store(_terminal(entries), data)).pull(
            "run-1", tmp_path / "output"
        )


def test_artifact_pull_never_overwrites_existing_destination(tmp_path: Path) -> None:
    entries, data = _fixture()
    output = tmp_path / "pulled"
    output.mkdir()
    (output / "sentinel").write_text("preserve")

    with pytest.raises(ArtifactDestinationExistsError):
        ArtifactPullService(_Store(_terminal(entries), data)).pull("run-1", output)

    assert (output / "sentinel").read_text() == "preserve"


def test_artifact_pull_requires_successful_terminal(tmp_path: Path) -> None:
    state = _terminal((), outcome="failed")

    with pytest.raises(ArtifactPullRefusedError, match="successful"):
        ArtifactPullService(_Store(state, {})).pull("run-1", tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_artifact_corruption_retains_private_reservation(tmp_path: Path) -> None:
    entries, data = _fixture()
    data[entries[0].content.sha256] = b"corrupt"
    output = tmp_path / "output"

    with pytest.raises(S3IntegrityError):
        ArtifactPullService(_Store(_terminal(entries), data)).pull("run-1", output)

    assert output.is_dir()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert (output / entries[0].logical_path).read_bytes() == b"corrupt"


@pytest.mark.parametrize(
    ("limits", "entries", "message"),
    [
        (ArtifactLimits(max_files=2), _fixture()[0], "max_files"),
        (
            ArtifactLimits(max_file_bytes=5),
            _fixture()[0],
            "max_file_bytes",
        ),
        (
            ArtifactLimits(max_total_bytes=10),
            _fixture()[0],
            "max_total_bytes",
        ),
    ],
)
def test_receiver_limits_fail_before_destination_reservation(
    tmp_path: Path,
    limits: ArtifactLimits,
    entries: tuple[ArtifactInventoryEntry, ...],
    message: str,
) -> None:
    output = tmp_path / "output"
    store = _Store(_terminal(entries), {})

    with pytest.raises(ArtifactPullRefusedError, match=message):
        ArtifactPullService(store, limits=limits).pull("run-1", output)

    assert not output.exists()
    assert store.reads == 0


def test_artifact_pull_streams_without_buffered_content_read(tmp_path: Path) -> None:
    entries, data = _fixture()
    store = _Store(_terminal(entries), data)

    ArtifactPullService(store).pull("run-1", tmp_path / "output")

    assert store.reads == len(entries)


def test_artifact_tree_fsyncs_files_and_each_created_directory_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entries, data = _fixture()
    synced: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def track_fsync(fd: int) -> None:
        value = os.fstat(fd)
        synced.append((value.st_dev, value.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)
    output = tmp_path / "output"

    ArtifactPullService(_Store(_terminal(entries), data)).pull("run-1", output)

    expected = {tmp_path, output, output / "job"}
    expected.update(output / item.logical_path for item in entries)
    assert {(path.stat().st_dev, path.stat().st_ino) for path in expected}.issubset(
        set(synced)
    )


def test_file_fd_closes_and_partial_evidence_syncs_when_parent_fsync_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entries, data = _fixture()
    output = tmp_path / "output"
    opened_file_fds: list[int] = []
    syncs_after_failure: list[tuple[int, int]] = []
    injected = OSError("injected parent fsync failure")
    failure_injected = False
    real_open = os.open
    real_fsync = os.fsync

    def track_open(path: os.PathLike[str] | str, *args, **kwargs) -> int:
        fd = real_open(path, *args, **kwargs)
        if path == "config.json":
            opened_file_fds.append(fd)
        return fd

    def fail_first_post_open_parent_fsync(fd: int) -> None:
        nonlocal failure_injected
        value = os.fstat(fd)
        if opened_file_fds and stat.S_ISDIR(value.st_mode) and not failure_injected:
            failure_injected = True
            raise injected
        if failure_injected:
            syncs_after_failure.append(_identity_for_test(value))
        real_fsync(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fsync", fail_first_post_open_parent_fsync)

    with pytest.raises(OSError) as caught:
        ArtifactPullService(_Store(_terminal(entries), data)).pull("run-1", output)

    assert caught.value is injected
    assert len(opened_file_fds) == 1
    with pytest.raises(OSError):
        os.fstat(opened_file_fds[0])
    partial = output / "job/config.json"
    assert partial.exists()
    assert _identity_for_test(partial.stat()) in syncs_after_failure
    assert _identity_for_test(partial.parent.stat()) in syncs_after_failure


@pytest.mark.parametrize("directory", ["root", "nested"])
@pytest.mark.parametrize("operation", ["fchmod", "fsync", "fstat", "type"])
def test_directory_fd_closes_once_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    directory: str,
    operation: str,
) -> None:
    entries, data = _fixture()
    output = tmp_path / "output"
    target_name = output.name if directory == "root" else "job"
    target_fd: int | None = None
    close_calls = 0
    failure_injected = False
    injected = OSError(f"injected {directory} {operation} failure")
    real_open = os.open
    real_close = os.close
    real_fchmod = os.fchmod
    real_fsync = os.fsync
    real_fstat = os.fstat
    real_isdir = stat.S_ISDIR

    def track_open(path: os.PathLike[str] | str, *args, **kwargs) -> int:
        nonlocal target_fd
        fd = real_open(path, *args, **kwargs)
        if path == target_name:
            target_fd = fd
        return fd

    def fail_fchmod(fd: int, mode: int) -> None:
        nonlocal failure_injected
        if operation == "fchmod" and fd == target_fd and not failure_injected:
            failure_injected = True
            raise injected
        real_fchmod(fd, mode)

    def fail_fsync(fd: int) -> None:
        nonlocal failure_injected
        if operation == "fsync" and fd == target_fd and not failure_injected:
            failure_injected = True
            raise injected
        real_fsync(fd)

    def fail_fstat(fd: int) -> os.stat_result:
        nonlocal failure_injected
        if operation == "fstat" and fd == target_fd and not failure_injected:
            failure_injected = True
            raise injected
        return real_fstat(fd)

    def fail_type_validation(mode: int) -> bool:
        nonlocal failure_injected
        if operation == "type" and target_fd is not None and not failure_injected:
            failure_injected = True
            return False
        return real_isdir(mode)

    def close_with_failure(fd: int) -> None:
        nonlocal close_calls
        if fd == target_fd:
            close_calls += 1
            real_close(fd)
            raise OSError("injected close failure")
        real_close(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "fchmod", fail_fchmod)
    monkeypatch.setattr(os, "fsync", fail_fsync)
    monkeypatch.setattr(os, "fstat", fail_fstat)
    monkeypatch.setattr(stat, "S_ISDIR", fail_type_validation)
    monkeypatch.setattr(os, "close", close_with_failure)

    expected = injected if operation != "type" else None
    with pytest.raises(OSError) as caught:
        ArtifactPullService(_Store(_terminal(entries), data)).pull("run-1", output)

    if expected is not None:
        assert caught.value is expected
    else:
        assert "not a directory" in str(caught.value) or "identity changed" in str(
            caught.value
        )
    assert target_fd is not None
    assert close_calls == 1
    with pytest.raises(OSError):
        real_fstat(target_fd)


def test_terminal_without_admission_rejects_wrong_storage_before_reservation(
    tmp_path: Path,
) -> None:
    entries, data = _fixture()
    store = _Store(_terminal(entries), data)
    store.storage = store.storage.model_copy(update={"region": "us-east-1"})
    output = tmp_path / "output"

    with pytest.raises(ArtifactPullRefusedError, match="storage"):
        ArtifactPullService(store).pull("run-1", output)

    assert not output.exists()
    assert store.reads == 0


@pytest.mark.parametrize("path", ["", ".", "../escape", "/absolute"])
def test_inventory_rejects_unsafe_paths_even_from_untrusted_construct(
    path: str,
) -> None:
    item = _entry("safe", b"x").model_construct(logical_path=path)

    with pytest.raises(ValueError, match="logical path"):
        _validate_inventory((item,))


def test_inventory_rejects_duplicate_and_prefix_conflicts() -> None:
    first = _entry("a", b"a")
    duplicate = first.model_copy()
    nested = _entry("a/b", b"b")

    with pytest.raises(ArtifactPullRefusedError, match="duplicate"):
        _validate_inventory((first, duplicate))
    with pytest.raises(ArtifactPullRefusedError, match="prefix"):
        _validate_inventory((first, nested))


def test_symlink_replacement_cannot_escape_opened_tree(tmp_path: Path) -> None:
    entries, data = _fixture()
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "moved-job"
    store = _Store(_terminal(entries), data)

    def replace_on_second_read(reads: int) -> None:
        if reads == 2:
            (output / "job").rename(moved)
            os.symlink(outside, output / "job")

    store.on_read = replace_on_second_read
    with pytest.raises(OSError, match="identity changed"):
        ArtifactPullService(store).pull("run-1", output)

    assert list(outside.iterdir()) == []
    assert (moved / "config.json").read_bytes() == b"config"


def test_root_replacement_fails_closed_without_writing_replacement(
    tmp_path: Path,
) -> None:
    entries, data = _fixture()
    output = tmp_path / "output"
    moved = tmp_path / "moved-output"
    store = _Store(_terminal(entries), data)

    def replace_on_first_read(reads: int) -> None:
        if reads == 1:
            output.rename(moved)
            output.mkdir()

    store.on_read = replace_on_first_read
    with pytest.raises(OSError, match="identity changed"):
        ArtifactPullService(store).pull("run-1", output)

    assert list(output.iterdir()) == []
    assert stat.S_IMODE(moved.stat().st_mode) == 0o700


def test_concurrent_injection_is_detected_and_retained(tmp_path: Path) -> None:
    entries, data = _fixture()
    output = tmp_path / "output"
    store = _Store(_terminal(entries), data)

    def inject_on_last_read(reads: int) -> None:
        if reads == len(entries):
            (output / "injected").write_text("evidence")

    store.on_read = inject_on_last_read
    with pytest.raises(OSError, match="unexpected content"):
        ArtifactPullService(store).pull("run-1", output)

    assert (output / "injected").read_text() == "evidence"
