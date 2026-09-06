"""Small built-in engine registry. Adapters own execution and lifecycle dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from tetrabench.models import ProjectConfig
from tetrabench.run_reference import RunReference
from tetrabench.submission import PreparedSubmission


@dataclass(frozen=True)
class Capabilities:
    detached: bool = False
    default_wait: bool = True
    cancel: bool = True
    recover: bool = False
    artifacts: bool = True

    def validate_launch(self, *, wait: bool, detach: bool, output: Path | None) -> bool:
        if wait and detach:
            raise ValueError("--wait and --detach are mutually exclusive")
        if detach and not self.detached:
            raise ValueError("this engine does not support --detach")
        if self.detached and output is not None:
            raise ValueError("remote output uses durable storage; use artifacts pull")
        return wait or (self.default_wait and not detach)


class Engine(Protocol):
    kind: str
    capabilities: Capabilities

    def compile(self, settings: dict[str, object]) -> tuple[dict, dict]: ...
    def launch(
        self, prepared: PreparedSubmission, output: Path | None
    ) -> BaseModel: ...
    def status(self, reference: RunReference) -> BaseModel: ...
    def result(self, reference: RunReference) -> BaseModel: ...
    def cancel(self, reference: RunReference) -> BaseModel: ...
    def recover(self, reference: RunReference) -> BaseModel: ...
    def artifacts(self, reference: RunReference, output: Path) -> BaseModel: ...


_ENGINES: dict[str, Engine] = {}
_BUILTINS_LOADED = False


def register_engine(engine: Engine) -> None:
    if engine.kind in _ENGINES:
        raise ValueError(f"engine already registered: {engine.kind}")
    _ENGINES[engine.kind] = engine


def get_engine(kind: str) -> Engine:
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        from tetrabench.engines.docker import DockerEngine
        from tetrabench.engines.modal import ModalEngine

        register_engine(DockerEngine())
        register_engine(ModalEngine())
        _BUILTINS_LOADED = True
    try:
        return _ENGINES[kind]
    except KeyError as error:
        raise ValueError(f"unknown engine: {kind}") from error


def selected_engine(config: ProjectConfig) -> Engine:
    return get_engine(config.engine.kind if config.engine else config.execution.kind)
