"""Strict configuration, catalog, and canonical record models."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

SchemaVersion = Literal[1]


def _reject_surrounding_whitespace(value: str) -> str:
    if value != value.strip():
        raise ValueError("surrounding whitespace is not allowed")
    return value


ExactString = Annotated[str, AfterValidator(_reject_surrounding_whitespace)]
NonEmptyString = Annotated[
    str,
    StringConstraints(min_length=1),
    AfterValidator(_reject_surrounding_whitespace),
]
TaskId = Annotated[
    NonEmptyString,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    """Base for user-authored data that must fail on unknown or coerced values."""

    model_config = ConfigDict(extra="forbid", strict=True)


class FrozenRecord(StrictModel):
    """Base for resolved records whose identity must not change after validation."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AwsStorageConfig(StrictModel):
    provider: Literal["aws"]
    bucket: NonEmptyString
    region: NonEmptyString
    prefix: ExactString = ""


class TigrisStorageConfig(StrictModel):
    provider: Literal["tigris"]
    bucket: NonEmptyString
    region: Literal["auto"] = "auto"
    prefix: ExactString = ""


StorageConfig = Annotated[
    AwsStorageConfig | TigrisStorageConfig,
    Field(discriminator="provider"),
]


class ModalControllerConfig(StrictModel):
    kind: Literal["modal"] = "modal"
    app_name: NonEmptyString = "tetrabench"
    function_name: NonEmptyString = "controller"
    secret_name: NonEmptyString | None = "tetrabench-controller"


class LocalControllerConfig(StrictModel):
    kind: Literal["local"]


ControllerConfig = Annotated[
    ModalControllerConfig | LocalControllerConfig,
    Field(discriminator="kind"),
]


class ModalExecutionConfig(StrictModel):
    kind: Literal["modal"] = "modal"


class DockerExecutionConfig(StrictModel):
    kind: Literal["docker"]


ExecutionConfig = Annotated[
    ModalExecutionConfig | DockerExecutionConfig,
    Field(discriminator="kind"),
]


class ContextFileSpec(StrictModel):
    source: NonEmptyString
    destination: NonEmptyString

    @model_validator(mode="after")
    def validate_destination(self) -> ContextFileSpec:
        _validate_destination(self.destination)
        return self


class ContextConfig(StrictModel):
    files: list[ContextFileSpec] = Field(default_factory=list)
    max_files: Annotated[int, Field(ge=0, le=256)] = 256
    max_file_bytes: Annotated[int, Field(ge=1, le=16 * 1024 * 1024)] = 16 * 1024 * 1024
    max_total_bytes: Annotated[
        int,
        Field(ge=1, le=128 * 1024 * 1024),
    ] = 128 * 1024 * 1024

    @model_validator(mode="after")
    def validate_files(self) -> ContextConfig:
        if len(self.files) > self.max_files:
            raise ValueError("context contains more than max_files")
        destinations = [item.destination for item in self.files]
        if len(destinations) != len(set(destinations)):
            raise ValueError("context destinations must be unique")
        return self


class TaskSelection(StrictModel):
    include: list[TaskId] = Field(default_factory=list)
    exclude: list[TaskId] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selection(self) -> TaskSelection:
        if len(self.include) != len(set(self.include)):
            raise ValueError("included task IDs must be unique")
        if len(self.exclude) != len(set(self.exclude)):
            raise ValueError("excluded task IDs must be unique")
        if set(self.include) & set(self.exclude):
            raise ValueError("a task cannot be both included and excluded")
        return self


class CatalogTask(StrictModel):
    id: TaskId
    harbor_task: NonEmptyString


class CatalogSection(StrictModel):
    readme: NonEmptyString
    tasks: list[CatalogTask] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_task_ids(self) -> CatalogSection:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("catalog task IDs must be unique within a section")
        return self


class CatalogSections(StrictModel):
    systems_design: CatalogSection = Field(alias="systems-design")
    github_workflow: CatalogSection = Field(alias="github-workflow")


class Catalog(StrictModel):
    schema_version: SchemaVersion
    sections: CatalogSections


class ControllerPatch(StrictModel):
    kind: Literal["modal", "local"] | None = None
    app_name: NonEmptyString | None = None
    function_name: NonEmptyString | None = None
    secret_name: NonEmptyString | None = None


class ExecutionPatch(StrictModel):
    kind: Literal["modal", "docker"] | None = None


class StoragePatch(StrictModel):
    provider: Literal["aws", "tigris"] | None = None
    bucket: NonEmptyString | None = None
    region: NonEmptyString | None = None
    prefix: ExactString | None = None

    @model_validator(mode="after")
    def validate_provider_fields(self) -> StoragePatch:
        if self.provider == "tigris" and self.region not in {None, "auto"}:
            raise ValueError("Tigris storage region must be 'auto'")
        return self


class TaskSelectionPatch(StrictModel):
    include: list[TaskId] | None = None
    exclude: list[TaskId] | None = None


class ProfilePatch(StrictModel):
    controller: ControllerPatch | None = None
    execution: ExecutionPatch | None = None
    storage: StoragePatch | None = None
    context_files: list[ContextFileSpec] | None = None
    selection: TaskSelectionPatch | None = None


class UserConfig(StrictModel):
    schema_version: SchemaVersion
    profiles: dict[NonEmptyString, ProfilePatch] = Field(default_factory=dict)


