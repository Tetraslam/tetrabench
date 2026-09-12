from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any

import modal
import pytest
from modal_proto import api_pb2

from tetrabench.auth_profiles import AUTH_CONFIG_CONTENT_ENV, parse_auth_config_file
from tetrabench.auth_sessions import AuthError, private_directory, write_private
from tetrabench.controller_configure import STORAGE_ENV_NAMES, configure_controller
from tetrabench.modal_app import controller_deployment_spec
from tetrabench.models import ProjectConfig


def project(**kwargs):
    return ProjectConfig.model_validate(
        {
            "schema_version": 1,
            "controller": {"kind": "modal", "secret_name": "controller-private"},
            "storage": {"provider": "tigris", "bucket": "artifact-only"},
        }
        | kwargs
    )


def source(**kwargs):
    return {name: "synthetic-" + name for name in STORAGE_ENV_NAMES} | kwargs


class NoValues(Mapping):
    def __getitem__(self, key):
        pytest.fail("preview resolved an environment value")

    def __iter__(self):
        pytest.fail("preview enumerated ambient environment")

    def __len__(self):
        pytest.fail("preview inspected environment")


class FakeModal:
    def __init__(self, *, environment=True, secret=None, fail=None):
        self.has_environment = environment
        self.contents = secret
        self.fail = fail
        self.events = []
        self.client = object()
        self.Client = SimpleNamespace(
            from_env=SimpleNamespace(aio=self.client_from_env)
        )
        self.Environment = SimpleNamespace(
            from_name=self.environment_from_name,
            objects=SimpleNamespace(
                create=SimpleNamespace(aio=self.create_environment)
            ),
        )
        self.Secret = SimpleNamespace(
            from_name=self.secret_from_name,
            objects=SimpleNamespace(create=SimpleNamespace(aio=self.create_secret)),
        )

    def event(self, kind):
        self.events.append(kind)
        if self.fail == kind:
            raise RuntimeError("synthetic-provider-credential-do-not-report")

    async def client_from_env(self):
        self.event("client")
        return self.client

    def environment_from_name(self, name, *, create_if_missing, client):
        assert not create_if_missing and client is self.client
        self.environment_name = name
        return SimpleNamespace(hydrate=SimpleNamespace(aio=self.environment_hydrate))

    async def environment_hydrate(self, *, client):
        assert client is self.client
        self.event("environment_lookup")
        if not self.has_environment:
            raise modal.exception.NotFoundError("synthetic absent environment")

    async def create_environment(self, name, *, client):
        assert name == self.environment_name and client is self.client
        self.has_environment = True
        self.event("environment_create")

    def secret_from_name(self, name, *, environment_name, client):
        assert environment_name == self.environment_name and client is self.client
        self.secret_name = name
        return SimpleNamespace(
            hydrate=SimpleNamespace(aio=self.secret_hydrate),
            update=SimpleNamespace(aio=self.update_secret),
        )

    async def secret_hydrate(self, *, client):
        assert client is self.client
        self.event("secret_lookup")
        if self.contents is None:
            raise modal.exception.NotFoundError("synthetic absent secret")

    async def create_secret(
        self, name, env_dict, *, allow_existing, environment_name, client
    ):
        assert name == self.secret_name and client is self.client
        assert environment_name == self.environment_name and not allow_existing
        assert self.contents is None
        self.contents = dict(env_dict)
        self.event("secret_create")

    async def update_secret(self, env_dict):
        assert self.contents is not None
        self.contents.update(env_dict)
        self.event("secret_update")


def configure(fake, **kwargs):
    return configure_controller(
        project(),
        profile="cloud",
        env_names=STORAGE_ENV_NAMES,
        write=True,
        confirmed=True,
        environment=source(),
        modal_module=fake,
        **kwargs,
    )


