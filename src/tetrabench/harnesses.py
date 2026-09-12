"""Validated translations to the four pinned Harbor 0.22 installed adapters."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tetrabench.canonical_json import sha256_hex
from tetrabench.harness_config import (
    MAX_NATIVE_CONFIG_BYTES,
    HarnessConfig,
    NativeConfig,
    ResolvedHarness,
    SealedNativeConfig,
    SealedResource,
)
from tetrabench.native_control import CLAUDE_APPLIED_SETTINGS_VERSIONS

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*\Z")
_CLAUDE_CONTEXT_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*\[1m\]\Z")


def _valid_model_selector(name: str, version: str, value: str) -> bool:
    return bool(
        _MODEL.fullmatch(value)
        or (
            name == "claude-code"
            and version in CLAUDE_APPLIED_SETTINGS_VERSIONS
            and _CLAUDE_CONTEXT_MODEL.fullmatch(value)
        )
    )


_VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}\Z")
_SENSITIVE = re.compile(
    r"api.?key|api.?token|auth.?token|authorization|password|secret|access.?token|"
    r"refresh.?token|id.?token|"
    r"^token$|^key$|private.?key|bearer.?token|^cookie$|^set-cookie$",
    re.I,
)
_FORBIDDEN_ENV = re.compile(
    r"^(AWS_|S3_|TIGRIS_|MODAL_|TETRABENCH_|LD_|DYLD_|BASH_|PYTHON|NODE_|NPM_|XDG_)"
)
_AUTH_ENV = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ZAI_API_KEY",
        "GROQ_API_KEY",
        "XAI_API_KEY",
        "MISTRAL_API_KEY",
        "DEEPSEEK_API_KEY",
        "CEREBRAS_API_KEY",
        "TOGETHER_API_KEY",
    }
)
SUPPORTED_OPENCODE_VERSIONS = ("1.18.29", "1.18.30")
STABLE_VERSIONS = {
    "opencode": "1.18.30",
    "codex": "0.154.0",
    "claude-code": "2.1.269",
    "pi": "0.85.1",
}


def supports_native_controls(name: str, version: str) -> bool:
    """Compatibility is independent of the preferred installation version."""
    return (
        version in CLAUDE_APPLIED_SETTINGS_VERSIONS
        if name == "claude-code"
        else version == STABLE_VERSIONS.get(name)
    )


@dataclass(frozen=True)
class HarnessAdapter:
    name: str
    import_path: str
    package: str
    native_format: str
    options: tuple[str, ...]


_ADAPTERS = {
    "opencode": HarnessAdapter(
        "opencode",
        "tetrabench.harness_agents:ControlledOpenCode",
        "opencode-ai",
        "json",
        ("variant", "title", "agent", "pure"),
    ),
    "codex": HarnessAdapter(
        "codex",
        "tetrabench.harness_agents:ControlledCodex",
        "@openai/codex",
        "toml",
        ("reasoning_effort", "reasoning_summary", "web_search"),
    ),
    "claude-code": HarnessAdapter(
        "claude-code",
        "tetrabench.harness_agents:ControlledClaudeCode",
        "@anthropic-ai/claude-code",
        "json",
        (
            "max_turns",
            "reasoning_effort",
            "max_budget_usd",
            "fallback_model",
            "append_system_prompt",
            "allowed_tools",
            "disallowed_tools",
            "permission_mode",
            "max_thinking_tokens",
            "max_output_tokens",
            "disable_adaptive_thinking",
            "autocompact",
            "disable_auto_compact",
            "tools",
            "system_prompt",
            "system_prompt_file",
            "append_system_prompt_file",
            "agent",
            "agents",
            "mcp_config",
            "setting_sources",
            "strict_mcp_config",
            "disable_slash_commands",
            "no_session_persistence",
            "fork_session",
        ),
    ),
    "pi": HarnessAdapter(
        "pi",
        "tetrabench.harness_agents:ControlledPi",
        "@earendil-works/pi-coding-agent",
        "json",
        (
            "thinking",
            "model_api",
            "tools",
            "exclude_tools",
            "no_tools",
            "no_builtin_tools",
            "offline",
            "no_extensions",
            "no_skills",
            "no_prompt_templates",
            "no_themes",
            "no_context_files",
            "system_prompt",
            "append_system_prompt",
            "extension",
            "skill",
            "prompt_template",
            "no_session",
            "name",
            "session_id",
        ),
    ),
}


def get_harness(name: str) -> HarnessAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError("unknown controlled harness; use tetrabench agents") from None


def registered_harnesses() -> tuple[HarnessAdapter, ...]:
    return tuple(_ADAPTERS.values())


def resolve_authoring_version(name: str, version: str) -> str:
    """Explicit authoring-only registry read; never called by plan/run/replay."""
    if version != "latest":
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("version must be latest or an exact x.y.z")
        return version
    from urllib.parse import quote
    from urllib.request import urlopen

    package = get_harness(name).package
    try:
        with urlopen(
            "https://registry.npmjs.org/" + quote(package, safe="") + "/latest",
            timeout=15,
        ) as response:  # nosec B310
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("native package metadata exceeds limit")
        metadata = json.loads(data)
        concrete = metadata["version"]
        if (
            metadata.get("name") != package
            or not isinstance(concrete, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", concrete)
        ):
            raise ValueError("native registry did not return a stable exact version")
        return concrete
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("cannot resolve stable native package metadata") from None


def supported_environment(name: str) -> tuple[str, ...]:
    if name == "codex":
        return ("OPENAI_API_KEY", "OPENAI_BASE_URL")
    if name == "claude-code":
        return (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
    excluded = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN"}
    return tuple(sorted(_AUTH_ENV - excluded))


def _native_environment_references(
    name: str, native: Any, version: str | None = None
) -> set[str]:
    references: set[str] = set()
    if isinstance(native, dict):
        for key, value in native.items():
            if (
                name == "codex"
                and key in {"env_key", "bearer_token_env_var"}
                and isinstance(value, str)
            ):
                references.add(value)
            if (
                name == "codex"
                and key == "env_http_headers"
                and isinstance(value, dict)
            ):
                references.update(
                    item for item in value.values() if isinstance(item, str)
                )
            if name == "pi" and key == "apiKey" and isinstance(value, str):
                if version == STABLE_VERSIONS["pi"]:
                    references.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", value))
                else:
                    references.add(value)
            if name == "pi" and key == "headers" and isinstance(value, dict):
                references.update(
                    item
                    for item in value.values()
                    if isinstance(item, str) and _VARIABLE.fullmatch(item)
                )
            references.update(_native_environment_references(name, value, version))
    elif isinstance(native, list):
        for value in native:
            references.update(_native_environment_references(name, value, version))
    elif isinstance(native, str):
        if name == "opencode":
            references.update(re.findall(r"\{env:([A-Z][A-Z0-9_]*)\}", native))
        if name == "pi" and version == STABLE_VERSIONS["pi"]:
            references.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", native))
    return references


def _reserved_variable(name: str) -> bool:
    return (
        bool(_FORBIDDEN_ENV.match(name))
        or name
        in {
            "HOME",
            "PATH",
            "SHELL",
            "ENV",
            "BASH_ENV",
            "CDPATH",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "SSL_CERT_FILE",
        }
        or name.startswith(
            ("GIT_", "OPENCODE_", "CODEX_", "PI_CODING_AGENT_", "CLAUDE_")
        )
    )


def _option_details(adapter: HarnessAdapter) -> dict[str, Any]:
    cls = _native_class(adapter.name)
    details = {
        descriptor.kwarg: {
            "type": descriptor.type,
            "choices": descriptor.choices,
            "default": descriptor.default,
        }
        for descriptor in [*cls.CLI_FLAGS, *cls.ENV_VARS]
    }
    if adapter.name == "opencode":
        details["title"] = {"type": "str", "default": "tetrabench", "required": True}
    if adapter.name == "codex":
        details["reasoning_effort"].update(
            default=None, choices=None, capability_source="native model/route discovery"
        )
    if adapter.name == "claude-code":
        details["reasoning_effort"]["choices"] = [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
        details["permission_mode"]["choices"] = [
            "acceptEdits",
            "auto",
            "bypassPermissions",
            "manual",
            "dontAsk",
            "plan",
        ]
    if adapter.name == "pi":
        details["thinking"]["choices"] = [
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
    if adapter.name == "pi":
        details["model_api"] = {
            "type": "str",
            "requires": "base URL reference",
            "choices": [
                "openai-completions",
                "openai-responses",
                "anthropic-messages",
                "google-generative-ai",
                "google-vertex",
            ],
        }
    for name in adapter.options:
        details.setdefault(
            name,
            {
                "type": "bool"
                if name in _BOOL_OPTIONS
                else "int"
                if name in _INT_OPTIONS
                else "str",
                "default": None,
            },
        )
    return {key: details[key] for key in adapter.options}


_BOOL_OPTIONS = {
    "pure",
    "disable_adaptive_thinking",
    "disable_auto_compact",
    "strict_mcp_config",
    "disable_slash_commands",
    "no_session_persistence",
    "fork_session",
    "no_tools",
    "no_builtin_tools",
    "offline",
    "no_extensions",
    "no_skills",
    "no_prompt_templates",
    "no_themes",
    "no_context_files",
    "no_session",
}
_INT_OPTIONS = {"max_turns", "max_thinking_tokens", "max_output_tokens"}
_EMPTY_OPTIONS = {"tools", "setting_sources"}


def capabilities() -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "package": item.package,
            "options": list(item.options),
            "option_details": _option_details(item),
            "credential_variables": list(supported_environment(item.name)),
            "custom_credential_variables": (
                "Non-reserved variables referenced by native configuration"
            ),
            "native_config": item.native_format,
            "native_config_formats": ["json", "toml"]
            if item.name == "codex"
            else ["json", "jsonc"]
            if item.name == "opencode"
            else ["json"],
            "version": "exact x.y.z required",
            "supported_versions": list(SUPPORTED_OPENCODE_VERSIONS)
            if item.name == "opencode"
            else list(CLAUDE_APPLIED_SETTINGS_VERSIONS)
            if item.name == "claude-code"
            else None,
            "minimum_version": "0.74.0" if item.name == "pi" else None,
            "stable_version": STABLE_VERSIONS[item.name],
            "runtime_snapshot_guard": "native-startup (strict-startup optional)",
            "version_evidence": {
                "baseline_frozen_at": "2026-09-11T20:15:24Z",
                "accepted_baseline": STABLE_VERSIONS[item.name],
                "metadata_verified": STABLE_VERSIONS[item.name],
                "native_consumer_tested": [STABLE_VERSIONS[item.name]],
                "model_live_verified": ["1.18.29"] if item.name == "opencode" else [],
                "historical_records": "Old bytes retained; no new live proof implied",
            },
            "discovery": ["native", "isolated"] if item.name != "codex" else ["native"],
            "resources": {
                "references": "resource:DESTINATION",
                "max_files": 128,
                "max_file_bytes": 128 * 1024,
                "max_total_bytes": 512 * 1024,
            },
            "session": {
                "resume_trajectory": True,
                "load_trajectory": item.name in {"codex", "claude-code", "pi"},
            },
            "nested_model_ids": item.name in {"opencode", "pi", "codex"},
            "ancillary_models": ["primary", "native"],
            "limitations": "Known native routing only; no universal billing cap",
        }
        for item in registered_harnesses()
    ]


def _native_class(name: str) -> Any:
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.name import AgentName

    return AgentFactory.get_agent_class(AgentName(name))


def normalized_options(
    spec: HarnessConfig, *, historical: bool = False
) -> dict[str, Any]:
    adapter = get_harness(spec.name)
    options = dict(spec.options)
    descriptors = {
        flag.kwarg: flag
        for flag in [
            *_native_class(spec.name).CLI_FLAGS,
            *_native_class(spec.name).ENV_VARS,
        ]
    }
    cli = {
        flag.cli: flag.kwarg
        for flag in _native_class(spec.name).CLI_FLAGS
        if flag.cli != "-c"
    }
    # Expose each supported native configuration flag without a raw shell escape.
    cli.update({"--" + name.replace("_", "-"): name for name in adapter.options})
    args = iter(spec.args)
    for argument in args:
        flag, equal, value = argument.partition("=")
        if flag in {"-c", "--config"} and spec.name == "codex":
            assignment = value if equal else next(args, "")
            native_key, separator, native_value = assignment.partition("=")
            config_flags = {
                "model_reasoning_effort": "reasoning_effort",
                "model_reasoning_summary": "reasoning_summary",
                "web_search": "web_search",
            }
            if not separator or native_key not in config_flags:
                raise ValueError("unsupported Codex -c assignment")
            key = config_flags[native_key]
            flag, equal, value = (
                "--" + key.replace("_", "-"),
                "=",
                native_value.strip('"'),
            )
        if flag not in cli:
            raise ValueError("unsupported harness argument; use tetrabench agents")
        key = cli[flag]
        if key in options:
            raise ValueError("duplicate/conflicting harness option and argument")
        if key in _BOOL_OPTIONS:
            if equal and value not in {"true", "false"}:
                raise ValueError("boolean harness argument requires true or false")
            options[key] = value != "false" if equal else True
            continue
        if not equal:
            value = next(args, "")
        if (not value and key not in _EMPTY_OPTIONS) or (
            not equal and value.startswith("--")
        ):
            raise ValueError("harness argument requires a value")
        if key in _INT_OPTIONS:
            if not value.isdecimal():
                raise ValueError("harness argument requires a nonnegative integer")
            options[key] = int(value)
        else:
            options[key] = value
    if set(options) - set(adapter.options):
        raise ValueError("unsupported harness option; use tetrabench agents")
    for key, value in options.items():
        if value is None:
            continue
        if key in _BOOL_OPTIONS:
            if type(value) is not bool:
                raise ValueError("harness option requires a boolean")
            continue
        if key in _INT_OPTIONS:
            if (
                type(value) is not int
                or value < 0
                or (key != "max_thinking_tokens" and value == 0)
            ):
                raise ValueError("harness limit must be a positive integer")
        elif (
            not isinstance(value, str)
            or (not value.strip() and key not in _EMPTY_OPTIONS)
            or len(value) > 8192
            or "\x00" in value
        ):
            raise ValueError("harness option requires bounded nonempty text")
        descriptor = descriptors.get(key)
        choices = descriptor.choices if descriptor is not None else None
        if spec.name == "codex" and key == "reasoning_effort":
            # Model/route discovery owns supported efforts. This is only safe
            # native TOML string syntax, not a universal model capability enum.
            if spec.version != STABLE_VERSIONS["codex"] and not re.fullmatch(
                r"[a-z][a-z0-9_-]{0,63}", str(value)
            ):
                raise ValueError("invalid native reasoning effort")
            choices = None
        if (
            spec.name == "claude-code"
            and key == "reasoning_effort"
            and not (
                historical and not supports_native_controls(spec.name, spec.version)
            )
        ):
            choices = (
                ["low", "medium", "high", "xhigh", "max"]
                if spec.version in CLAUDE_APPLIED_SETTINGS_VERSIONS
                else ["low", "medium", "high", "max"]
            )
        if (
            spec.name == "claude-code"
            and key == "permission_mode"
            and spec.version in CLAUDE_APPLIED_SETTINGS_VERSIONS
        ):
            choices = [
                "acceptEdits",
                "auto",
                "bypassPermissions",
                "manual",
                "dontAsk",
                "plan",
            ]
        if spec.name == "pi" and key == "thinking" and spec.version == "0.85.1":
            choices = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
        if choices and value not in choices:
            raise ValueError(f"unsupported {key}; choices: {', '.join(choices)}")
        if key == "max_budget_usd" and not re.fullmatch(
            r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", str(value)
        ):
            raise ValueError("max_budget_usd must be a nonnegative decimal string")
        if key == "fallback_model" and (
            not isinstance(value, str)
            or not _valid_model_selector(spec.name, spec.version, value)
        ):
            raise ValueError("invalid fallback model identifier")
        if (
            key == "fallback_model"
            and isinstance(value, str)
            and "/" in value
            and (
                value.count("/") != 1
                or value.split("/", 1)[0] != spec.model.split("/", 1)[0]
            )
        ):
            raise ValueError(
                "fallback model must use the primary provider without nested IDs"
            )
        if key == "autocompact" and not re.fullmatch(
            r"auto|[1-9][0-9]*(?:[kKmM])?", str(value)
        ):
            raise ValueError("autocompact requires auto or a native token window")
        if key == "autocompact" and value != "auto":
            text = str(value).lower()
            count = int(text.rstrip("km")) * (
                1000 if text.endswith("k") else 1000000 if text.endswith("m") else 1
            )
            if not 100000 <= count <= 1000000:
                raise ValueError("Claude autocompact window must be 100k-1M tokens")
        if key == "setting_sources" and any(
            part not in {"", "user", "project", "local"}
            for part in str(value).split(",")
        ):
            raise ValueError("unsupported Claude setting source")
    if spec.name == "opencode" and spec.ancillary_models == "primary":
        if options.get("title", "tetrabench") is None:
            raise ValueError("controlled OpenCode requires a fixed nonempty title")
        options.setdefault("title", "tetrabench")
    if spec.ancillary_models == "primary" and options.get("fallback_model") not in {
        None,
        spec.model,
        spec.model.split("/", 1)[-1],
    }:
        raise ValueError("fallback_model conflicts with primary ancillary policy")
    return options


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native configuration key")
        result[key] = value
    return result


def parse_native(config: NativeConfig | SealedNativeConfig | None) -> dict[str, Any]:
    if config is None or config.text is None:
        return {}
    try:
        if config.format == "toml":
            value = tomllib.loads(config.text)
        else:
            from jsonc import loads as loads_jsonc

            loader = loads_jsonc if config.format == "jsonc" else json.loads
            value = loader(
                config.text,
                object_pairs_hook=_no_duplicates,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(
            "native_config requires a finite JSON-compatible object/table"
        ) from None
    if not isinstance(value, dict):
        raise ValueError("native_config must contain an object/table")
    return value


def _check_native_secrets(
    value: Any,
    env: dict[str, str],
    name: str,
    depth: int = 0,
    *,
    version: str | None = None,
    mcp: bool = False,
) -> None:
    if depth > 32:
        raise ValueError("native_config nesting exceeds 32")
    if isinstance(value, dict):
        for key, child in value.items():
            if name == "codex" and key in {"experimental_bearer_token", "bearer_token"}:
                raise ValueError(
                    "Codex literal bearer-token fields are unsupported; "
                    "use env_key or bearer_token_env_var with harness.env"
                )
            if name == "codex" and key in {"env_key", "bearer_token_env_var"}:
                if not isinstance(child, str) or child not in env:
                    raise ValueError(
                        "Codex credential selectors must name harness.env variables"
                    )
                continue
            if name == "codex" and key == "env_http_headers":
                if not isinstance(child, dict) or any(
                    not isinstance(item, str) or item not in env
                    for item in child.values()
                ):
                    raise ValueError(
                        "Codex env_http_headers must name harness.env variables"
                    )
                continue
            if key in {"headers", "http_headers"} and isinstance(child, dict):
                for header, text in child.items():
                    if not isinstance(text, str):
                        raise ValueError("native HTTP headers must be strings")
                    references = re.findall(
                        r"\$\{([^}]+)\}|\$([A-Z][A-Z0-9_]*)|\{env:([^}]+)\}", text
                    )
                    credential_header = bool(_SENSITIVE.search(header)) or any(
                        variable in env or _SENSITIVE.search(variable)
                        for groups in references
                        for variable in groups
                        if variable
                    )
                    if (
                        name in {"codex", "claude-code"}
                        and credential_header
                        and not (name == "claude-code" and mcp)
                    ):
                        raise ValueError(
                            "Codex http_headers are literal; "
                            "use env_http_headers for credentials"
                            if name == "codex"
                            else "Claude settings headers cannot interpolate; "
                            "use harness.env authentication"
                        )
                    if (
                        name == "pi"
                        and version != STABLE_VERSIONS["pi"]
                        and credential_header
                        and references
                    ):
                        raise ValueError(
                            "Pi headers require bare environment names for credentials"
                        )
                    if (
                        name == "opencode"
                        and credential_header
                        and ("${" in text or re.search(r"\$[A-Z][A-Z0-9_]*", text))
                    ):
                        raise ValueError(
                            "OpenCode header references use {env:VARIABLE}"
                        )
            if key.lower() in {"env", "auth", "auth.json"}:
                if (
                    key == "env"
                    and mcp
                    and isinstance(child, dict)
                    and all(
                        isinstance(item, str)
                        and item in {f"${{{variable}}}" for variable in env}
                        for item in child.values()
                    )
                ):
                    continue
                raise ValueError(
                    "native auth/env overlays unsupported; use harness.env references"
                )
            if _SENSITIVE.search(key) and child is not None:
                forms = (
                    list(env)
                    if name == "pi" and version != STABLE_VERSIONS["pi"]
                    else [
                        f"{{env:{key}}}" if name == "opencode" else f"${{{key}}}"
                        for key in env
                    ]
                )
                if name == "opencode" and key.lower() == "authorization":
                    forms += [f"Bearer {{env:{variable}}}" for variable in env]
                if key.lower() == "authorization" and (
                    (name == "claude-code" and mcp)
                    or (name == "pi" and version == STABLE_VERSIONS["pi"])
                ):
                    forms += [f"Bearer ${{{variable}}}" for variable in env]
                if child not in forms:
                    raise ValueError(
                        "native credentials must reference harness.env variables"
                    )
            _check_native_secrets(child, env, name, depth + 1, version=version, mcp=mcp)
    elif isinstance(value, list):
        for child in value:
            _check_native_secrets(child, env, name, depth + 1, version=version, mcp=mcp)
    elif isinstance(value, str):
        # File references are sealed and rewritten before constructing a resolved
        # harness. They are never interpolated as credentials.
        # Reject URL credentials/query strings without echoing the supplied value.
        if "://" in value:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "native endpoint URLs cannot contain credentials or query strings"
                )


def validate_harness(
    spec: HarnessConfig, *, historical: bool = False, _check_layers: bool = True
) -> None:
    from tetrabench.auth_config import validate_auth_spec

    validate_auth_spec(
        spec.name, spec.auth, env=spec.env, version=spec.version, model=spec.model
    )
    adapter = get_harness(spec.name)
    if spec.name == "opencode" and spec.version not in SUPPORTED_OPENCODE_VERSIONS:
        raise ValueError(
            "supported controlled OpenCode versions: 1.18.29, 1.18.30 (native --auto); "
            "other versions require adapter verification"
        )
    if (
        not _valid_model_selector(spec.name, spec.version, spec.model)
        or "/" not in spec.model
    ):
        raise ValueError(
            "harness.model requires a shell-safe provider/model identifier"
        )
    if any(not part for part in spec.model.split("/")):
        raise ValueError("harness.model contains an empty component")
    if (
        spec.name == "claude-code"
        or (spec.name == "codex" and spec.version != STABLE_VERSIONS["codex"])
    ) and spec.model.count("/") > 1:
        raise ValueError(
            "this Harbor adapter truncates nested model IDs; use provider/model"
        )
    if spec.name == "pi" and tuple(map(int, spec.version.split("."))) < (0, 74, 0):
        raise ValueError("controlled Pi requires Earendil pi-coding-agent >=0.74.0")
    native = parse_native(spec.native_config)
    native_references = _native_environment_references(spec.name, native, spec.version)
    from tetrabench.harness_config import ResourceSource, SealedResource

    for resource in spec.resources:
        if isinstance(resource, SealedResource):
            native_references.update(
                re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", resource.text)
            )
            native_references.update(
                re.findall(r"\{env:([A-Z][A-Z0-9_]*)\}", resource.text)
            )
    pending_path = (
        spec.native_config is not None and spec.native_config.path is not None
    ) or any(isinstance(item, ResourceSource) for item in spec.resources)
    for key, reference in spec.env.items():
        match = _REFERENCE.fullmatch(reference)
        if not _VARIABLE.fullmatch(key) or match is None:
            raise ValueError("harness.env values must be ${VARIABLE} references")
        conventional = key in supported_environment(spec.name) or (
            historical
            and spec.name == "pi"
            and spec.version != STABLE_VERSIONS["pi"]
            and key == "ANTHROPIC_OAUTH_TOKEN"
        )
        if (
            (
                not conventional
                and (
                    _reserved_variable(key)
                    or (key not in native_references and not pending_path)
                )
            )
            or _FORBIDDEN_ENV.match(match[1])
            or match[1] in {"HOME", "PATH", "SHELL", "BASH_ENV", "ENV"}
        ):
            raise ValueError("reserved or unsupported harness credential variable")
    if "ANTHROPIC_API_KEY" in spec.env and "ANTHROPIC_AUTH_TOKEN" in spec.env:
        raise ValueError("choose one Anthropic authentication mechanism")
    if (
        spec.name == "claude-code"
        and "CLAUDE_CODE_OAUTH_TOKEN" in spec.env
        and any(
            key in spec.env for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        )
    ):
        raise ValueError("choose API credentials or Claude OAuth, not both")
    options = normalized_options(spec, historical=historical)
    old_options = {
        "opencode": {"variant", "title"},
        "codex": {"reasoning_effort", "reasoning_summary", "web_search"},
        "claude-code": {
            "max_turns",
            "reasoning_effort",
            "max_budget_usd",
            "fallback_model",
            "append_system_prompt",
            "allowed_tools",
            "disallowed_tools",
            "permission_mode",
            "max_thinking_tokens",
        },
        "pi": {"thinking", "model_api"},
    }
    if not supports_native_controls(spec.name, spec.version) and (
        set(options) - old_options[spec.name]
        or spec.discovery
        or spec.session
        or spec.resources
    ):
        raise ValueError("new native controls require a verified native pin")
    if spec.name == "codex" and spec.discovery == "isolated":
        raise ValueError(
            "Codex isolated discovery is not advertised; use native config"
        )
    if spec.discovery == "isolated" and "setting_sources" in options:
        raise ValueError("setting_sources conflicts with isolated discovery")
    if spec.discovery == "isolated" and any(
        options.get(key) is False
        for key in (
            "no_extensions",
            "no_skills",
            "no_prompt_templates",
            "no_themes",
            "no_context_files",
            "strict_mcp_config",
        )
    ):
        raise ValueError("native discovery option conflicts with isolated discovery")
    if options.get("autocompact") and options.get("disable_auto_compact"):
        raise ValueError("autocompact window conflicts with disabled auto compaction")
    if (
        options.get("system_prompt") is not None
        and options.get("system_prompt_file") is not None
    ):
        raise ValueError("choose one system prompt source")
    if spec.session:
        if spec.session.load_trajectory and spec.name not in {
            "codex",
            "claude-code",
            "pi",
        }:
            raise ValueError(
                "Harbor load_trajectory is supported for Codex, Claude Code and Pi"
            )
        if (
            spec.session.resume_trajectory
            and (options.get("no_session") or options.get("no_session_persistence"))
        ) or (
            spec.name == "pi"
            and spec.session.load_trajectory
            and options.get("no_session")
        ):
            raise ValueError("session resume requires native persistence")
        if (
            spec.name == "pi"
            and spec.session.load_trajectory
            and not spec.session.load_trajectory.endswith(".jsonl")
        ):
            raise ValueError("Pi imports native JSONL sessions, not ATIF")
    if options.get("session_id") is not None:
        if spec.session and (
            spec.session.resume_trajectory or spec.session.load_trajectory
        ):
            raise ValueError("session_id conflicts with Harbor trajectory continuation")
        import uuid

        try:
            uuid.UUID(str(options["session_id"]))
        except ValueError:
            raise ValueError("native session_id must be a UUID") from None
    if options.get("fork_session") and not (
        spec.session
        and (spec.session.resume_trajectory or spec.session.load_trajectory)
    ):
        raise ValueError("fork_session requires a resumed or imported session")
    config = spec.native_config
    if (
        config
        and config.format != adapter.native_format
        and not (spec.name == "codex" and config.format == "json")
        and not (spec.name == "opencode" and config.format == "jsonc")
    ):
        raise ValueError("native_config format is unsupported by this harness")
    _check_native_secrets(native, spec.env, spec.name, version=spec.version)
    if spec.name == "codex":
        providers = native.get("model_providers", {})
        if not isinstance(providers, dict):
            raise ValueError("Codex model_providers must be a table")
        for provider in providers.values():
            if not isinstance(provider, dict) or (
                "env_key" in provider and provider["env_key"] not in spec.env
            ):
                raise ValueError(
                    "Codex provider env_key must name a declared harness.env variable"
                )
        if "OPENAI_BASE_URL" in spec.env and "openai_base_url" in native:
            raise ValueError("duplicate Codex base URL configuration")
    if "model" in native and native["model"] not in {
        spec.model,
        spec.model.split("/", 1)[-1],
    }:
        raise ValueError("native model conflicts with harness.model")
    mapping = {
        "reasoning_effort": "model_reasoning_effort",
        "reasoning_summary": "model_reasoning_summary",
        "web_search": "web_search",
    }
    for key, native_key in mapping.items():
        if spec.name == "codex" and key in options and native_key in native:
            raise ValueError("duplicate native configuration and harness option")
    if spec.name == "pi":
        has_endpoint = any(key.endswith("BASE_URL") for key in spec.env)
        if bool(options.get("model_api")) != has_endpoint:
            raise ValueError(
                "Pi endpoints require model_api and a base URL reference together"
            )
        if options.get("model_api") is not None and options["model_api"] not in {
            "openai-completions",
            "openai-responses",
            "anthropic-messages",
            "google-generative-ai",
            "google-vertex",
        }:
            raise ValueError("unsupported Pi model_api")
        if set(native) - {"settings", "models"}:
            raise ValueError("Pi native_config accepts settings and models objects")
        for key in ("settings", "models"):
            if key in native and not isinstance(native[key], dict):
                raise ValueError("Pi settings/models must be objects")
        settings = native.get("settings", {})
        for key, expected in {
            "defaultModel": spec.model.split("/", 1)[-1],
            "defaultProvider": spec.model.split("/", 1)[0],
        }.items():
            if key in settings and settings[key] != expected:
                raise ValueError("Pi native model conflicts with harness.model")
        if options.get("thinking") is not None and "defaultThinkingLevel" in settings:
            raise ValueError("duplicate Pi thinking configuration")
        if "models" in native and (
            "model_api" in options or any(key.endswith("BASE_URL") for key in spec.env)
        ):
            raise ValueError(
                "Pi models file conflicts with Harbor endpoint configuration"
            )
    if spec.ancillary_models == "primary":
        if spec.name == "opencode":
            if native.get("small_model", spec.model) != spec.model:
                raise ValueError("small_model conflicts with primary ancillary policy")
            agents, modes = native.get("agent", {}), native.get("mode", {})
            if not isinstance(agents, dict) or not isinstance(modes, dict):
                raise ValueError("native agent/mode configuration must be objects")
            # OpenCode 1.18.29 migrates mode after agent, deep-merging each role.
            models = {}
            for layer in (agents, modes):
                for role, value in layer.items():
                    if not isinstance(value, dict):
                        raise ValueError("native agent/mode entries must be objects")
                    if "model" in value:
                        models[role] = value["model"]
            if any(value != spec.model for value in models.values()):
                raise ValueError(
                    "native agent model conflicts with primary ancillary policy"
                )
        if spec.name == "codex":
            layers = [native]
            profiles = native.get("profiles", {})
            if isinstance(profiles, dict):
                layers.extend(
                    profile
                    for profile in profiles.values()
                    if isinstance(profile, dict)
                )
            for layer in layers:
                roles = layer.get("agents", {})
                if isinstance(roles, dict) and any(
                    isinstance(role, dict) and role.get("config_file") is not None
                    for role in roles.values()
                ):
                    raise ValueError(
                        "Codex role config_file can override model selection; "
                        "use ancillary_models='native' for role files"
                    )

    if _check_layers:
        validate_explicit_auth_configuration(spec)


@dataclass(frozen=True)
class NativeConfigurationLayer:
    source: str
    config: dict[str, Any]


@dataclass(frozen=True)
class NativeConfigurationLayers:
    """Sealed files in native load order, not observed native resolution."""

    layers: tuple[NativeConfigurationLayer, ...]
    effective_config: dict[str, Any]
    config_directory: str | None
    resources: tuple[SealedResource, ...]
    executable_resources: tuple[str, ...]

    @property
    def main_config(self) -> dict[str, Any]:
        return self.layers[0].config


def _merge_native_layer(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    from copy import deepcopy

    result = deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_native_layer(result[key], value)
        else:
            result[key] = deepcopy(value)
    if isinstance(left.get("instructions"), list) and isinstance(
        right.get("instructions"), list
    ):
        if any(
            not isinstance(item, str)
            for item in left["instructions"] + right["instructions"]
        ):
            raise ValueError("native instructions must be path strings")
        result["instructions"] = list(
            dict.fromkeys(left["instructions"] + right["instructions"])
        )
    return result


def native_configuration_layers(
    harness: HarnessConfig | ResolvedHarness,
    *,
    base_config: dict[str, Any] | None = None,
) -> NativeConfigurationLayers:
    """Shared description for execution/inspection, in native load order.

    base_config supplies Harbor's generated main config when available. Resources
    retain their bytes; native consumers still perform the actual merge.
    """
    from tetrabench.harness_config import SealedResource
    from tetrabench.resources import AGENT_RESOURCE_ROOT

    main = parse_native(harness.native_config) if base_config is None else base_config
    if harness.name == "opencode" and base_config is None:
        provider, model = harness.model.split("/", 1)
        main = _merge_native_layer(
            {"provider": {provider: {"models": {model: {}}}}}, main
        )
        if harness.ancillary_models == "primary":
            main["small_model"] = harness.model
    layers = [NativeConfigurationLayer("native_config", main)]
    directory = None
    resources = {
        item.destination: item
        for item in harness.resources
        if isinstance(item, SealedResource)
    }
    if harness.name == "opencode" and any(
        name.startswith("opencode/") for name in resources
    ):
        directory = AGENT_RESOURCE_ROOT + "/opencode"
        for name in ("opencode/opencode.json", "opencode/opencode.jsonc"):
            if name in resources:
                item = resources[name]
                layer = parse_native(
                    NativeConfig(
                        format="jsonc" if name.endswith(".jsonc") else "json",
                        text=item.text,
                    )
                )
                layers.append(NativeConfigurationLayer(name, layer))
        seen: set[tuple[str, str]] = set()
        for group in (("agent", "agents"), ("mode", "modes")):
            for name, item in sorted(resources.items()):
                parts = name.split("/")
                if (
                    len(parts) < 3
                    or parts[0] != "opencode"
                    or parts[1] not in group
                    or not name.endswith(".md")
                ):
                    continue
                if group[0] == "mode" and len(parts) != 3:
                    continue
                import yaml

                if not item.text.startswith("---\n"):
                    raise ValueError("native agent resources require YAML frontmatter")
                frontmatter, separator, body = item.text[4:].partition("\n---")
                value = yaml.safe_load(frontmatter)
                if not separator or not isinstance(value, dict):
                    raise ValueError("invalid native agent resource frontmatter")
                role = value.get("name", "/".join(parts[2:])[:-3])
                if not isinstance(role, str) or (group[0], role) in seen:
                    raise ValueError("ambiguous native resource agent definition")
                seen.add((group[0], role))
                value = dict(value, name=role, prompt=body.strip())
                if group[0] == "mode":
                    value["mode"] = "primary"
                layers.append(NativeConfigurationLayer(name, {"agent": {role: value}}))
    effective: dict[str, Any] = {}
    for layer in layers:
        effective = _merge_native_layer(effective, layer.config)
    if harness.name == "opencode" and isinstance(effective.get("mode"), dict):
        for role, value in effective["mode"].items():
            if not isinstance(value, dict):
                raise ValueError("native mode entries must be objects")
            effective = _merge_native_layer(
                effective, {"agent": {role: dict(value, mode="primary")}}
            )
    executable = []
    for name, resource in resources.items():
        if name.endswith((".js", ".mjs", ".ts", ".py", ".sh")):
            executable.append(name)
        elif Path(name).name == "package.json":
            package = parse_native(NativeConfig(text=resource.text))
            if package.get("scripts"):
                executable.append(name)
    return NativeConfigurationLayers(
        tuple(layers),
        effective,
        directory,
        tuple(resources.values()),
        tuple(executable),
    )


def validate_explicit_auth_configuration(
    harness: HarnessConfig | ResolvedHarness,
) -> None:
    """Validate every sealed native layer offline, before credential handoff."""
    from tetrabench.auth_config import validate_native_auth_config
    from tetrabench.harness_config import ResourceSource

    description = native_configuration_layers(harness)
    options = dict(harness.options)

    def auth_layer(config: dict[str, Any]) -> None:
        validate_native_auth_config(config)
        if harness.name == "opencode":
            _validate_opencode_auth_transport(config)
        stack = [config]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if (
                        key == "model_provider"
                        and child != harness.model.split("/", 1)[0]
                    ):
                        raise ValueError(
                            "native provider layer conflicts with explicit auth"
                        )
                    stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)

    if harness.auth is not None:
        for resource in harness.resources:
            if isinstance(resource, ResourceSource):
                continue  # Authoring only; execution requires ResolvedHarness.
            if resource.destination.endswith((".json", ".jsonc", ".toml")):
                if harness.session and resource.destination == (
                    harness.session.load_trajectory or ""
                ).removeprefix("resource:"):
                    continue
                config = parse_native(
                    NativeConfig.model_validate(
                        {
                            "format": Path(resource.destination).suffix[1:],
                            "text": resource.text,
                        }
                    )
                )
                auth_layer(config)
    for layer in description.layers:
        if harness.auth is not None:
            auth_layer(layer.config)
        if layer.source != "native_config":
            candidate = HarnessConfig.model_construct(
                name=harness.name,
                version=harness.version,
                model=harness.model,
                options=options,
                args=getattr(harness, "args", []),
                env=harness.env,
                native_config=NativeConfig(text=json.dumps(layer.config)),
                ancillary_models=harness.ancillary_models,
                resources=list(harness.resources),
                discovery=harness.discovery,
                session=harness.session,
                auth=harness.auth,
            )
            validate_harness(candidate, _check_layers=False)
    if harness.name == "opencode":
        variant = options.get("variant")
        provider, model = harness.model.split("/", 1)
        providers = description.effective_config.get("provider", {})
        definition = providers
        for key in (provider, "models", model):
            definition = definition.get(key, {}) if isinstance(definition, dict) else {}
        variants = (
            definition.get("variants", {}) if isinstance(definition, dict) else {}
        )
        choice = (
            variants.get(variant)
            if isinstance(variants, dict) and isinstance(variant, str)
            else None
        )
        if isinstance(choice, dict) and choice.get("disabled") is True:
            raise ValueError(
                "selected native variant is disabled by a sealed config layer"
            )


def _validate_opencode_auth_transport(config: dict[str, Any]) -> None:
    """ConfigProviderV1.Info/Model transport selectors at OpenCode 1.18.30.

    provider.api/npm and models.*.provider.api/npm become model.api.url/npm,
    independently of options.baseURL. Native authenticated routes must retain
    their built-in transport, including the OAuth fetch wrapper's path handling.
    """
    providers = config.get("provider", {})
    if not isinstance(providers, dict):
        raise ValueError("native providers must be objects")
    for provider in providers.values():
        if not isinstance(provider, dict):
            raise ValueError("native provider configuration must be an object")
        if {"api", "npm"} & provider.keys():
            raise ValueError("explicit auth refuses OpenCode endpoint/SDK overrides")
        models = provider.get("models", {})
        if not isinstance(models, dict):
            raise ValueError("native provider models must be objects")
        for model in models.values():
            if not isinstance(model, dict):
                raise ValueError("native model configuration must be an object")
            transport = model.get("provider", {})
            if not isinstance(transport, dict):
                raise ValueError("native model provider must be an object")
            if {"api", "npm"} & transport.keys():
                raise ValueError(
                    "explicit auth refuses OpenCode model endpoint/SDK overrides"
                )


def read_config_text(path: Path) -> str:
    """Seal one bounded regular UTF-8 file; never return an unresolved path."""
    from tetrabench.context import seal_context
    from tetrabench.models import ContextConfig, ContextFileSpec

    try:
        sealed = seal_context(
            path.parent,
            ContextConfig(
                files=[ContextFileSpec(source=path.name, destination=path.name)],
                max_files=1,
                max_file_bytes=MAX_NATIVE_CONFIG_BYTES,
                max_total_bytes=MAX_NATIVE_CONFIG_BYTES,
            ),
        )
        return sealed.files[0].content.decode("utf-8")
    except (OSError, UnicodeError):
        raise ValueError("cannot seal native configuration file") from None


def prepare_harness(spec: HarnessConfig, base: Path) -> HarnessConfig:
    """Seal resources/configuration without resolving authoring auth selectors."""
    from tetrabench.resources import prepare_resources

    native = spec.native_config
    native_base = base
    sealed = None
    if native:
        text = native.text
        if native.path is not None:
            path = Path(native.path).expanduser()
            path = path if path.is_absolute() else base / path
            native_base = path.parent
            text = read_config_text(path)
        if text is None:
            raise ValueError("native configuration must resolve to text")
        sealed = SealedNativeConfig(
            format=native.format, text=text, sha256=sha256_hex(text.encode())
        )
    resources, native_value, options = prepare_resources(
        spec,
        parse_native(sealed),
        normalized_options(spec),
        base,
        native_base=native_base,
    )
    if resources and native_value != parse_native(sealed):
        text = json.dumps(native_value, ensure_ascii=False, allow_nan=False)
        sealed = SealedNativeConfig(
            format="json", text=text, sha256=sha256_hex(text.encode())
        )
    prepared = HarnessConfig(
        name=spec.name,
        version=spec.version,
        model=spec.model,
        options=options,
        env=spec.env,
        native_config=NativeConfig(format=sealed.format, text=sealed.text)
        if sealed
        else None,
        ancillary_models=spec.ancillary_models,
        resources=list(resources),
        discovery=spec.discovery,
        session=spec.session,
        auth=spec.auth,
        capability_snapshot=spec.capability_snapshot,
    )
    from tetrabench.harness_config import validate_prepared_resources

    validate_prepared_resources(prepared)
    return prepared


def seal_harness(spec: HarnessConfig, base: Path) -> ResolvedHarness:
    from tetrabench.auth_config import ProfileAuthSpec

    if isinstance(spec.auth, ProfileAuthSpec):
        raise ValueError("resolve the selected auth profile before sealing a run")
    prepared = prepare_harness(spec, base)
    fields = prepared.model_dump(mode="python", exclude={"args", "native_config"})
    native = prepared.native_config
    fields["native_config"] = (
        SealedNativeConfig(
            format=native.format,
            text=native.text,
            sha256=sha256_hex(native.text.encode()),
        )
        if native is not None and native.text is not None
        else None
    )
    resolved = ResolvedHarness.model_validate(fields)
    # Prove the real native constructor accepts the sealed translation before
    # reserving output or contacting a provider. Do not resolve credential refs.
    import tempfile

    from harbor.agents.factory import AgentFactory

    from tetrabench.resources import materialize_resources

    with tempfile.TemporaryDirectory(prefix="tetrabench-harness-") as temporary:
        if resolved.resources:
            materialize_resources(resolved.resources, Path(temporary) / "resources")
        config = compile_agent_config(
            resolved, resource_directory=Path(temporary) / "resources"
        )
        AgentFactory.create_agent_from_import_path(
            config.import_path,
            logs_dir=Path("/tetrabench-preflight-unused"),
            model_name=resolved.model,
            extra_env={},
            load_trajectory=config.load_trajectory,
            **config.kwargs,
        )
    return resolved


def compile_agent_config(
    spec: ResolvedHarness, *, resource_directory: Path | None = None
) -> Any:
    """Reconstruct native configuration without reading or writing resource files."""
    from harbor.models.trial.config import AgentConfig

    adapter = get_harness(spec.name)
    session_options: dict[str, Any] = {}
    if spec.resources and resource_directory is not None:
        skill_directories = sorted(
            {
                item.destination.split("/")[1]
                for item in spec.resources
                if item.destination.startswith("skills/")
                and item.destination.count("/") >= 2
            }
        )
        if skill_directories:
            session_options["skills"] = [
                str(resource_directory / "skills" / name) for name in skill_directories
            ]
    if spec.session:
        session_options["resume_trajectory"] = spec.session.resume_trajectory
        if spec.session.load_trajectory and resource_directory is not None:
            session_options["load_trajectory"] = str(
                resource_directory
                / spec.session.load_trajectory.removeprefix("resource:")
            )
    return AgentConfig(
        import_path=adapter.import_path,
        model_name=spec.model,
        env=dict(spec.env),
        kwargs={"version": spec.version, "harness": spec.model_dump(mode="json")},
        **session_options,
    )


def validate_credentials(spec: ResolvedHarness | None) -> None:
    """Check host references without resolving them into a persisted config."""
    if spec is None:
        return
    validate_credential_configuration(spec)
    if spec.auth:
        from tetrabench.auth_config import EnvAuthReference

        if isinstance(spec.auth.reference, EnvAuthReference) and not os.environ.get(
            spec.auth.reference.name
        ):
            from tetrabench.diagnostics import missing_credentials

            raise missing_credentials([spec.auth.reference.name])
    missing = [
        key for key, value in spec.env.items() if not os.environ.get(value[2:-1])
    ]
    if missing:
        from tetrabench.diagnostics import missing_credentials

        raise missing_credentials(missing)


def validate_credential_configuration(spec: ResolvedHarness | None) -> None:
    """Require deliberate auth selection without consulting a remote Secret."""
    if spec is None:
        return
    if spec.auth is not None:
        return

    def has_endpoint(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        return any(
            (
                key in {"baseURL", "baseUrl", "base_url", "openai_base_url"}
                and isinstance(child, str)
                and bool(child)
            )
            or has_endpoint(child)
            for key, child in value.items()
        )

    if not spec.env and not has_endpoint(parse_native(spec.native_config)):
        raise ValueError(
            "controlled harness requires harness.env credential references; "
            "anonymous access requires an explicit native endpoint; "
            "ambient login is not copied"
        )
