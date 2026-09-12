"""Run-level harness inputs and sealed, portable configuration snapshots."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
)

from tetrabench.auth_config import AuthSpec, ProfileAuthSpec
from tetrabench.capabilities import CapabilitySnapshot, CapabilitySnapshotRef

MAX_NATIVE_CONFIG_BYTES = 128 * 1024
OptionValue = str | int | bool | None


class NativeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format: Literal["json", "jsonc", "toml"] = "json"
    path: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> NativeConfig:
        if (self.path is None) == (self.text is None):
            raise ValueError("native_config requires exactly one of path or text")
        if self.text is not None and len(self.text.encode()) > MAX_NATIVE_CONFIG_BYTES:
            raise ValueError("native_config exceeds 128 KiB")
        return self


class SealedNativeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format: Literal["json", "jsonc", "toml"]
    text: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def check_digest(self) -> SealedNativeConfig:
        from tetrabench.canonical_json import sha256_hex

        data = self.text.encode()
        if len(data) > MAX_NATIVE_CONFIG_BYTES or sha256_hex(data) != self.sha256:
            raise ValueError("native_config snapshot size or digest mismatch")
        return self


class ResourceSource(BaseModel):
    """An explicitly selected file or directory, relative to its config file."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source: str
    destination: str
    directory: bool = False


class SealedResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    destination: str
    text: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    mode: Literal[420, 493] = 420

    @model_validator(mode="after")
    def validate_snapshot(self) -> SealedResource:
        from tetrabench.resources import validate_resource

        validate_resource(self)
        return self


class HarnessSession(BaseModel):
    """Harbor's native trajectory controls, never a custom conversation format."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    resume_trajectory: bool = False
    load_trajectory: Annotated[str, Field(min_length=1, max_length=4095)] | None = None

    @model_validator(mode="after")
    def validate_path(self) -> HarnessSession:
        if self.load_trajectory:
            from tetrabench.storage import validate_logical_path

            alias = self.load_trajectory.removeprefix("resource:")
            validate_logical_path(alias)
            if not alias.endswith((".json", ".jsonl")):
                raise ValueError("session seed must be a native or ATIF resource")
        return self


class HarnessConfig(BaseModel):
    """An explicit harness is always pinned; env contains host references only."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[str, Field(max_length=32, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    options: dict[str, OptionValue] = Field(default_factory=dict)
    args: list[str] = Field(default_factory=list, max_length=64)
    env: dict[str, str] = Field(default_factory=dict, max_length=32)
    native_config: NativeConfig | None = None
    ancillary_models: Literal["primary", "native"] = "primary"
    resources: list[ResourceSource | SealedResource] = Field(
        default_factory=list, max_length=128, exclude_if=lambda value: not value
    )
    discovery: Literal["native", "isolated"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    session: HarnessSession | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    auth: AuthSpec | ProfileAuthSpec | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    capability_snapshot: CapabilitySnapshotRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_contract(self, info: ValidationInfo) -> HarnessConfig:
        from tetrabench.harnesses import validate_harness

        validate_harness(
            self,
            historical=bool(info.context and info.context.get("historical_record")),
        )
        if self.capability_snapshot is not None:
            from tetrabench.reasoning import validate_snapshot_for_harness

            snapshot = validate_snapshot_for_harness(self.capability_snapshot, self)
            if self.auth is not None and snapshot.identity.auth_mode != self.auth.mode:
                raise ValueError("capability authentication mode drift")
            if self.auth is not None:
                from tetrabench.auth_config import (
                    NativeAuthReference,
                    ProfileAuthReference,
                )

                reference = self.auth.reference
                profile = (
                    reference.profile
                    if isinstance(
                        reference, (NativeAuthReference, ProfileAuthReference)
                    )
                    else "env:" + reference.name
                )
                if snapshot.identity.profile_ref != profile:
                    raise ValueError("capability auth reference drift")
        return self


class ResolvedHarness(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str
    version: str
    model: str
    options: dict[str, OptionValue] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    native_config: SealedNativeConfig | None = None
    ancillary_models: Literal["primary", "native"] = "primary"
    resources: list[SealedResource] = Field(
        default_factory=list, max_length=128, exclude_if=lambda value: not value
    )
    discovery: Literal["native", "isolated"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    session: HarnessSession | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    auth: AuthSpec | None = Field(default=None, exclude_if=lambda value: value is None)
    capability_snapshot: CapabilitySnapshotRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_contract(self) -> ResolvedHarness:
        fields = dict(
            name=self.name,
            version=self.version,
            model=self.model,
            options=self.options,
            env=self.env,
            native_config=(
                NativeConfig(
                    format=self.native_config.format, text=self.native_config.text
                )
                if self.native_config
                else None
            ),
            ancillary_models=self.ancillary_models,
            resources=list(self.resources),
            discovery=self.discovery,
            session=self.session,
            auth=self.auth,
            capability_snapshot=self.capability_snapshot,
        )
        HarnessConfig.model_validate(fields, context={"historical_record": True})
        validate_prepared_resources(self)
        return self


def validate_prepared_resources(config: HarnessConfig | ResolvedHarness) -> None:
    """Resource safety applies before discovery as well as immutable execution."""
    from tetrabench.harnesses import parse_native, supports_native_controls
    from tetrabench.resources import (
        assert_portable,
        validate_resource_contents,
        validate_resources,
    )

    resources = [item for item in config.resources if isinstance(item, SealedResource)]
    if len(resources) != len(config.resources):
        raise ValueError("prepared harness still contains unsealed resource sources")
    validate_resources(resources)
    session_file = (
        config.session.load_trajectory.removeprefix("resource:")
        if config.session and config.session.load_trajectory
        else None
    )
    if session_file and not any(item.destination == session_file for item in resources):
        raise ValueError("session seed is not a sealed resource file")
    validate_resource_contents(
        resources,
        config.env,
        config.name,
        config.version,
        primary_model=config.model if config.ancillary_models == "primary" else None,
        session_file=session_file,
    )
    if supports_native_controls(config.name, config.version):
        assert_portable(parse_native(config.native_config), resources)
        assert_portable(config.options, resources)


def capability_config(config: HarnessConfig | ResolvedHarness) -> HarnessConfig:
    """One digest shape for authoring and immutable execution records."""
    values = config.model_dump(mode="python", exclude={"capability_snapshot"})
    if isinstance(config, ResolvedHarness) and config.native_config is not None:
        values["native_config"] = {
            "format": config.native_config.format,
            "text": config.native_config.text,
        }
    return HarnessConfig.model_validate(values)


def bind_capability_adoption(
    source: HarnessConfig, snapshot: CapabilitySnapshot, **selection: OptionValue
) -> HarnessConfig:
    """Keep the observed identity intact and bind the exact resulting config."""
    from tetrabench.reasoning import adopt_harness, rebind_after_adoption

    source = capability_config(source)
    adopted = adopt_harness(source, snapshot, snapshot.identity, base=None, **selection)
    rebound = rebind_after_adoption(
        source, adopted, snapshot, identity=snapshot.identity, base=None, **selection
    )
    evidence = CapabilitySnapshotRef.from_snapshot(rebound)
    return HarnessConfig.model_validate(
        adopted.model_dump(mode="python") | {"capability_snapshot": evidence}
    )
