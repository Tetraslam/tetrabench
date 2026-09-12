"""Native reasoning selection and exact, opt-in configuration adoption.

The CLI owner can wire ``inspect_handler`` and ``adopt_handler`` under any command
group. No existing CLI/harness registration is changed by this module.
"""

from __future__ import annotations

import difflib
import fcntl
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import tomlkit

from tetrabench.canonical_json import dumps_canonical_json, sha256_hex
from tetrabench.capabilities import (
    MAX_SNAPSHOT_BYTES,
    CapabilityIdentity,
    CapabilitySnapshot,
    CapabilitySnapshotRef,
    MetadataError,
    Setting,
    metadata_text,
    parse_metadata,
)
from tetrabench.discovery import NativeObservation, discover
from tetrabench.harness_config import (
    HarnessConfig,
    NativeConfig,
    ResolvedHarness,
    ResourceSource,
)

_REASONING_OPTIONS = {
    "opencode": {"variant"},
    "codex": {"reasoning_effort", "reasoning_summary"},
    "claude-code": {
        "reasoning_effort",
        "max_thinking_tokens",
        "max_output_tokens",
        "disable_adaptive_thinking",
    },
    "pi": {"thinking"},
}


def harness_config_digest(
    config: HarnessConfig | ResolvedHarness, *, base: Path | None = None
) -> str:
    """Digest explicit config, including references but never resolving their values.

    Native file resources must already be sealed/inlined by the host. Otherwise
    their changed bytes would not invalidate a reasoning capture.
    """
    if base is not None and isinstance(config, HarnessConfig):
        from tetrabench.native_discovery import _prepared

        config = _prepared(config, base)
    if isinstance(config, ResolvedHarness):
        values = config.model_dump(mode="python")
        if values.get("native_config"):
            values["native_config"].pop("sha256", None)
        values.pop("capability_snapshot", None)
        config = HarnessConfig.model_validate(values)
    if config.native_config and config.native_config.path is not None:
        raise MetadataError("inline or seal native_config before capability discovery")
    if any(isinstance(resource, ResourceSource) for resource in config.resources):
        raise MetadataError("seal harness resources before capability discovery")
    values = config.model_dump(mode="json")
    values.pop("capability_snapshot", None)
    return sha256_hex(dumps_canonical_json(values))


def validate_snapshot_for_harness(
    snapshot: CapabilitySnapshot | CapabilitySnapshotRef,
    config: HarnessConfig | ResolvedHarness,
    *,
    base: Path | None = None,
    resolved_identity: CapabilityIdentity | None = None,
    require_supported: bool = True,
) -> CapabilitySnapshot:
    """Offline request-binding check, including sealed resource/native-file contents.

    A config digest alone does not prove route freshness. The execution owner must
    also supply its resolved identity before claiming current native route support.
    The optional spec field name is ``capability_snapshot`` (excluded from hashing).
    """
    if isinstance(snapshot, CapabilitySnapshotRef):
        snapshot = snapshot.snapshot()
    identity = snapshot.identity
    if (config.name, config.version, config.model) != (
        identity.harness,
        identity.harness_version,
        identity.requested_model,
    ) or harness_config_digest(config, base=base) != identity.config_digest:
        raise MetadataError("snapshot does not bind current harness/resources")
    if resolved_identity is not None:
        snapshot.require_current(resolved_identity)
    if require_supported:
        if snapshot.status != "supported":
            raise MetadataError("snapshot reasoning support is unknown or unavailable")
        from tetrabench.harnesses import normalized_options

        options = (
            normalized_options(config)
            if isinstance(config, HarnessConfig)
            else config.options
        )
        key, control = {
            "opencode": ("variant", "variant"),
            "codex": ("reasoning_effort", "effort"),
            "claude-code": ("reasoning_effort", "effort"),
            "pi": ("thinking", "thinking"),
        }[config.name]
        if options.get(key) is not None:
            select_reasoning(
                snapshot, identity, control=control, select=str(options[key])
            )
        for item in snapshot.controls:
            bounds = item.budget
            if bounds and bounds.binding and bounds.binding.surface == "options":
                value = options.get(bounds.binding.path[0])
                if value is not None:
                    if type(value) is not int:
                        raise MetadataError(
                            "configured reasoning budget must be an integer"
                        )
                    select_reasoning(
                        snapshot, identity, control=item.name, budget=value
                    )
    return snapshot


def validate_resolved_identity(
    snapshot: CapabilitySnapshot, identity: CapabilityIdentity
) -> None:
    snapshot.require_current(identity)


