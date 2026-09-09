"""Validated translations to the four pinned Harbor 0.22 installed adapters."""

from __future__ import annotations

import json
import os
import re
import stat
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
)

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*\Z")
_VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}\Z")
_SENSITIVE = re.compile(
    r"api.?key|api.?token|auth.?token|authorization|password|secret|access.?token|"
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
SUPPORTED_OPENCODE_VERSIONS = ("1.18.29",)


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
        ("variant", "title"),
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
        ),
    ),
    "pi": HarnessAdapter(
        "pi",
        "tetrabench.harness_agents:ControlledPi",
        "@earendil-works/pi-coding-agent",
        "json",
        ("thinking", "model_api"),
    ),
}


def get_harness(name: str) -> HarnessAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError("unknown controlled harness; use tetrabench agents") from None


def registered_harnesses() -> tuple[HarnessAdapter, ...]:
    return tuple(_ADAPTERS.values())


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
    excluded = {"CLAUDE_CODE_OAUTH_TOKEN"}
    if name != "pi":
        excluded.add("ANTHROPIC_OAUTH_TOKEN")
    return tuple(sorted(_AUTH_ENV - excluded))


def _native_environment_references(name: str, native: Any) -> set[str]:
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
                references.add(value)
            if name == "pi" and key == "headers" and isinstance(value, dict):
                references.update(
                    item
                    for item in value.values()
                    if isinstance(item, str) and _VARIABLE.fullmatch(item)
                )
            references.update(_native_environment_references(name, value))
    elif isinstance(native, list):
        for value in native:
            references.update(_native_environment_references(name, value))
    elif isinstance(native, str):
        if name == "opencode":
            references.update(re.findall(r"\{env:([A-Z][A-Z0-9_]*)\}", native))
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
        details["reasoning_effort"]["choices"] = [
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
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
    return details


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
            else ["json"],
            "version": "exact x.y.z required",
            "supported_versions": list(SUPPORTED_OPENCODE_VERSIONS)
            if item.name == "opencode"
            else None,
            "minimum_version": "0.74.0" if item.name == "pi" else None,
            "nested_model_ids": item.name in {"opencode", "pi"},
            "ancillary_models": ["primary", "native"],
            "limitations": "Known native routing only; no universal billing cap",
        }
        for item in registered_harnesses()
    ]


def _native_class(name: str) -> Any:
    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.name import AgentName

    return AgentFactory.get_agent_class(AgentName(name))


