from __future__ import annotations

import json
from pathlib import Path

import pytest

from tetrabench.auth_config import NativeAuthReference
from tetrabench.auth_profiles import (
    auth_profile_command,
    load_auth_config_file,
    load_auth_profile,
    parse_auth_config_file,
    profile_store,
)
from tetrabench.auth_sessions import AuthError, private_directory, write_private


def config_bytes(tmp_path: Path) -> bytes:
    return f'''schema_version = 1
runtime_directory = "{tmp_path}/runtime"
[profiles.codex]
harness = "codex"
binding = "dedicated"
generation = 1
[profiles.codex.backend]
kind = "local"
approved_private_backend = true
state_directory = "{tmp_path}/state"
'''.encode()


def test_private_toml_load_and_versioned_profile_binding(tmp_path):
    root = private_directory(tmp_path / "config", create=True)
    path = root / "auth.toml"
    write_private(path, config_bytes(tmp_path))
    config = load_auth_config_file(path, environment={})
    ref = NativeAuthReference(profile="codex", binding="dedicated", generation=1)
    assert load_auth_profile(config, ref, "codex").backend.kind == "local"
    with pytest.raises(AuthError, match="generation"):
        load_auth_profile(config, ref.model_copy(update={"generation": 2}), "codex")
    with pytest.raises(AuthError, match="harness"):
        load_auth_profile(config, ref, "pi")
    with pytest.raises(AuthError):
        load_auth_config_file(
            path, environment={"TETRABENCH_AUTH_CONFIG_CONTENT": "{}"}
        )
    path.chmod(0o644)
    with pytest.raises(AuthError, match="0600"):
        load_auth_config_file(path, environment={})


def test_offline_parse_never_constructs_provider_client_or_native_command(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("offline auth configuration performed an operation")

    monkeypatch.setattr("tetrabench.auth_profiles.boto3.client", forbidden)
    monkeypatch.setattr("tetrabench.auth.run_native", forbidden)
    config = parse_auth_config_file(config_bytes(tmp_path))
    assert config.profiles["codex"].generation == 1
    with pytest.raises(AuthError, match="Volume"):
        profile_store(
            config.profiles["codex"],
            engine="modal",
            environment={},
            artifact_buckets=["artifacts"],
        )


def test_status_handler_no_python_setup_or_secret_read_required(tmp_path, monkeypatch):
    root = private_directory(tmp_path / "config", create=True)
    path = root / "auth.toml"
    write_private(path, config_bytes(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("status started a native command")

    monkeypatch.setattr("tetrabench.auth.run_native", forbidden)
    status = auth_profile_command(
        "status", name="codex", executable="codex", config_path=path, environment={}
    )
    assert status.state == "absent"


def test_remote_transport_contains_configuration_only(tmp_path):
    config = parse_auth_config_file(config_bytes(tmp_path))
    encoded = config.model_dump_json()
    loaded = load_auth_config_file(
        environment={"TETRABENCH_AUTH_CONFIG_CONTENT": encoded}
    )
    assert loaded == config
    bad = json.loads(encoded)
    bad["profiles"]["codex"]["backend"]["access_token"] = "SYNTHETIC_MISTAKEN_SECRET"
    with pytest.raises(AuthError) as error:
        parse_auth_config_file(json.dumps(bad).encode(), format="json")
    assert "SYNTHETIC_MISTAKEN_SECRET" not in str(error.value)


def test_profile_requires_explicit_backend_approval(tmp_path):
    with pytest.raises(AuthError):
        parse_auth_config_file(config_bytes(tmp_path).replace(b"= true", b"= false"))


def s3_config(tmp_path):
    config = json.loads(
        parse_auth_config_file(config_bytes(tmp_path)).model_dump_json()
    )
    config["profiles"]["codex"]["backend"] = {
        "kind": "s3",
        "approved_private_backend": True,
        "storage": {"provider": "tigris", "bucket": "synthetic-auth-only"},
        "access_key": {"kind": "env", "name": "TETRABENCH_AUTH_ACCESS_KEY"},
        "secret_key": {"kind": "env", "name": "TETRABENCH_AUTH_SECRET_KEY"},
    }
    return config


@pytest.mark.parametrize("trust", [None, False, True])
def test_profile_transports_explicit_org_admin_trust_to_dedicated_store(
    tmp_path, monkeypatch, trust
):
    data = s3_config(tmp_path)
    if trust is not None:
        data["profiles"]["codex"]["backend"]["trust_organization_admins"] = trust
    config = parse_auth_config_file(json.dumps(data).encode(), format="json")
    config = load_auth_config_file(
        environment={"TETRABENCH_AUTH_CONFIG_CONTENT": config.model_dump_json()}
    )
    client = object()
    result = object()
    observed = {}

    def make_client(service, **kwargs):
        observed["client"] = kwargs
        return client

    def make_store(selected_client, storage, **kwargs):
        assert selected_client is client
        observed["store"] = kwargs
        return result

    monkeypatch.setattr("tetrabench.auth_profiles.boto3.client", make_client)
    monkeypatch.setattr("tetrabench.auth_profiles.S3SessionStore", make_store)
    assert (
        profile_store(
            config.profiles["codex"],
            engine="modal",
            environment={
                "TETRABENCH_AUTH_ACCESS_KEY": "SYNTHETIC_DEDICATED_ID",
                "TETRABENCH_AUTH_SECRET_KEY": "SYNTHETIC_DEDICATED_SECRET",
            },
            artifact_buckets=["synthetic-artifacts"],
        )
        is result
    )
    assert observed["store"]["trust_organization_admins"] is (trust is True)
    assert observed["store"]["approved_private_backend"] is True
    assert observed["client"]["endpoint_url"] == "https://t3.storage.dev"
    assert observed["client"]["config"].retries == {"total_max_attempts": 1}


@pytest.mark.parametrize("invalid", ["true", "false", 0, 1, None, []])
def test_org_admin_trust_requires_a_boolean(tmp_path, invalid):
    data = s3_config(tmp_path)
    data["profiles"]["codex"]["backend"]["trust_organization_admins"] = invalid
    with pytest.raises(AuthError):
        parse_auth_config_file(json.dumps(data).encode(), format="json")


def test_org_admin_trust_is_not_aws_or_private_backend_approval(tmp_path):
    data = s3_config(tmp_path)
    backend = data["profiles"]["codex"]["backend"]
    backend["trust_organization_admins"] = True
    backend["storage"] = {
        "provider": "aws",
        "bucket": "synthetic-auth-only",
        "region": "us-east-1",
    }
    with pytest.raises(AuthError):
        parse_auth_config_file(json.dumps(data).encode(), format="json")
    backend["storage"] = {"provider": "tigris", "bucket": "synthetic-auth-only"}
    backend["approved_private_backend"] = False
    with pytest.raises(AuthError):
        parse_auth_config_file(json.dumps(data).encode(), format="json")
    del backend["approved_private_backend"]
    with pytest.raises(AuthError):
        parse_auth_config_file(json.dumps(data).encode(), format="json")