def select_reasoning(
    snapshot: CapabilitySnapshot,
    identity: CapabilityIdentity,
    *,
    control: str,
    select: str | None = None,
    budget: int | None = None,
    accept_normalization: bool = False,
) -> tuple[Setting, ...]:
    """Return actual native settings. Never treat unknowns as validated support."""
    snapshot.require_current(identity)
    if snapshot.status != "supported":
        raise MetadataError(
            "reasoning support unavailable or unknown; inspect the bound route"
        )
    matches = [item for item in snapshot.controls if item.name == control]
    if len(matches) != 1 or matches[0].status != "supported":
        raise MetadataError("reasoning control unsupported or unknown")
    item = matches[0]
    if not item.evidence or all(e.kind == "user-assertion" for e in item.evidence):
        raise MetadataError(
            "strict adoption requires native or explicit metadata evidence"
        )
    if item.kind == "budget":
        bounds = item.budget
        if select is not None or type(budget) is not int or bounds is None:
            raise MetadataError("select one integer token budget")
        if bounds.binding is None or bounds.minimum is None or bounds.maximum is None:
            raise MetadataError(
                "native budget binding or bounds unknown; cannot adopt strictly"
            )
        if not bounds.minimum <= budget <= bounds.maximum:
            raise MetadataError("token budget outside native bounds")
        if bounds.less_than_output and (
            bounds.output_limit is None or budget >= bounds.output_limit
        ):
            raise MetadataError("token budget must be below a known output limit")
        settings = (
            *bounds.requires,
            Setting(binding=bounds.binding, value_json=str(budget)),
        )
    else:
        if budget is not None or select is None:
            raise MetadataError("select one native named choice")
        choices = [choice for choice in item.choices if choice.name == select]
        if len(choices) != 1:
            raise MetadataError("choice absent from native metadata")
        choice = choices[0]
        if choice.normalization == "unknown" or not choice.settings:
            raise MetadataError("native config binding or normalization unknown")
        if choice.normalization == "changed" and not accept_normalization:
            raise MetadataError(
                "native choice clamps; explicitly accept its displayed normalization"
            )
        if choice.normalization == "changed" and choice.effective_choice is None:
            raise MetadataError("native clamp has no disclosed effective choice")
        _check_catalog_choice(snapshot, item.kind, choice.native_value_json)
        settings = choice.settings
    _check_settings(identity.harness, settings)
    return settings


def _check_catalog_choice(
    snapshot: CapabilitySnapshot, kind: str, native_value_json: str | None
) -> None:
    """An explicit catalog restricts a known scalar mapping, never variant names."""
    if kind == "default":
        return
    efforts = [c for c in snapshot.controls if c.name == "catalog.effort"]
    if not efforts:
        return
    effort = efforts[0]
    if effort.choices_presence == "null":
        return  # No gateway allowlist; retain native validation, not endpoint claims.
    if effort.choices_presence == "omitted":
        raise MetadataError("refreshed catalog effort support is unknown")
    if native_value_json is None:
        raise MetadataError("native-to-catalog effort mapping unknown")
    value = parse_metadata(native_value_json)
    if not isinstance(value, str):
        raise MetadataError("native-to-catalog effort mapping unknown")
    if value not in {c.name for c in effort.choices}:
        raise MetadataError("native choice is unsupported by the refreshed catalog")


def _check_settings(harness: str, settings: tuple[Setting, ...]) -> None:
    paths = set()
    for setting in settings:
        key = (setting.binding.surface, setting.binding.path)
        if key in paths:
            raise MetadataError("conflicting native reasoning settings")
        paths.add(key)
        if setting.binding.surface == "options" and (
            len(setting.binding.path) != 1
            or setting.binding.path[0] not in _REASONING_OPTIONS.get(harness, set())
        ):
            raise MetadataError("binding is not a reasoning option for this harness")
        if setting.binding.surface != "options":
            native_paths = {
                "codex": {("model_reasoning_effort",), ("model_reasoning_summary",)},
                "claude-code": {("effortLevel",), ("alwaysThinkingEnabled",)},
            }
            if setting.binding.path not in native_paths.get(harness, set()):
                raise MetadataError(
                    "native binding is not a verified reasoning setting"
                )
        # Native bindings are explicit adapter evidence; config validators remain
        # the authority on which native fields this controlled harness accepts.
        value = parse_metadata(setting.value_json)
        if setting.binding.surface == "options" and type(value) not in {
            str,
            int,
            bool,
            type(None),
        }:
            raise MetadataError("harness options require scalar values")