class ProjectConfig(StrictModel):
    schema_version: SchemaVersion
    catalog_path: NonEmptyString = "benchmarks/catalog.toml"
    controller: ControllerConfig = Field(default_factory=ModalControllerConfig)
    execution: ExecutionConfig = Field(default_factory=ModalExecutionConfig)
    storage: StorageConfig | None = None
    context: ContextConfig = Field(default_factory=ContextConfig)
    selection: TaskSelection = Field(default_factory=TaskSelection)

    @model_validator(mode="after")
    def validate_controller_execution(self) -> ProjectConfig:
        if self.execution.kind == "docker" and self.controller.kind != "local":
            raise ValueError("Docker execution requires an explicit local controller")
        if self.execution.kind == "modal" and self.controller.kind != "modal":
            raise ValueError("Modal execution requires the Modal controller")
        return self


class ContextPatch(StrictModel):
    files: list[ContextFileSpec] | None = None
    max_files: Annotated[int, Field(ge=0, le=256)] | None = None
    max_file_bytes: Annotated[int, Field(ge=1, le=16 * 1024 * 1024)] | None = None
    max_total_bytes: Annotated[int, Field(ge=1, le=128 * 1024 * 1024)] | None = None


class ProjectConfigPatch(ProfilePatch):
    schema_version: SchemaVersion
    catalog_path: NonEmptyString | None = None
    context: ContextPatch | None = None


class ConfigOverrides(ProfilePatch):
    pass


def _validate_destination(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts:
        raise ValueError("destination must be a normalized relative POSIX path")
    if value in {"", "."}:
        raise ValueError("destination must name a file")


class ResolvedAwsStorageConfig(FrozenRecord):
    provider: Literal["aws"]
    bucket: NonEmptyString
    region: NonEmptyString
    prefix: ExactString = ""


class ResolvedTigrisStorageConfig(FrozenRecord):
    provider: Literal["tigris"]
    bucket: NonEmptyString
    region: Literal["auto"] = "auto"
    prefix: ExactString = ""
    endpoint_url: Literal["https://t3.storage.dev"] = "https://t3.storage.dev"


ResolvedStorageConfig = Annotated[
    ResolvedAwsStorageConfig | ResolvedTigrisStorageConfig,
    Field(discriminator="provider"),
]


class ResolvedModalControllerConfig(FrozenRecord):
    kind: Literal["modal"] = "modal"
    app_name: NonEmptyString = "tetrabench"
    function_name: NonEmptyString = "controller"
    secret_name: NonEmptyString | None = "tetrabench-controller"


class ResolvedLocalControllerConfig(FrozenRecord):
    kind: Literal["local"]


ResolvedControllerConfig = Annotated[
    ResolvedModalControllerConfig | ResolvedLocalControllerConfig,
    Field(discriminator="kind"),
]


class ResolvedModalExecutionConfig(FrozenRecord):
    kind: Literal["modal"] = "modal"


class ResolvedDockerExecutionConfig(FrozenRecord):
    kind: Literal["docker"]


ResolvedExecutionConfig = Annotated[
    ResolvedModalExecutionConfig | ResolvedDockerExecutionConfig,
    Field(discriminator="kind"),
]


class ResolvedTaskSelection(FrozenRecord):
    include: tuple[TaskId, ...] = ()
    exclude: tuple[TaskId, ...] = ()

    @model_validator(mode="after")
    def validate_selection(self) -> ResolvedTaskSelection:
        if len(self.include) != len(set(self.include)):
            raise ValueError("included task IDs must be unique")
        if len(self.exclude) != len(set(self.exclude)):
            raise ValueError("excluded task IDs must be unique")
        if set(self.include) & set(self.exclude):
            raise ValueError("a task cannot be both included and excluded")
        return self


class ResolvedContextFile(FrozenRecord):
    destination: NonEmptyString
    mode: Literal[420, 493]
    size: Annotated[int, Field(ge=0)]
    sha256: Sha256

    @model_validator(mode="after")
    def validate_destination(self) -> ResolvedContextFile:
        _validate_destination(self.destination)
        return self


class ResolvedTrial(FrozenRecord):
    task_id: TaskId
    harbor_task: NonEmptyString


class ResolvedPlan(FrozenRecord):
    schema_version: SchemaVersion
    section: Literal["systems-design", "github-workflow"]
    controller: ResolvedControllerConfig
    execution: ResolvedExecutionConfig
    storage: ResolvedStorageConfig | None
    selection: ResolvedTaskSelection
    context: tuple[ResolvedContextFile, ...]
    trials: tuple[ResolvedTrial, ...]
    runnable: bool
    not_runnable_reasons: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_invariants(self) -> ResolvedPlan:
        compatible = (self.execution.kind, self.controller.kind) in {
            ("modal", "modal"),
            ("docker", "local"),
        }
        if not compatible:
            raise ValueError("controller and execution are incompatible")
        destinations = [item.destination for item in self.context]
        if len(destinations) != len(set(destinations)):
            raise ValueError("context destinations must be unique")
        task_ids = [trial.task_id for trial in self.trials]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("trial task IDs must be unique")
        if not self.trials and self.runnable:
            raise ValueError("a plan with zero trials is not runnable")
        if self.runnable == bool(self.not_runnable_reasons):
            raise ValueError("runnable and not_runnable_reasons disagree")
        return self


class ResolvedRequest(FrozenRecord):
    schema_version: SchemaVersion
    run_id: NonEmptyString
    plan_sha256: Sha256
    plan: ResolvedPlan

    @model_validator(mode="after")
    def validate_plan_digest(self) -> ResolvedRequest:
        from tetrabench.canonical_json import dumps_canonical_json, sha256_hex

        plan_bytes = dumps_canonical_json(
            self.plan.model_dump(mode="json", by_alias=True)
        )
        if sha256_hex(plan_bytes) != self.plan_sha256:
            raise ValueError("plan_sha256 does not match embedded plan")
        return self
