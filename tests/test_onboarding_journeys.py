"""Small offline checks for the reusable installed-journey transport harness."""

import importlib.util
from pathlib import Path


def test_synthetic_issuer_compiles_without_any_embedded_credential():
    path = Path(__file__).parents[1] / "tools/onboarding_journey_fakes.py"
    spec = importlib.util.spec_from_file_location("journey_fakes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    compile(module.ISSUER, "synthetic-native-issuer", "exec")


def test_s3_double_preserves_cas_and_returns_independent_streams():
    import io
    from types import SimpleNamespace

    import pytest
    from botocore.exceptions import ClientError

    path = Path(__file__).parents[1] / "tools/onboarding_journey_fakes.py"
    spec = importlib.util.spec_from_file_location("journey_fakes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.SyntheticS3([], {}, config=SimpleNamespace())
    first = client.put_object(Bucket="test", Key="state", Body=b"one", IfNoneMatch="*")
    client.put_object(
        Bucket="test", Key="state", Body=io.BytesIO(b"two"), IfMatch=first["ETag"]
    )
    with pytest.raises(ClientError):
        client.put_object(
            Bucket="test", Key="state", Body=b"stale", IfMatch=first["ETag"]
        )
    for _ in range(2):
        assert client.get_object(Bucket="test", Key="state")["Body"].read() == b"two"