def _patch_value(root: Any, setting: Setting) -> None:
    parent = root
    for component in setting.binding.path[:-1]:
        if component not in parent:
            parent[component] = {}
        parent = parent[component]
        if not isinstance(parent, dict):
            raise MetadataError("native setting path crosses a non-object")
    leaf = setting.binding.path[-1]
    if setting.remove:
        parent.pop(leaf, None)
    else:
        value = parse_metadata(setting.value_json)
        if value is None and setting.binding.surface == "native-toml":
            raise MetadataError(
                "TOML cannot encode null; use an explicit removal binding"
            )
        parent[leaf] = value


def adopt_harness(
    config: HarnessConfig,
    snapshot: CapabilitySnapshot,
    identity: CapabilityIdentity,
    *,
    base: Path | None = None,
    **selection: Any,
) -> HarnessConfig:
    """Pure adoption into the current HarnessConfig schema, with validation."""
    if harness_config_digest(config, base=base) != identity.config_digest or (
        config.name,
        config.version,
        config.model,
    ) != (identity.harness, identity.harness_version, identity.requested_model):
        raise MetadataError("harness configuration drift; inspect again")
    settings = select_reasoning(snapshot, identity, **selection)
    values = config.model_dump(mode="python")
    options = dict(config.options)
    native = config.native_config
    for setting in settings:
        if setting.binding.surface == "options":
            _patch_value(options, setting)
            continue
        fmt = "json" if setting.binding.surface == "native-json" else "toml"
        if native is not None and native.path is not None:
            from tetrabench.harnesses import read_config_text

            if base is None:
                raise MetadataError(
                    "native config path requires an explicit base directory"
                )
            native_path = Path(native.path).expanduser()
            if not native_path.is_absolute():
                native_path = base / native_path
            native = NativeConfig(
                format=native.format, text=read_config_text(native_path)
            )
        if native and native.format != fmt:
            raise MetadataError(
                "reasoning binding conflicts with native configuration format"
            )
        text = native.text if native else None
        root = (
            parse_metadata(text or "{}") if fmt == "json" else tomlkit.parse(text or "")
        )
        if not isinstance(root, dict):
            raise MetadataError("native configuration must be an object")
        _patch_value(root, setting)
        native = NativeConfig(
            format=fmt,
            text=metadata_text(root) if fmt == "json" else tomlkit.dumps(root),
        )
    values["options"] = options
    values["native_config"] = native.model_dump() if native else None
    try:
        return HarnessConfig.model_validate(values)
    except ValueError:
        raise MetadataError(
            "selected native setting is not accepted by the installed harness adapter; "
            "update that adapter before adoption"
        ) from None


def rebind_after_adoption(
    before: HarnessConfig,
    after: HarnessConfig,
    snapshot: CapabilitySnapshot,
    *,
    identity: CapabilityIdentity,
    base: Path | None = None,
    **selection: Any,
) -> CapabilitySnapshot:
    """Rebind only a proven selection diff; retain the original capture identity."""
    expected = adopt_harness(before, snapshot, identity, base=base, **selection)
    if harness_config_digest(expected, base=base) != harness_config_digest(
        after, base=base
    ):
        raise MetadataError(
            "post-adoption config changes exceed selected native settings"
        )
    return CapabilitySnapshot.model_validate(
        {
            **snapshot.model_dump(mode="python"),
            "identity": identity.model_copy(
                update={
                    "config_digest": harness_config_digest(after, base=base),
                }
            ),
            "capture_identity": snapshot.capture_identity or identity,
            "adopted_from_digest": snapshot.digest,
        }
    )


def suggested_snippets(snapshot: CapabilitySnapshot) -> tuple[dict[str, Any], ...]:
    """Small valid TOML patches, with normalization and unknowns kept visible."""
    suggestions = []
    for control in snapshot.controls:
        for choice in control.choices:
            if not choice.settings:
                continue
            patch: dict[str, Any] = {}
            for setting in choice.settings:
                if setting.binding.surface != "options" or setting.remove:
                    continue
                patch.setdefault("harness", {}).setdefault("options", {})[
                    setting.binding.path[0]
                ] = parse_metadata(setting.value_json)
            suggestions.append(
                {
                    "control": control.name,
                    "select": choice.name,
                    "effective_choice": choice.effective_choice,
                    "normalization": choice.normalization,
                    "status": control.status,
                    "config_snippet": tomlkit.dumps(patch),
                    "requires_normalization_acceptance": choice.normalization
                    == "changed",
                }
            )
    return tuple(suggestions)


def inspect_handler(
    identity: CapabilityIdentity,
    *,
    observation: NativeObservation | None = None,
    cached: CapabilitySnapshot | None = None,
    **discovery_options: Any,
) -> dict[str, Any]:
    """CLI-neutral JSON-safe handler. It does not print, create sessions, or write."""
    snapshot = discover(
        identity, observation=observation, cached=cached, **discovery_options
    )
    return {
        "capability": snapshot.model_dump(mode="json"),
        "snapshot_sha256": snapshot.digest,
        "suggestions": list(suggested_snippets(snapshot)),
        "inference_validated": False,
    }