def normalized_options(spec: HarnessConfig) -> dict[str, Any]:
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
        if not equal:
            value = next(args, "")
        if not value or (not equal and value.startswith("--")):
            raise ValueError("harness argument requires a value")
        if key in {"max_turns", "max_thinking_tokens"}:
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
        if key in {"max_turns", "max_thinking_tokens"}:
            if (
                type(value) is not int
                or value < 0
                or (key == "max_turns" and value == 0)
            ):
                raise ValueError("harness limit must be a positive integer")
        elif (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 8192
            or "\x00" in value
        ):
            raise ValueError("harness option requires bounded nonempty text")
        descriptor = descriptors.get(key)
        if (
            spec.name == "codex"
            and key == "reasoning_effort"
            and value
            not in {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
            }
        ):
            raise ValueError("unsupported Codex reasoning_effort")
        if (
            descriptor is not None
            and descriptor.choices
            and value not in descriptor.choices
        ):
            raise ValueError(
                f"unsupported {key}; choices: {', '.join(descriptor.choices)}"
            )
        if key == "max_budget_usd" and not re.fullmatch(
            r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", str(value)
        ):
            raise ValueError("max_budget_usd must be a nonnegative decimal string")
        if key == "fallback_model" and (
            not isinstance(value, str) or not _MODEL.fullmatch(value)
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
    if spec.name == "opencode":
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
            value = json.loads(
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
    value: Any, env: dict[str, str], name: str, depth: int = 0
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
                    if name in {"codex", "claude-code"} and credential_header:
                        raise ValueError(
                            "Codex http_headers are literal; "
                            "use env_http_headers for credentials"
                            if name == "codex"
                            else "Claude settings headers cannot interpolate; "
                            "use harness.env authentication"
                        )
                    if name == "pi" and credential_header and references:
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
                raise ValueError(
                    "native auth/env overlays unsupported; use harness.env references"
                )
            if _SENSITIVE.search(key) and child is not None:
                forms = (
                    list(env)
                    if name == "pi"
                    else [
                        f"{{env:{key}}}" if name == "opencode" else f"${{{key}}}"
                        for key in env
                    ]
                )
                if name == "opencode" and key.lower() == "authorization":
                    forms += [f"Bearer {{env:{variable}}}" for variable in env]
                if child not in forms:
                    raise ValueError(
                        "native credentials must reference harness.env variables"
                    )
            _check_native_secrets(child, env, name, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_native_secrets(child, env, name, depth + 1)
    elif isinstance(value, str):
        if "{file:" in value or value.startswith("~/"):
            raise ValueError("native_config cannot refer to unresolved host files")
        # Reject URL credentials/query strings without echoing the supplied value.
        if "://" in value:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "native endpoint URLs cannot contain credentials or query strings"
                )


def validate_harness(spec: HarnessConfig) -> None:
    adapter = get_harness(spec.name)
    if spec.name == "opencode" and spec.version not in SUPPORTED_OPENCODE_VERSIONS:
        raise ValueError(
            "supported controlled OpenCode version: 1.18.29 (native --auto); "
            "other versions require adapter verification"
        )
    if not _MODEL.fullmatch(spec.model) or "/" not in spec.model:
        raise ValueError(
            "harness.model requires a shell-safe provider/model identifier"
        )
    if any(not part for part in spec.model.split("/")):
        raise ValueError("harness.model contains an empty component")
    if spec.name in {"codex", "claude-code"} and spec.model.count("/") > 1:
        raise ValueError(
            "this Harbor adapter truncates nested model IDs; use provider/model"
        )
    if spec.name == "pi" and tuple(map(int, spec.version.split("."))) < (0, 74, 0):
        raise ValueError("controlled Pi requires Earendil pi-coding-agent >=0.74.0")
    native = parse_native(spec.native_config)
    native_references = _native_environment_references(spec.name, native)
    pending_path = (
        spec.native_config is not None and spec.native_config.path is not None
    )
    for key, reference in spec.env.items():
        match = _REFERENCE.fullmatch(reference)
        if not _VARIABLE.fullmatch(key) or match is None:
            raise ValueError("harness.env values must be ${VARIABLE} references")
        conventional = key in supported_environment(spec.name)
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
    options = normalized_options(spec)
    config = spec.native_config
    if (
        config
        and config.format != adapter.native_format
        and not (spec.name == "codex" and config.format == "json")
    ):
        raise ValueError("native_config format is unsupported by this harness")
    _check_native_secrets(native, spec.env, spec.name)
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


def read_config_text(path: Path) -> str:
    """Seal one bounded regular UTF-8 file; never return an unresolved path."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > MAX_NATIVE_CONFIG_BYTES
            ):
                raise ValueError("native configuration must be a bounded regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_NATIVE_CONFIG_BYTES + 1)
            after = os.fstat(fd)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if (
                any(getattr(before, key) != getattr(after, key) for key in fields)
                or len(data) > MAX_NATIVE_CONFIG_BYTES
            ):
                raise ValueError("native configuration changed while sealing")
            return data.decode("utf-8")
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        raise ValueError("cannot seal native configuration file") from None


def seal_harness(spec: HarnessConfig, base: Path) -> ResolvedHarness:
    native = spec.native_config
    sealed = None
    if native:
        text = native.text
        if native.path is not None:
            path = Path(native.path).expanduser()
            text = read_config_text(path if path.is_absolute() else base / path)
        if text is None:
            raise ValueError("native configuration must resolve to text")
        sealed = SealedNativeConfig(
            format=native.format, text=text, sha256=sha256_hex(text.encode())
        )
    resolved = ResolvedHarness(
        name=spec.name,
        version=spec.version,
        model=spec.model,
        options=normalized_options(spec),
        env=spec.env,
        native_config=sealed,
        ancillary_models=spec.ancillary_models,
    )
    # Prove the real native constructor accepts the sealed translation before
    # reserving output or contacting a provider. Do not resolve credential refs.
    from harbor.agents.factory import AgentFactory

    config = compile_agent_config(resolved)
    AgentFactory.create_agent_from_import_path(
        config.import_path,
        logs_dir=Path("/tetrabench-preflight-unused"),
        model_name=resolved.model,
        extra_env={},
        **config.kwargs,
    )
    return resolved


def compile_agent_config(spec: ResolvedHarness) -> Any:
    from harbor.models.trial.config import AgentConfig

    adapter = get_harness(spec.name)
    return AgentConfig(
        import_path=adapter.import_path,
        model_name=spec.model,
        env=dict(spec.env),
        kwargs={"version": spec.version, "harness": spec.model_dump(mode="json")},
    )


def validate_credentials(spec: ResolvedHarness | None) -> None:
    """Check host references without resolving them into a persisted config."""
    if spec is None:
        return
    validate_credential_configuration(spec)
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