def auth_file(tmp_path, *, backend_patch=None):
    backend = {
        "kind": "s3",
        "approved_private_backend": True,
        "storage": {"provider": "tigris", "bucket": "auth-only"},
        "access_key": {"kind": "env", "name": "TETRABENCH_AUTH_ACCESS_KEY"},
        "secret_key": {"kind": "env", "name": "TETRABENCH_AUTH_SECRET_KEY"},
    } | (backend_patch or {})
    selected = {
        "harness": "codex",
        "binding": "dedicated",
        "generation": 1,
        "backend": backend,
    }
    data = {
        "schema_version": 1,
        "runtime_directory": str(tmp_path / "HOST-ONLY"),
        "profiles": {"chosen": selected, "not-exported": selected},
    }
    config = parse_auth_config_file(json.dumps(data).encode(), format="json")
    # Exercise the actual private TOML loader, not a monkeypatched profile object.
    import tomlkit

    path = private_directory(tmp_path / "private", create=True) / "auth.toml"
    write_private(path, tomlkit.dumps(config.model_dump(exclude_none=True)).encode())
    return path


AUTH_KEYS = {"TETRABENCH_AUTH_ACCESS_KEY", "TETRABENCH_AUTH_SECRET_KEY"}


def test_preview_is_offline_exact_and_names_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("preview contacted a provider")

    monkeypatch.setattr("tetrabench.auth_profiles.boto3.client", forbidden)
    monkeypatch.setattr("tetrabench.auth.run_native", forbidden)
    result = configure_controller(
        project(),
        profile="cloud",
        env_names=STORAGE_ENV_NAMES,
        environment=NoValues(),
        modal_module=SimpleNamespace(),
        update=True,
        create_environment=True,
    )
    expected = controller_deployment_spec(project(), "cloud").as_dict()
    assert all(result[key] == value for key, value in expected.items())
    assert result["env_names"] == sorted(STORAGE_ENV_NAMES)
    assert result["secret_state"] == "unchecked"
    assert result["readiness"] == "unproven"
    assert result["write"] is False and result["deployed"] is False


def test_preview_oauth_loads_metadata_without_values_or_native(tmp_path, monkeypatch):
    path = auth_file(tmp_path)

    class ConfigSelectorsOnly(dict):
        def get(self, key, default=None):
            assert key in {AUTH_CONFIG_CONTENT_ENV, "TETRABENCH_AUTH_CONFIG_FILE"}
            return default

    def forbidden(*args, **kwargs):
        pytest.fail("preview performed online auth")

    monkeypatch.setattr("tetrabench.auth_profiles.boto3.client", forbidden)
    monkeypatch.setattr("tetrabench.auth.run_native", forbidden)
    result = configure_controller(
        project(),
        profile="cloud",
        auth_profiles=["chosen"],
        auth_config_path=path,
        env_names=STORAGE_ENV_NAMES | AUTH_KEYS,
        environment=ConfigSelectorsOnly(),
        modal_module=SimpleNamespace(),
    )
    assert result["auth_profiles"] == ["chosen"]
    assert result["secret_keys"] == sorted(
        STORAGE_ENV_NAMES | AUTH_KEYS | {AUTH_CONFIG_CONTENT_ENV}
    )
    assert result["backend_privacy"] == "unchecked"
    assert "HOST-ONLY" not in json.dumps(result)
    assert "not-exported" not in json.dumps(result)


@pytest.mark.parametrize(
    "names", [[], ["AWS_ACCESS_KEY_ID"], ["AWS_SECRET_ACCESS_KEY"]]
)
def test_storage_selection_is_mandatory_even_in_preview(names):
    with pytest.raises(ValueError, match="explicit --env selection required"):
        configure_controller(project(), profile="cloud", env_names=names)


@pytest.mark.parametrize(
    "name",
    [
        "bad-name",
        "KEY=value",
        "KEY\n",
        "",
        AUTH_CONFIG_CONTENT_ENV,
        "TETRABENCH_AUTH_CONFIG_FILE",
        "HOME",
    ],
)
def test_invalid_and_reserved_env_names_are_rejected(name):
    with pytest.raises(ValueError):
        configure_controller(
            project(), profile="cloud", env_names=[*STORAGE_ENV_NAMES, name]
        )


def test_write_requires_confirmation_before_loading_anything():
    with pytest.raises(ValueError, match="confirmation"):
        configure_controller(
            project(), profile="cloud", write=True, environment=NoValues()
        )