def preview_adoption(
    document: str,
    snapshot: CapabilitySnapshot,
    identity: CapabilityIdentity,
    *,
    base: Path | None = None,
    **selection: Any,
) -> dict[str, Any]:
    """Preserve unrelated TOML fields/comments and show the exact resulting diff."""
    if len(document.encode()) > MAX_SNAPSHOT_BYTES:
        raise MetadataError("harness document exceeds 128 KiB")
    try:
        parsed = tomlkit.parse(document)
        config = HarnessConfig.model_validate(parsed["harness"].unwrap())
    except (ValueError, KeyError, AttributeError):
        raise MetadataError("invalid portable harness TOML") from None
    updated = adopt_harness(config, snapshot, identity, base=base, **selection)
    # Change only selected members; do not dump a whole default-filled config.
    settings = select_reasoning(snapshot, identity, **selection)
    for setting in settings:
        if setting.binding.surface == "options":
            harness = parsed["harness"]
            if "options" not in harness:
                harness["options"] = tomlkit.table()
            _patch_value(harness["options"], setting)
        else:
            native = updated.native_config
            if native is None:
                raise MetadataError("adopted native config unexpectedly absent")
            parsed["harness"]["native_config"] = {
                "format": native.format,
                "text": native.text,
            }
    after = tomlkit.dumps(parsed)
    rebound = rebind_after_adoption(
        config, updated, snapshot, identity=identity, base=base, **selection
    )
    return {
        "before_sha256": sha256_hex(document.encode()),
        "after_sha256": sha256_hex(after.encode()),
        "document": after,
        "diff": "".join(
            difflib.unified_diff(
                document.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="harness.before.toml",
                tofile="harness.after.toml",
            )
        ),
        "snapshot_sha256": snapshot.digest,
        "after_config_digest": rebound.identity.config_digest,
        "after_capability_snapshot": CapabilitySnapshotRef.from_snapshot(
            rebound
        ).model_dump(),
        "inference_validated": False,
        "written": False,
        "reinspect_required": after != document,
    }


def adopt_handler(
    path: Path,
    snapshot: CapabilitySnapshot,
    identity: CapabilityIdentity,
    *,
    write: bool = False,
    expected_file_sha256: str | None = None,
    **selection: Any,
) -> dict[str, Any]:
    """Preview by default. Explicit write uses locked, checked atomic replacement.

    Same-UID uncooperative writers/path-ancestor replacement remain outside the
    cooperative lock boundary; no overwrite is attempted after observed drift.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as source:
        if write:
            fcntl.flock(source, fcntl.LOCK_EX)
        before_stat = os.fstat(source.fileno())
        if not stat.S_ISREG(before_stat.st_mode) or before_stat.st_nlink != 1:
            raise MetadataError("adoption requires a regular single-link harness file")
        before = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(before) > MAX_SNAPSHOT_BYTES:
            raise MetadataError("harness document exceeds 128 KiB")
        if (
            expected_file_sha256 is not None
            and sha256_hex(before) != expected_file_sha256
        ):
            raise MetadataError("harness file differs from expected inspection bytes")
        try:
            text = before.decode("utf-8")
        except UnicodeError:
            raise MetadataError("harness document must be UTF-8") from None
        result = preview_adoption(
            text, snapshot, identity, base=path.parent, **selection
        )
        if not write or result["document"] == text:
            return result
        temp_name = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".reasoning-", dir=path.parent)
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), stat.S_IMODE(before_stat.st_mode) & 0o777)
                target.write(result["document"].encode())
                target.flush()
                os.fsync(target.fileno())
            original_config = HarnessConfig.model_validate(
                tomlkit.parse(text)["harness"].unwrap()
            )
            validate_snapshot_for_harness(
                snapshot, original_config, base=path.parent, require_supported=False
            )
            current = path.stat(follow_symlinks=False)
            source.seek(0)
            if (
                current.st_dev,
                current.st_ino,
                current.st_mtime_ns,
                current.st_size,
            ) != (
                before_stat.st_dev,
                before_stat.st_ino,
                before_stat.st_mtime_ns,
                before_stat.st_size,
            ) or source.read(MAX_SNAPSHOT_BYTES + 1) != before:
                raise MetadataError("harness changed during adoption; retry inspection")
            os.replace(temp_name, path)
            temp_name = None
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            result["written"] = True
            return result
        finally:
            if temp_name is not None:
                os.unlink(temp_name)
