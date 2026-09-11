"""Offline-first native model discovery; no runtime creation or credential access.

The host supplies effective, already-merged native metadata. Readers are injected
and must be metadata-only: starting a CLI/SDK session is deliberately not done
here (even native model-list initialization can run auth helpers or plugins).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from tetrabench.capabilities import (
    MAX_METADATA_BYTES,
    Binding,
    Budget,
    CapabilityIdentity,
    CapabilitySnapshot,
    Choice,
    Control,
    Evidence,
    MetadataError,
    Setting,
    make_evidence,
    metadata_text,
    parse_metadata,
)
from tetrabench.native_control import ControlError, select_model_info

NATIVE_VERSIONS = {
    "opencode": ("1.18.30", "1.18.30"),
    "codex": ("0.154.0", "0.154.0"),
    "claude-code": ("2.1.267", "0.3.267"),
    "pi": ("0.85.1", "0.85.1"),
}
NATIVE_SOURCES = {
    "opencode": "https://github.com/anomalyco/opencode/blob/v1.18.30/"
    "packages/opencode/src/provider/provider.ts",
    "codex": "https://github.com/openai/codex/blob/rust-v0.154.0/"
    "codex-rs/app-server-protocol/schema/typescript/v2/Model.ts",
    "claude-code": "https://unpkg.com/@anthropic-ai/claude-agent-sdk@0.3.267/sdk.d.ts",
    "pi": "https://github.com/earendil-works/pi/blob/v0.85.1/packages/ai/src/models.ts",
}
INSTALL_ACTIONS = {
    "opencode": "Install opencode-ai@1.18.30; supply the effective provider response.",
    "codex": "Install @openai/codex@0.154.0 and supply app-server model/list pages.",
    "claude-code": "Install @anthropic-ai/claude-agent-sdk@0.3.267 with CLI 2.1.267; "
    "supply supportedModels() from an explicitly authorized session.",
    "pi": "Install @earendil-works/pi-coding-agent@0.85.1 and pi-ai@0.85.1; "
    "supply ModelRuntime.getModel/ModelRegistry.find and getSupportedThinkingLevels.",
}


@dataclass(frozen=True)
class NativeObservation:
    identity: CapabilityIdentity
    payload_json: str
    evidence: Evidence


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MetadataError("expected a native metadata object")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list) or len(value) > 10000:
        raise MetadataError("expected a bounded native metadata array")
    return value


def _names(value: Any) -> list[str]:
    result = _array(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise MetadataError("expected native choice names")
    if len(result) > 128 or len(result) != len(set(result)):
        raise MetadataError("too many or duplicate native choices")
    return result


def _flag(row: dict[str, Any], key: str) -> bool | None:
    value = row.get(key)
    if key in row and type(value) is not bool:
        raise MetadataError("expected a boolean native capability")
    return value


def _only(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 1:
        raise MetadataError(
            "native model missing or ambiguous; resolve the route/model"
        )
    return rows[0]


def _option(name: str, value: Any, *, remove: bool = False) -> Setting:
    return Setting(
        binding=Binding(surface="options", path=(name,)),
        value_json=metadata_text(value),
        remove=remove,
    )


def _choice(name: str, option: str, native: Any = None) -> Choice:
    return Choice(
        name=name,
        settings=(_option(option, name),),
        normalization="identity",
        effective_choice=name,
        native_value_json=metadata_text(native) if native is not None else None,
    )


def _default(option: str, evidence: Evidence, default: Any = None) -> Control:
    return Control(
        name="default",
        kind="default",
        status="supported",
        choices_presence="present",
        evidence=(evidence,),
        default_json=metadata_text(default),
        choices=(
            Choice(
                name="default",
                settings=(_option(option, None, remove=True),),
                normalization="identity",
                effective_choice="default",
            ),
        ),
    )


def _snapshot(
    identity: CapabilityIdentity,
    controls: list[Control],
    evidence: Evidence,
    metadata: dict[str, Any],
    limitations: tuple[str, ...] = (),
) -> CapabilitySnapshot:
    usable = any(
        control.status == "supported" and control.kind != "default"
        for control in controls
    )
    return CapabilitySnapshot(
        identity=identity,
        status="supported"
        if usable and identity.route_status == "bound"
        else "unknown",
        controls=tuple(controls),
        evidence=(evidence,),
        metadata_json=metadata_text(metadata),
        limitations=(
            "Native metadata acceptance only; provider execution is unproven.",
            "Default removes the harness override; native settings remain active.",
            *limitations,
        ),
    )


def _opencode(obs: NativeObservation, payload: Any) -> CapabilitySnapshot:
    identity, evidence = obs.identity, obs.evidence
    root = _object(payload)
    # GET /provider returns {all, default, connected}; /config/providers uses providers.
    providers = _array(root.get("all", root.get("providers")))
    provider = _only(
        [
            _object(row)
            for row in providers
            if _object(row).get("id") == identity.provider_id
        ]
    )
    models = _object(provider.get("models"))
    requested = identity.requested_model.removeprefix(identity.provider_id + "/")
    row = _object(models.get(requested))
    raw_api = _object(row.get("api"))
    api = {key: raw_api[key] for key in ("id", "url", "npm") if key in raw_api}
    endpoint = _object(provider.get("options", {})).get("baseURL") or api.get("url")
    if (
        row.get("providerID") != identity.provider_id
        or api.get("id") != identity.resolved_model
        or (endpoint and endpoint not in identity.endpoints)
        or api.get("npm") != identity.protocol
    ):
        raise MetadataError("effective OpenCode model/route differs from identity")
    capabilities = _object(row.get("capabilities"))
    reasoning = _flag(capabilities, "reasoning")
    variants = row.get("variants")
    controls: list[Control] = []
    if variants is None:
        controls.append(Control(name="variant", kind="choices", status="unknown"))
    else:
        variants = _object(variants)
        # This is an *effective* native response; do not redo provider/config merging.
        if any("disabled" in _object(value) for value in variants.values()):
            raise MetadataError(
                "raw variant overrides supplied; obtain merged native variants"
            )
        choices = tuple(
            _choice(name, "variant", value) for name, value in variants.items()
        )
        controls.append(
            Control(
                name="variant",
                kind="choices",
                status="supported" if choices else "unsupported",
                choices=choices,
                choices_presence="present",
                evidence=(evidence,),
            )
        )
    controls.append(_default("variant", evidence))
    return _snapshot(
        identity,
        controls,
        evidence,
        {
            "id": row.get("id"),
            "api": api,
            "reasoning": reasoning,
            "variants": variants,
        },
        ("Variant names are native selectors, not wire effort levels.",),
    )


# Version-matched pi-ai algorithm. This is a harness transform, not provider inference.
_PI_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def pi_supported_thinking_levels(model: dict[str, Any]) -> tuple[str, ...]:
    reasoning = _flag(model, "reasoning")
    if reasoning is None:
        raise MetadataError("Pi model omits reasoning capability")
    if not reasoning:
        return ("off",)
    mapping = _object(model.get("thinkingLevelMap", {}))
    if any(key not in _PI_LEVELS for key in mapping) or any(
        value is not None and not isinstance(value, str) for value in mapping.values()
    ):
        raise MetadataError("malformed Pi thinkingLevelMap")
    return tuple(
        level
        for level in _PI_LEVELS
        if not (level in mapping and mapping[level] is None)
        and (level not in {"xhigh", "max"} or level in mapping)
    )


def pi_clamp_thinking_level(model: dict[str, Any], level: str) -> str:
    levels = pi_supported_thinking_levels(model)
    if level in levels:
        return level
    if level in _PI_LEVELS:
        index = _PI_LEVELS.index(level)
        for candidate in (*_PI_LEVELS[index:], *reversed(_PI_LEVELS[:index])):
            if candidate in levels:
                return candidate
    return levels[0] if levels else "off"


def _pi(obs: NativeObservation, payload: Any) -> CapabilitySnapshot:
    identity, evidence = obs.identity, obs.evidence
    root = _object(payload)
    model = _object(root.get("model"))
    if (
        model.get("id") != identity.resolved_model
        or model.get("provider") != identity.provider_id
        or model.get("api") != identity.protocol
        or model.get("baseUrl") not in identity.endpoints
    ):
        raise MetadataError("effective Pi model/route differs from identity")
    levels = pi_supported_thinking_levels(model)
    if "supportedThinkingLevels" in root:
        # Prefer SDK output; fail rather than silently accept a different SDK algorithm.
        if tuple(_names(root["supportedThinkingLevels"])) != levels:
            raise MetadataError(
                "Pi SDK and pinned transform disagree; revalidate SDK version"
            )
    else:
        evidence = evidence.model_copy(update={"kind": "native-heuristic"})
    mapping = _object(model.get("thinkingLevelMap", {}))
    choices = []
    clamps = {}
    for level in _PI_LEVELS:
        effective = pi_clamp_thinking_level(model, level)
        clamps[level] = effective
        choice = _choice(level, "thinking", mapping.get(effective))
        choices.append(
            choice.model_copy(
                update={
                    "normalization": "identity" if level == effective else "changed",
                    "effective_choice": effective,
                    "settings": (_option("thinking", effective),),
                }
            )
        )
    if "normalizations" in root and _object(root["normalizations"]) != clamps:
        raise MetadataError("Pi SDK normalization disagrees with pinned transform")
    control = Control(
        name="thinking",
        kind="choices",
        status="supported" if levels else "unsupported",
        choices=tuple(choices),
        choices_presence="present",
        evidence=(evidence,),
    )
    metadata = {
        key: model[key]
        for key in (
            "id",
            "provider",
            "api",
            "baseUrl",
            "reasoning",
            "thinkingLevelMap",
            "maxTokens",
        )
        if key in model
    }
    # Preserve native compatibility facts, never infer from vendor/model names.
    compat = _object(model.get("compat", {}))
    metadata["compat"] = {
        key: compat[key]
        for key in (
            "forceAdaptiveThinking",
            "supportsReasoningEffort",
            "thinkingFormat",
            "thinkingTokenBudgetField",
            "supportsThinkingTokenBudget",
        )
        if key in compat
    }
    metadata["normalizations"] = clamps
    if "supportedThinkingLevels" in root and "normalizations" in root:
        metadata["runtime_capture"] = {
            "model": model,
            "supportedThinkingLevels": root["supportedThinkingLevels"],
            "normalizations": root["normalizations"],
        }
        if "catalogEntry" in root:
            metadata["native_catalog_entry"] = root["catalogEntry"]
    off_choice = next(choice for choice in choices if choice.name == "off")
    off = Control(
        name="off",
        kind="off",
        status="supported" if "off" in levels else "unsupported",
        choices=(off_choice,) if "off" in levels else (),
        evidence=(evidence,),
    )
    adaptive = Control(
        name="adaptive",
        kind="adaptive",
        status="supported"
        if compat.get("forceAdaptiveThinking") is True
        else "unknown",
        evidence=(evidence,),
    )
    return _snapshot(
        identity,
        [control, off, adaptive, _default("thinking", evidence)],
        evidence,
        metadata,
        (
            "Missing Pi thinkingLevelMap entries use provider defaults; "
            "the native level is not a proven wire effort.",
            "Eligible captured catalog entries can seed a fresh native store. "
            "The published consumer is checked at startup; future dispatch and "
            "provider changes are not verified.",
        ),
    )


def _codex(obs: NativeObservation, payload: Any) -> CapabilitySnapshot:
    root = _object(payload)
    if root.get("nextCursor", "missing") is not None:
        raise MetadataError("incomplete Codex model/list pages")
    identity, evidence = obs.identity, obs.evidence
    row = _only(
        [
            _object(row)
            for row in _array(root.get("data"))
            if _object(row).get("model") == identity.resolved_model
        ]
    )
    efforts = row.get("supportedReasoningEfforts")
    choices = (
        ()
        if efforts is None
        else tuple(
            _choice(
                _object(item)["reasoningEffort"],
                "reasoning_effort",
                _object(item)["reasoningEffort"],
            )
            for item in _array(efforts)
        )
    )
    control = Control(
        name="effort",
        kind="choices",
        status="unknown"
        if efforts is None
        else "supported"
        if choices
        else "unsupported",
        choices=choices,
        choices_presence="present" if efforts is not None else "omitted",
        default_json=metadata_text(row.get("defaultReasoningEffort")),
        evidence=(evidence,),
    )
    return _snapshot(
        identity,
        [
            control,
            _default("reasoning_effort", evidence, row.get("defaultReasoningEffort")),
        ],
        evidence,
        {
            key: row[key]
            for key in (
                "id",
                "model",
                "supportedReasoningEfforts",
                "defaultReasoningEffort",
            )
            if key in row
        },
        ("Codex catalog richness depends on the explicit native auth route.",),
    )


def _claude(obs: NativeObservation, payload: Any) -> CapabilitySnapshot:
    identity, evidence = obs.identity, obs.evidence
    # supportedModels() array, or the models member of initializationResult().
    rows = _array(payload.get("models") if isinstance(payload, dict) else payload)
    try:
        row = select_model_info(
            [_object(row) for row in rows],
            identity.requested_model.split("/", 1)[-1],
            identity.resolved_model,
        )
    except ControlError:
        raise MetadataError("native model missing or conflicting descriptor") from None
    if row.get("resolvedModel") != identity.resolved_model:
        raise MetadataError(
            "Claude SDK did not resolve the requested model; inspect again"
        )
    supports = _flag(row, "supportsEffort")
    levels = row.get("supportedEffortLevels")
    if supports is False and levels:
        raise MetadataError("contradictory Claude effort metadata")
    choices = (
        ()
        if levels is None
        else tuple(
            _choice(level, "reasoning_effort", level) for level in _names(levels)
        )
    )
    adaptive = _flag(row, "supportsAdaptiveThinking")
    controls = [
        Control(
            name="effort",
            kind="choices",
            status=(
                "unsupported"
                if supports is False
                else "supported"
                if supports and choices
                else "unknown"
            ),
            choices=choices,
            choices_presence="present" if levels is not None else "omitted",
            evidence=(evidence,),
        ),
        Control(
            name="adaptive",
            kind="adaptive",
            status="unknown"
            if adaptive is None
            else "supported"
            if adaptive
            else "unsupported",
            evidence=(evidence,),
        ),
        _default("reasoning_effort", evidence),
    ]
    return _snapshot(
        identity,
        controls,
        evidence,
        {
            key: row[key]
            for key in (
                "value",
                "resolvedModel",
                "supportsEffort",
                "supportedEffortLevels",
                "supportsAdaptiveThinking",
            )
            if key in row
        },
        ("Adaptive support alone does not supply a portable budget or off binding.",),
    )


def discover(
    identity: CapabilityIdentity,
    *,
    observation: NativeObservation | None = None,
    cached: CapabilitySnapshot | None = None,
    refresh: bool = False,
    allow_authenticated_read: bool = False,
    native_reader: Callable[[CapabilityIdentity], NativeObservation] | None = None,
) -> CapabilitySnapshot:
    """Inspect supplied/native-cache data; invoke an external reader only by opt-in.

    A cached snapshot is never replaced in place. Drift always requires a fresh
    observation. Native readers are host-owned, reviewed metadata-only operations;
    both flags are required because initialization may read native auth state.
    """
    if cached is not None:
        cached.require_current(identity)
        if not refresh:
            if observation is not None:
                raise MetadataError(
                    "explicit refresh required to replace cached evidence"
                )
            return cached
    if (
        identity.harness_version,
        identity.native_adapter_version,
    ) != NATIVE_VERSIONS.get(identity.harness):
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                "Native version/schema unverified; install the supported pin.",
                INSTALL_ACTIONS.get(
                    identity.harness, "Supply a verified native adapter."
                ),
            ),
        )
    if observation is None and refresh and allow_authenticated_read and native_reader:
        try:
            observation = native_reader(identity)
        except Exception:
            # SDK exceptions frequently include request headers or subprocess output.
            raise MetadataError(
                "native metadata read failed; check the native installation"
            ) from None
    if observation is None:
        return CapabilitySnapshot(
            identity=identity,
            status="unavailable",
            limitations=(
                INSTALL_ACTIONS[identity.harness],
                "No native session was started. Supply a metadata capture, or allow "
                "refresh and authenticated metadata reads with a reviewed reader.",
            ),
        )
    if observation.identity != identity:
        raise MetadataError("native observation identity differs; revalidate")
    parser = {
        "opencode": _opencode,
        "pi": _pi,
        "codex": _codex,
        "claude-code": _claude,
    }[identity.harness]
    try:
        return parser(observation, parse_metadata(observation.payload_json))
    except ValidationError:
        raise MetadataError(
            "native metadata is malformed or not snapshot-safe"
        ) from None
    except (KeyError, TypeError):
        raise MetadataError("malformed native model metadata") from None


def collect_codex_pages(
    request: Callable[[str, dict[str, Any]], str],
    *,
    refresh: bool = False,
    allow_authenticated_read: bool = False,
) -> str:
    """Read model/list from an already-authorized app-server, never start one."""
    if not refresh or not allow_authenticated_read:
        raise MetadataError(
            "model/list requires explicit refresh and authenticated-read opt-in"
        )
    rows: list[Any] = []
    cursor = None
    seen = set()
    total_bytes = 0
    for _ in range(32):
        try:
            raw = request(
                "model/list",
                {"cursor": cursor, "limit": 100, "includeHidden": True},
            )
            total_bytes += len(raw.encode())
            if total_bytes > MAX_METADATA_BYTES:
                raise MetadataError("Codex metadata exceeds total byte limit")
            page = _object(parse_metadata(raw))
        except MetadataError:
            raise
        except Exception:
            raise MetadataError("Codex metadata page failed") from None
        rows.extend(_array(page.get("data")))
        if len(rows) > 10000:
            raise MetadataError("Codex model count exceeds limit")
        if "nextCursor" not in page:
            raise MetadataError("Codex page omitted pagination completion")
        cursor = page["nextCursor"]
        if cursor is None:
            return metadata_text({"data": rows, "nextCursor": None})
        if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
            raise MetadataError("invalid or repeated Codex pagination cursor")
        seen.add(cursor)
    raise MetadataError("Codex page limit exceeded")


def catalog_controls(
    row: dict[str, Any],
    evidence: Evidence,
    *,
    schema: Literal["models.dev", "openrouter"],
) -> tuple[Control, ...]:
    """Metadata-only controls. Bindings must come from the effective native SDK.

    In particular, a gateway's accepted wire effort is not an OpenCode variant
    name or a Pi level. No provider-specific config translation takes place here.
    """
    controls: list[Control] = []
    if schema == "models.dev":
        options = row.get("reasoning_options")
        if options is None:
            return (
                Control(
                    name="catalog.reasoning",
                    kind="choices",
                    status="unknown",
                    evidence=(evidence,),
                ),
            )
        for index, option in enumerate(_array(options)):
            item = _object(option)
            kind = item.get("type")
            if kind == "effort":
                values = _array(item.get("values"))
                if any(
                    value is not None and not isinstance(value, str) for value in values
                ):
                    raise MetadataError("invalid catalog effort values")
                controls.append(
                    Control(
                        name=f"catalog.effort.{index}",
                        kind="choices",
                        status="supported",
                        choices_presence="present",
                        evidence=(evidence,),
                        choices=tuple(
                            Choice(
                                name=value if value is not None else "<null>",
                                native_value_json=metadata_text(value),
                            )
                            for value in values
                        ),
                    )
                )
            elif kind == "budget_tokens":
                controls.append(
                    Control(
                        name=f"catalog.budget.{index}",
                        kind="budget",
                        status="supported",
                        budget=Budget(
                            minimum=_bound(item.get("min")),
                            maximum=_bound(item.get("max")),
                        ),
                        evidence=(evidence,),
                    )
                )
            elif kind == "toggle":
                controls.append(
                    Control(
                        name=f"catalog.toggle.{index}",
                        kind="off",
                        status="supported",
                        evidence=(evidence,),
                    )
                )
            else:
                raise MetadataError("unknown models.dev reasoning control")
    else:
        reasoning = row.get("reasoning")
        if reasoning is None:
            return (
                Control(
                    name="catalog.effort",
                    kind="choices",
                    status="unknown",
                    evidence=(evidence,),
                ),
            )
        reasoning = _object(reasoning)
        presence = (
            "omitted"
            if "supported_efforts" not in reasoning
            else "null"
            if reasoning["supported_efforts"] is None
            else "present"
        )
        values = _array(reasoning["supported_efforts"]) if presence == "present" else []
        if any(value is not None and not isinstance(value, str) for value in values):
            raise MetadataError("invalid gateway effort values")
        controls.append(
            Control(
                name="catalog.effort",
                kind="choices",
                status="unknown" if presence == "omitted" else "supported",
                choices_presence=presence,
                evidence=(evidence,),
                choices=tuple(
                    Choice(
                        name=value if value is not None else "<null>",
                        native_value_json=metadata_text(value),
                    )
                    for value in values
                ),
                default_json=metadata_text(reasoning.get("default_effort")),
            )
        )
        mandatory = _flag(reasoning, "mandatory")
        controls.append(
            Control(
                name="catalog.off",
                kind="off",
                status="unknown"
                if mandatory is None
                else "unsupported"
                if mandatory
                else "supported",
                evidence=(evidence,),
                default_json=metadata_text(reasoning.get("default_enabled")),
            )
        )
        if _flag(reasoning, "supports_max_tokens"):
            controls.append(
                Control(
                    name="catalog.budget",
                    kind="budget",
                    status="supported",
                    budget=Budget(),
                    evidence=(evidence,),
                )
            )
        parameters = (
            _names(row["supported_parameters"]) if "supported_parameters" in row else []
        )
        controls.append(
            Control(
                name="catalog.visibility",
                kind="visibility",
                status="supported" if "include_reasoning" in parameters else "unknown",
                evidence=(evidence,),
            )
        )
    return tuple(controls)


def _bound(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) not in {int, float} or value < 0 or value != int(value):
        raise MetadataError("token bound must be a nonnegative integer")
    return int(value)


def attach_catalog(
    snapshot: CapabilitySnapshot,
    row_json: str,
    *,
    evidence: Evidence,
    schema: Literal["models.dev", "openrouter"],
    refresh: bool = False,
) -> CapabilitySnapshot:
    """Return a new snapshot; never replace native caches or native choice bindings."""
    if not refresh:
        raise MetadataError("catalog adoption requires explicit refresh")
    if snapshot.identity.route_status != "bound":
        raise MetadataError("bind the route before using catalog metadata")
    row = _object(parse_metadata(row_json))
    if row.get("id") != snapshot.identity.resolved_model:
        raise MetadataError("catalog model differs from bound resolved model")
    if any(control.name.startswith("catalog.") for control in snapshot.controls):
        raise MetadataError(
            "snapshot already includes a catalog; create a fresh snapshot"
        )
    controls = catalog_controls(row, evidence, schema=schema)
    projection = {
        key: row[key]
        for key in ("id", "reasoning", "reasoning_options", "supported_parameters")
        if key in row
    }
    return CapabilitySnapshot(
        identity=snapshot.identity,
        status=snapshot.status,
        controls=(*snapshot.controls, *controls),
        evidence=(*snapshot.evidence, evidence),
        metadata_json=metadata_text(
            {"native": parse_metadata(snapshot.metadata_json), "catalog": projection}
        ),
        limitations=(
            *snapshot.limitations,
            "Catalog controls describe this model, not per-endpoint effort enums. "
            "Null supported_efforts means no gateway allowlist, not a fabricated list.",
        ),
    )


def observation_from_json(
    identity: CapabilityIdentity,
    payload_json: str,
    *,
    observed_at: str,
    source_url: str | None = None,
    method: str = "native metadata capture",
) -> NativeObservation:
    """Bind a host-supplied capture; callers own truthful capture provenance."""
    parse_metadata(payload_json)
    return NativeObservation(
        identity,
        payload_json,
        make_evidence(
            source_url or NATIVE_SOURCES[identity.harness],
            payload_json,
            observed_at,
            method,
        ),
    )