def test_create_transfers_only_selected_variables_and_never_deploys():
    fake = FakeModal()
    result = configure_controller(
        project(),
        profile="cloud",
        env_names=[*STORAGE_ENV_NAMES, "AWS_SESSION_TOKEN", "MODEL_API_KEY"],
        environment=source(
            AWS_SESSION_TOKEN="synthetic-session",
            MODEL_API_KEY="synthetic-model",
            UNRELATED="synthetic-unrelated",
            TETRABENCH_AUTH_CONFIG_CONTENT="ignored",
        ),
        write=True,
        confirmed=True,
        modal_module=fake,
    )
    assert result["ok"] is True and result["secret_state"] == "created"
    assert fake.contents is not None
    assert set(fake.contents) == STORAGE_ENV_NAMES | {
        "AWS_SESSION_TOKEN",
        "MODEL_API_KEY",
    }
    assert fake.events == [
        "client",
        "environment_lookup",
        "secret_lookup",
        "secret_create",
    ]
    assert "synthetic-" not in json.dumps(result)


def test_create_refuses_existing_secret_without_mutation():
    fake = FakeModal(secret={"OLD_KEY": "synthetic-old"})
    result = configure(fake)
    assert result["ok"] is False
    assert result["error"] == "secret_exists_use_update"
    assert fake.contents == {"OLD_KEY": "synthetic-old"}
    assert "secret_create" not in fake.events


def test_update_uses_merge_keeps_unknown_credentials_and_warns():
    fake = FakeModal(secret={"OLD_CREDENTIAL": "synthetic-old"})
    result = configure(fake, update=True)
    assert result["secret_state"] == "updated"
    assert fake.contents == {"OLD_CREDENTIAL": "synthetic-old"} | source()
    assert "secret_update" in fake.events and "secret_create" not in fake.events
    assert "does not remove" in " ".join(result["warnings"])
    assert result["readiness"] == "unproven"


@pytest.mark.parametrize("allow", [False, True])
def test_environment_creation_is_explicit_and_exact(allow):
    fake = FakeModal(environment=False)
    result = configure(fake, create_environment=allow)
    assert ("environment_create" in fake.events) is allow
    assert result["ok"] is allow
    assert (
        fake.environment_name
        == controller_deployment_spec(project(), "cloud").environment_name
    )


def test_update_never_creates_an_absent_environment_or_secret():
    for exists in [False, True]:
        fake = FakeModal(environment=exists)
        result = configure(fake, update=True, create_environment=True)
        assert result["ok"] is False and result["error"] == "secret_missing"
        assert not any(event.endswith("create") for event in fake.events)


@pytest.mark.parametrize(
    "stage", ["environment_create", "secret_create", "secret_update", "secret_lookup"]
)
def test_ambiguous_or_partial_failure_never_retries_or_leaks(stage):
    fake = FakeModal(
        environment=stage == "secret_update",
        secret={} if stage == "secret_update" else None,
        fail=stage,
    )
    result = configure(fake, update=stage == "secret_update", create_environment=True)
    assert result["ok"] is False and result["error"] == stage + "_failed"
    assert fake.events.count(stage) == 1
    assert "synthetic-" not in json.dumps(result)
    if stage == "environment_create":
        assert result["environment_state"] == "unknown"
        assert "secret_create" not in fake.events
    elif stage in {"secret_create", "secret_lookup"}:
        assert result["environment_state"] == "created"
    if stage in {"secret_create", "secret_update"}:
        assert result["secret_state"] == "unknown"


def test_missing_env_value_fails_before_modal():
    fake = FakeModal()
    with pytest.raises(ValueError, match="missing or invalid"):
        configure_controller(
            project(),
            profile="cloud",
            env_names=STORAGE_ENV_NAMES,
            environment={},
            write=True,
            confirmed=True,
            modal_module=fake,
        )
    assert fake.events == []


@pytest.mark.parametrize("mode", ["api_key", "claude_setup_token"])
def test_harness_reference_requires_explicit_selection(tmp_path, mode):
    harness = tmp_path / "run.toml"
    harness.write_text(
        '[harness]\nname = "claude-code"\nversion = "2.1.267"\n'
        'model = "anthropic/claude-opus-5"\n[harness.auth]\n'
        f'mode = "{mode}"\nreference = {{kind="env", name="MODEL_TOKEN"}}\n'
    )
    with pytest.raises(ValueError, match="MODEL_TOKEN"):
        configure_controller(
            project(),
            profile="cloud",
            harness=harness,
            env_names=STORAGE_ENV_NAMES,
            environment=NoValues(),
        )
    result = configure_controller(
        project(),
        profile="cloud",
        harness=harness,
        env_names=STORAGE_ENV_NAMES | {"MODEL_TOKEN"},
        environment=NoValues(),
    )
    assert result["env_names"] == sorted(STORAGE_ENV_NAMES | {"MODEL_TOKEN"})


