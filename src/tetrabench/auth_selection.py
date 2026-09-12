"""Explicit authoring-to-execution auth selection, outside model validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from tetrabench.auth_config import AuthSpec, ProfileAuthSpec
from tetrabench.harness_config import HarnessConfig


def resolve_harness_auth(
    harness: HarnessConfig,
    *,
    allow_online: bool = False,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> HarnessConfig:
    """Read one selected authority; never login, claim, refresh, or rewrite inputs."""
    if not isinstance(harness.auth, ProfileAuthSpec):
        return harness
    from tetrabench.auth_profiles import resolve_profile_reference

    if harness.capability_snapshot is not None:
        raise ValueError(
            "a profile-auth capability snapshot must be adopted with "
            "--allow-authenticated-read to bind its exact credential generation"
        )

    reference = resolve_profile_reference(
        harness.auth.reference.profile,
        harness=harness.name,
        config_path=config_path,
        environment=environment,
        allow_online=allow_online,
    )
    fields = harness.model_dump(mode="python")
    fields["auth"] = AuthSpec(mode="chatgpt_oauth", reference=reference)
    return HarnessConfig.model_validate(fields)
