"""Bounded, secret-free run resources, sealed separately from task fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tetrabench.canonical_json import sha256_hex
from tetrabench.harness_config import HarnessConfig, ResourceSource, SealedResource
from tetrabench.storage import validate_logical_path

AGENT_RESOURCE_ROOT = "/tmp/tetrabench-resources"  # nosec B108
MAX_RESOURCE_FILES = 128
MAX_RESOURCE_BYTES = 512 * 1024
MAX_RESOURCE_FILE_BYTES = 128 * 1024
_TYPES = {
    ".md",
    ".txt",
    ".json",
    ".jsonc",
    ".jsonl",
    ".toml",
    ".yaml",
    ".yml",
    ".js",
    ".mjs",
    ".ts",
    ".py",
    ".sh",
}
_SECRET_NAME = re.compile(
    r"(?:^|[._-])(?:auth|credentials?|secrets?|tokens?|password|private[-_]?key)(?:[._-]|$)",
    re.I,
)


def validate_resource_name(name: str) -> None:
    validate_logical_path(name)
    if any(
        _SECRET_NAME.search(part)
        or part.startswith(".env")
        or part in {".ssh", ".aws", ".claude.json", "id_rsa", "id_ed25519"}
        for part in name.split("/")
    ):
        raise ValueError("credential/auth files cannot be harness resources")
    if Path(name).suffix.lower() not in _TYPES:
        raise ValueError("unsupported harness resource file type")


def validate_resource(resource: SealedResource) -> None:
    validate_resource_name(resource.destination)
    data = resource.text.encode()
    if len(data) > MAX_RESOURCE_FILE_BYTES or sha256_hex(data) != resource.sha256:
        raise ValueError("harness resource size or digest mismatch")
    if "\x00" in resource.text or "-----BEGIN " in resource.text:
        raise ValueError("binary/key material cannot be a harness resource")


def validate_resources(resources: list[SealedResource]) -> None:
    from tetrabench.models import validate_context_destinations

    validate_context_destinations(item.destination for item in resources)
    if (
        len(resources) > MAX_RESOURCE_FILES
        or sum(len(item.text.encode()) for item in resources) > MAX_RESOURCE_BYTES
    ):
        raise ValueError("harness resource bundle exceeds file/byte limits")


def validate_resource_contents(
    resources: list[SealedResource],
    env: dict[str, str],
    name: str,
    version: str | None = None,
    *,
    primary_model: str | None = None,
    session_file: str | None = None,
) -> None:
    from tetrabench.harness_config import NativeConfig
    from tetrabench.harnesses import _check_native_secrets, parse_native

    for item in resources:
        suffix = Path(item.destination).suffix
        values = []
        if suffix in {".json", ".jsonc", ".toml"}:
            values = [
                parse_native(
                    NativeConfig.model_validate(
                        {"format": suffix[1:], "text": item.text}
                    )
                )
            ]
        elif suffix == ".jsonl":
            try:
                values = [
                    json.loads(line) for line in item.text.splitlines() if line.strip()
                ]
            except ValueError:
                raise ValueError("native session resource is not valid JSONL") from None
        elif suffix in {".yaml", ".yml"}:
            import yaml

            try:
                values = [yaml.safe_load(item.text)]
            except yaml.YAMLError:
                raise ValueError("invalid YAML harness resource") from None
        elif suffix == ".md" and item.text.startswith("---\n"):
            import yaml

            frontmatter, separator, _ = item.text[4:].partition("\n---")
            if separator:
                try:
                    values = [yaml.safe_load(frontmatter)]
                except yaml.YAMLError:
                    raise ValueError("invalid resource frontmatter") from None
        for value in values:
            _check_native_secrets(
                value,
                env,
                name,
                version=version,
                mcp=name == "claude-code"
                and isinstance(value, dict)
                and "mcpServers" in value,
            )
            if suffix != ".jsonl" and item.destination != session_file:
                assert_portable(value, resources)
                if primary_model is not None:
                    validate_resource_models(value, primary_model)


def validate_resource_models(value: Any, model: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key in {"model", "small_model"}
                and isinstance(child, str)
                and child not in {model, model.split("/", 1)[-1], "inherit"}
            ):
                raise ValueError(
                    "resource model conflicts with primary ancillary policy"
                )
            validate_resource_models(child, model)
    elif isinstance(value, list):
        for child in value:
            validate_resource_models(child, model)


def seal_resources(
    resources: list[ResourceSource | SealedResource], base: Path
) -> list[SealedResource]:
    from tetrabench.context import seal_context
    from tetrabench.models import ContextConfig, ContextFileSpec

    result: list[SealedResource] = []
    for resource in resources:
        if isinstance(resource, SealedResource):
            result.append(resource)
            continue
        validate_logical_path(resource.destination)
        path = Path(resource.source).expanduser()
        path = path if path.is_absolute() else base / path
        if not resource.directory:
            validate_resource_name(path.name)
        # The existing sealer traverses every ancestor no-follow and double-reads
        # directory trees. Do not resolve() away symlinks before giving it authority.
        config = ContextConfig(
            max_files=MAX_RESOURCE_FILES - len(result),
            max_file_bytes=MAX_RESOURCE_FILE_BYTES,
            max_total_bytes=max(
                1, MAX_RESOURCE_BYTES - sum(len(item.text.encode()) for item in result)
            ),
            files=[]
            if resource.directory
            else [ContextFileSpec(source=path.name, destination=resource.destination)],
        )
        sealed = seal_context(
            path.parent,
            config,
            fixture_roots=(path.name,) if resource.directory else (),
        )
        for entry, item in zip(sealed.manifest.files, sealed.files, strict=True):
            destination = resource.destination
            if resource.directory:
                destination += "/" + entry.destination.removeprefix(path.name + "/")
            validate_resource_name(
                path.name if not resource.directory else entry.destination
            )
            try:
                text = item.content.decode("utf-8")
            except UnicodeError:
                raise ValueError("harness resources must be UTF-8 text") from None
            result.append(
                SealedResource(
                    destination=destination,
                    text=text,
                    sha256=item.descriptor.sha256,
                    mode=entry.mode,
                )
            )
        validate_resources(result)
    validate_resources(result)
    return sorted(result, key=lambda item: item.destination)


def resource_path(reference: str, resources: list[SealedResource]) -> str:
    """Resolve a declared file/directory alias, not an arbitrary host pathname."""
    alias = reference.removeprefix("resource:")
    validate_logical_path(alias)
    if not any(
        item.destination == alias or item.destination.startswith(alias + "/")
        for item in resources
    ):
        raise ValueError("native resource path must name a sealed resource")
    return f"{AGENT_RESOURCE_ROOT}/{alias}"


def rewrite_resource_references(value: Any, resources: list[SealedResource]) -> Any:
    if isinstance(value, dict):
        return {
            key: rewrite_resource_references(child, resources)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [rewrite_resource_references(child, resources) for child in value]
    if isinstance(value, str):
        if value.startswith("file://resource:"):
            return "file://" + resource_path(value.removeprefix("file://"), resources)
        value = re.sub(
            r"\{file:resource:([^}]+)\}",
            lambda match: "{file:" + resource_path(match[1], resources) + "}",
            value,
        )
        if value.startswith("resource:"):
            return resource_path(value, resources)
    return value


_PATH_KEYS = {
    "instructions",
    "config_file",
    "model_instructions_file",
    "experimental_compact_prompt_file",
    "system_prompt_file",
    "append_system_prompt_file",
    "mcp_config",
    "extension",
    "skill",
    "prompt_template",
    "paths",
    "agentDefinitionFiles",
}
_EXEC_PATH_KEYS = {"command", "args", "cwd", "path"}


def _host_path(value: str, key: str) -> bool:
    return (
        key in _EXEC_PATH_KEYS
        and value.startswith(("/", "~/"))
        and not value.startswith(("/usr/bin/", "/bin/", "/app/", "/workspace/"))
        and value not in {"/app", "/workspace"}
    )


def prepare_resources(
    spec: HarnessConfig,
    native: dict[str, Any],
    options: dict[str, Any],
    base: Path,
    *,
    native_base: Path | None = None,
) -> tuple[list[SealedResource], dict[str, Any], dict[str, Any]]:
    """Snapshot declared bundles and file references before request admission."""
    from tetrabench.harnesses import _check_native_secrets

    sources = list(spec.resources)

    def source_path(source: ResourceSource) -> Path:
        path = Path(source.source).expanduser()
        return (path if path.is_absolute() else base / path).absolute()

    def origin(item: SealedResource) -> Path | None:
        for source in sources:
            if not isinstance(source, ResourceSource):
                continue
            if item.destination == source.destination:
                return source_path(source).parent
            if source.directory and item.destination.startswith(
                source.destination + "/"
            ):
                return (
                    source_path(source)
                    / item.destination.removeprefix(source.destination + "/")
                ).parent
        return None

    def reference(value: str, relative_base: Path | None) -> str:
        if value.startswith(("resource:", AGENT_RESOURCE_ROOT + "/")):
            return value
        if "://" in value or "{env:" in value:
            return value
        if relative_base is None:
            raise ValueError("sealed resources cannot retain unresolved host paths")
        path = Path(value).expanduser()
        path = (path if path.is_absolute() else relative_base / path).absolute()
        for source in sources:
            if isinstance(source, ResourceSource):
                candidate = source_path(source)
                if path == candidate:
                    return "resource:" + source.destination
                if source.directory and path.is_relative_to(candidate):
                    return (
                        "resource:"
                        + source.destination
                        + "/"
                        + path.relative_to(candidate).as_posix()
                    )
        if len(sources) >= MAX_RESOURCE_FILES:
            raise ValueError("harness resource reference count exceeds limit")
        alias = f"native/{len(sources)}/{path.name}"
        sources.append(
            ResourceSource(source=str(path), destination=alias, directory=path.is_dir())
        )
        return "resource:" + alias

    def collect(value: Any, relative_base: Path | None, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                child_key: collect(child, relative_base, child_key)
                for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [collect(child, relative_base, key) for child in value]
        if isinstance(value, str):
            if value.startswith("file://") and not value.startswith("file://resource:"):
                from urllib.parse import unquote, urlsplit

                url = urlsplit(value)
                if url.netloc or url.query or url.fragment:
                    raise ValueError(
                        "local resource URLs cannot contain authority or query fields"
                    )
                return "file://" + reference(unquote(url.path), relative_base)
            if "{file:" in value:
                return re.sub(
                    r"\{file:([^}]+)\}",
                    lambda match: "{file:" + reference(match[1], relative_base) + "}",
                    value,
                )
            if (
                key in _PATH_KEYS
                or _host_path(value, key)
                or (key == "plugin" and value.startswith(("./", "../", "~/", "/")))
            ) and value:
                return reference(value, relative_base)
        return value

    native, options = collect(native, native_base or base), collect(options, base)
    resources = seal_resources(sources, base)
    native = rewrite_resource_references(native, resources)
    options = rewrite_resource_references(options, resources)
    rewritten = []
    for item in resources:
        if Path(item.destination).suffix in {
            ".json",
            ".jsonc",
            ".toml",
        } and item.destination != (
            spec.session.load_trajectory.removeprefix("resource:")
            if spec.session and spec.session.load_trajectory
            else None
        ):
            from tetrabench.harness_config import NativeConfig
            from tetrabench.harnesses import parse_native

            content = parse_native(
                NativeConfig.model_validate(
                    {"format": Path(item.destination).suffix[1:], "text": item.text}
                )
            )
            previous_count = len(sources)
            content = collect(content, origin(item))
            if len(sources) != previous_count:
                resources.extend(seal_resources(sources[previous_count:], base))
                validate_resources(resources)
            content = rewrite_resource_references(content, resources)
            _check_native_secrets(
                content,
                spec.env,
                spec.name,
                version=spec.version,
                mcp=spec.name == "claude-code" and "mcpServers" in content,
            )
            assert_portable(content, resources)
            if content != parse_native(
                NativeConfig.model_validate(
                    {"format": Path(item.destination).suffix[1:], "text": item.text}
                )
            ):
                if item.destination.endswith(".toml"):
                    import toml

                    text = toml.dumps(content)
                else:
                    text = json.dumps(content, ensure_ascii=False, allow_nan=False)
                item = SealedResource(
                    destination=item.destination,
                    text=text,
                    sha256=sha256_hex(text.encode()),
                    mode=item.mode,
                )
        rewritten.append(item)
    resources = sorted(rewritten, key=lambda item: item.destination)
    validate_resources(resources)
    validate_resource_contents(
        resources,
        spec.env,
        spec.name,
        spec.version,
        primary_model=spec.model if spec.ancillary_models == "primary" else None,
        session_file=spec.session.load_trajectory.removeprefix("resource:")
        if spec.session and spec.session.load_trajectory
        else None,
    )
    if spec.session and spec.session.load_trajectory:
        alias = spec.session.load_trajectory.removeprefix("resource:")
        if not any(item.destination == alias for item in resources):
            raise ValueError("load_trajectory must name one sealed resource file")
        if Path(alias).suffix not in {".jsonl", ".json"}:
            raise ValueError("load_trajectory requires a Harbor native/ATIF transcript")
    assert_portable(native, resources)
    assert_portable(options, resources)
    return resources, native, options


def assert_portable(value: Any, resources: list[SealedResource], key: str = "") -> None:
    if isinstance(value, dict):
        for name, child in value.items():
            assert_portable(child, resources, name)
    elif isinstance(value, list):
        for child in value:
            assert_portable(child, resources, key)
    elif isinstance(value, str):
        if value.startswith("file://"):
            assert_portable(value.removeprefix("file://"), resources, "path")
            return
        paths = re.findall(r"\{file:([^}]+)\}", value)
        if (
            key in _PATH_KEYS
            or _host_path(value, key)
            or value.startswith(("~/", "resource:", AGENT_RESOURCE_ROOT + "/"))
        ):
            paths.append(value)
        for path in paths:
            if "://" in path or "{env:" in path:
                continue
            if not path.startswith(AGENT_RESOURCE_ROOT + "/"):
                raise ValueError(
                    "unresolved native resource path; declare and seal the resource"
                )
            resource_path(path.removeprefix(AGENT_RESOURCE_ROOT + "/"), resources)


def relocate_resource_references(value: Any, destination: Path) -> Any:
    """Relocate sealed runtime references in a private discovery copy only."""
    if isinstance(value, dict):
        return {
            key: relocate_resource_references(child, destination)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [relocate_resource_references(child, destination) for child in value]
    if isinstance(value, str):
        return value.replace(AGENT_RESOURCE_ROOT + "/", str(destination) + "/")
    return value


def _resource_bytes(resource: SealedResource, reference_root: Path | None) -> bytes:
    validate_resource(resource)
    suffix = Path(resource.destination).suffix
    if reference_root is not None and suffix in {".json", ".jsonc", ".toml"}:
        from tetrabench.harness_config import NativeConfig
        from tetrabench.harnesses import parse_native

        value = parse_native(NativeConfig(format=suffix[1:], text=resource.text))
        relocated = relocate_resource_references(value, reference_root)
        if relocated != value:
            if suffix == ".toml":
                import toml

                return toml.dumps(relocated).encode()
            return json.dumps(relocated, ensure_ascii=False, allow_nan=False).encode()
    return resource.text.encode()


def materialize_resources(
    resources: list[SealedResource],
    destination: Path,
    *,
    relocate_references: bool = False,
) -> None:
    """Write a fresh bundle; relocation never changes its sealed source identity."""
    from tetrabench.context import (
        SealedContext,
        SealedContextFile,
        materialize_sealed_context,
    )
    from tetrabench.records import ContentObject, ContextManifest, ContextManifestFile

    validate_resources(resources)
    contents = [
        _resource_bytes(item, destination if relocate_references else None)
        for item in resources
    ]
    if destination.exists():
        raise ValueError("harness resource materialization must use a fresh directory")
    destination.mkdir(mode=0o700)
    files = tuple(
        SealedContextFile(
            descriptor=ContentObject(
                sha256=sha256_hex(content),
                size=len(content),
                key=f"objects/sha256/{sha256_hex(content)}",
                media_type="text/plain",
            ),
            content=content,
        )
        for content in contents
    )
    manifest = ContextManifest(
        schema_version=1,
        files=tuple(
            ContextManifestFile(
                destination=item.destination, mode=item.mode, content=file.descriptor
            )
            for item, file in zip(resources, files, strict=True)
        ),
    )
    materialize_sealed_context(
        SealedContext(manifest=manifest, files=files), destination
    )