def test_oauth_backend_references_must_be_selected(tmp_path):
    with pytest.raises(ValueError, match="TETRABENCH_AUTH_ACCESS_KEY"):
        configure_controller(
            project(),
            profile="cloud",
            auth_config_path=auth_file(tmp_path),
            auth_profiles=["chosen"],
            env_names=STORAGE_ENV_NAMES,
            environment={},
        )


def test_auth_bucket_must_differ_offline(tmp_path):
    path = auth_file(
        tmp_path,
        backend_patch={"storage": {"provider": "tigris", "bucket": "artifact-only"}},
    )
    with pytest.raises(AuthError, match="separate private bucket"):
        configure_controller(
            project(),
            profile="cloud",
            auth_config_path=path,
            auth_profiles=["chosen"],
            env_names=STORAGE_ENV_NAMES | AUTH_KEYS,
            environment={},
        )


def test_public_sdk_installed_contract_executes_without_network():
    assert version("modal") == "1.5.4"
    inspect.signature(modal.Secret.objects.create).bind(
        None,
        "synthetic",
        {"KEY": "synthetic"},
        allow_existing=False,
        environment_name="exact",
        client=None,
    )
    inspect.signature(modal.Environment.objects.create).bind(None, "exact", client=None)
    inspect.signature(modal.Secret.update).bind(None, {"KEY": "synthetic"})
    calls = []

    async def get(request):
        calls.append(request)
        return api_pb2.SecretGetOrCreateResponse(
            secret_id="st-synthetic", metadata=api_pb2.SecretMetadata(name="synthetic")
        )

    async def mutate(request):
        calls.append(request)

    client: Any = SimpleNamespace(
        stub=SimpleNamespace(
            SecretGetOrCreate=get, SecretUpdate=mutate, EnvironmentCreate=mutate
        ),
        _snapshotted=False,
    )

    async def exercise():
        await modal.Secret.objects.create.aio(
            "synthetic",
            {"KEY": "synthetic"},
            allow_existing=False,
            environment_name="exact",
            client=client,
        )
        secret = modal.Secret.from_name(
            "synthetic", environment_name="exact", client=client
        )
        await secret.hydrate.aio(client=client)
        await secret.update.aio({"KEY": "synthetic-new"})
        await modal.Environment.objects.create.aio("exact", client=client)

    asyncio.run(exercise())
    assert (
        calls[0].object_creation_type
        == api_pb2.OBJECT_CREATION_TYPE_CREATE_FAIL_IF_EXISTS
    )
    assert calls[0].environment_name == calls[1].environment_name == "exact"
    assert dict(calls[0].env_dict) == {"KEY": "synthetic"}
    assert calls[2].secret_id == "st-synthetic"
    assert [(item.key, item.value) for item in calls[2].updates] == [
        ("KEY", "synthetic-new")
    ]
    assert calls[3].name == "exact"


class PrivacyS3:
    """Only bucket metadata exists; credential reads/writes are deliberately absent."""

    def __init__(self, events, *, public=False, admins=False):
        self.events = events
        self.public = public
        self.admins = admins
        self.meta = SimpleNamespace(
            config=SimpleNamespace(retries={"total_max_attempts": 1}),
            endpoint_url="https://t3.storage.dev",
        )

    def get_bucket_location(self, *, Bucket):
        assert Bucket == "auth-only"
        self.events.append("privacy_location")
        return {"LocationConstraint": "iad"}

    def get_bucket_policy_status(self, *, Bucket):
        assert Bucket == "auth-only"
        self.events.append("privacy_policy")
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "PolicyStatus": {"IsPublic": self.public},
        }

    def get_bucket_acl(self, *, Bucket):
        assert Bucket == "auth-only"
        self.events.append("privacy_acl")
        grants = [
            {
                "Grantee": {"Type": "CanonicalUser", "ID": "synthetic-owner"},
                "Permission": "FULL_CONTROL",
            }
        ]
        if self.admins:
            grants.append(
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "https://groups.tigris.dev/org/admins",
                    },
                    "Permission": "FULL_CONTROL",
                }
            )
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "Owner": {"ID": "synthetic-owner"},
            "Grants": grants,
        }


