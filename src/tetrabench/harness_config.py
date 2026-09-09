"""Run-level harness inputs and sealed, portable configuration snapshots."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_NATIVE_CONFIG_BYTES = 128 * 1024
OptionValue = str | int | bool | None


class NativeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format: Literal["json", "toml"] = "json"
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
    format: Literal["json", "toml"]
    text: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def check_digest(self) -> SealedNativeConfig:
        from tetrabench.canonical_json import sha256_hex

        data = self.text.encode()
        if len(data) > MAX_NATIVE_CONFIG_BYTES or sha256_hex(data) != self.sha256:
            raise ValueError("native_config snapshot size or digest mismatch")
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

    @model_validator(mode="after")
    def validate_contract(self) -> HarnessConfig:
        from tetrabench.harnesses import validate_harness

        validate_harness(self)
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

    @model_validator(mode="after")
    def validate_contract(self) -> ResolvedHarness:
        HarnessConfig(
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
        )
        return self