@pytest.mark.parametrize(
    "public,admins,trust",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, True, True),
        (True, True, True),
    ],
)
def test_real_backend_privacy_gate_precedes_transfer_and_preserves_trust(
    tmp_path, monkeypatch, public, admins, trust
):
    fake = FakeModal()
    backend = PrivacyS3(fake.events, public=public, admins=admins)
    path = auth_file(tmp_path, backend_patch={"trust_organization_admins": trust})
    observed = []

    def client(service, **kwargs):
        assert service == "s3"
        observed.append(kwargs)
        return backend

    monkeypatch.setattr("tetrabench.auth_profiles.boto3.client", client)
    values = source(**{name: "synthetic-" + name for name in AUTH_KEYS})
    result = configure_controller(
        project(),
        profile="cloud",
        auth_profiles=["chosen"],
        auth_config_path=path,
        env_names=STORAGE_ENV_NAMES | AUTH_KEYS,
        environment=values,
        write=True,
        confirmed=True,
        modal_module=fake,
    )
    accepted = not public and (not admins or trust)
    assert result["ok"] is accepted
    assert observed[0]["aws_access_key_id"] == values["TETRABENCH_AUTH_ACCESS_KEY"]
    assert observed[0]["config"].retries == {"total_max_attempts": 1}
    assert observed[0]["config"].proxies == {}
    if accepted:
        assert fake.events.index("privacy_acl") < fake.events.index("secret_create")
        assert fake.contents is not None
        exported = json.loads(fake.contents[AUTH_CONFIG_CONTENT_ENV])
        assert set(exported) == {"schema_version", "profiles"}
        assert set(exported["profiles"]) == {"chosen"}
        assert (
            exported["profiles"]["chosen"]["backend"]["trust_organization_admins"]
            is trust
        )
        assert "HOST-ONLY" not in fake.contents[AUTH_CONFIG_CONTENT_ENV]
        assert result["backend_privacy"] == "verified_at_write"
    else:
        assert fake.contents is None and "client" not in fake.events
        assert result["backend_privacy"] == "failed"
    assert "synthetic-" not in json.dumps(result)
    assert "not-exported" not in json.dumps(result)


def test_oauth_harness_requires_selected_matching_profile(tmp_path):
    path = auth_file(tmp_path)
    harness = tmp_path / "run.toml"
    harness.write_text(
        '[harness]\nname = "codex"\nversion = "0.154.0"\n'
        'model = "openai/gpt-5"\n[harness.auth]\nmode = "chatgpt_oauth"\n'
        'reference = {kind="native_session", profile="chosen", '
        'binding="dedicated", generation=1}\n'
    )
    with pytest.raises(AuthError, match="not configured"):
        configure_controller(
            project(),
            profile="cloud",
            harness=harness,
            env_names=STORAGE_ENV_NAMES,
            environment={},
        )
    result = configure_controller(
        project(),
        profile="cloud",
        harness=harness,
        auth_config_path=path,
        auth_profiles=["chosen"],
        env_names=STORAGE_ENV_NAMES | AUTH_KEYS,
        environment={},
    )
    assert result["ok"] is True
    harness.write_text(harness.read_text().replace("generation=1", "generation=2"))
    with pytest.raises(AuthError, match="generation"):
        configure_controller(
            project(),
            profile="cloud",
            harness=harness,
            auth_config_path=path,
            auth_profiles=["chosen"],
            env_names=STORAGE_ENV_NAMES | AUTH_KEYS,
            environment={},
        )


@pytest.mark.parametrize("mutation", [False, True])
def test_timeout_bounds_reads_and_marks_mutation_unknown(monkeypatch, mutation):
    fake = FakeModal()

    async def hang(*args, **kwargs):
        fake.events.append("hang")
        await asyncio.sleep(10)

    monkeypatch.setattr("tetrabench.controller_configure.READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("tetrabench.controller_configure.WRITE_TIMEOUT_SECONDS", 0.01)
    if mutation:
        fake.Secret.objects.create.aio = hang
    else:
        monkeypatch.setattr(fake, "environment_hydrate", hang)
    result = configure(fake)
    assert result["ok"] is False
    assert fake.events.count("hang") == 1
    assert result["secret_state"] == ("unknown" if mutation else "unchecked")
