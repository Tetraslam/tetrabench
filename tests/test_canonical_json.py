from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from tetrabench import (
    MAX_CANONICAL_JSON_BYTES,
    CanonicalJsonError,
    DuplicateKeyError,
    JsonValue,
    dumps_canonical_json,
    loads_canonical_json,
    sha256_hex,
)


class JsonValueModel(BaseModel):
    model_config = ConfigDict(strict=True)

    value: JsonValue


def test_nested_unicode_integer_golden_bytes() -> None:
    value = {
        "z": [3, {"é": "雪", "a": True}],
        "a": {"β": None, "A": -7},
        "int": 9_007_199_254_740_991,
    }

    expected = (
        b'{"a":{"A":-7,"\xce\xb2":null},"int":9007199254740991,'
        b'"z":[3,{"a":true,"\xc3\xa9":"\xe9\x9b\xaa"}]}'
    )

    assert dumps_canonical_json(value) == expected
    assert loads_canonical_json(expected) == value


def test_orders_object_keys_by_utf16_code_units() -> None:
    assert dumps_canonical_json({"\ue000": 2, "\U00010000": 1}) == (
        b'{"\xf0\x90\x80\x80":1,"\xee\x80\x80":2}'
    )


def test_rejects_duplicate_keys_at_any_depth() -> None:
    with pytest.raises(DuplicateKeyError, match="duplicate"):
        loads_canonical_json(b'{"outer":{"x":1,"\\u0078":2}}')


@pytest.mark.parametrize(
    "value",
    [1.0, [0.5], {"nested": float("inf")}],
)
def test_rejects_python_floats(value: object) -> None:
    with pytest.raises(CanonicalJsonError, match="no-float"):
        dumps_canonical_json(value)


@pytest.mark.parametrize("data", [b"1.0", b"1e0", b"NaN", b"Infinity"])
def test_rejects_json_floats(data: bytes) -> None:
    with pytest.raises(CanonicalJsonError, match="floating-point"):
        loads_canonical_json(data)


def test_rejects_noncanonical_json() -> None:
    with pytest.raises(CanonicalJsonError, match="not RFC 8785 canonical"):
        loads_canonical_json(b'{"b":2, "a":1}')


def test_rejects_values_outside_strict_json_profile() -> None:
    for value in [(1, 2), {1: "integer key"}, 9_007_199_254_740_992]:
        with pytest.raises(CanonicalJsonError):
            dumps_canonical_json(value)


@pytest.mark.parametrize(
    "value",
    [
        9_007_199_254_740_992,
        -9_007_199_254_740_992,
        {"nested": [9_007_199_254_740_992]},
    ],
)
def test_json_value_model_rejects_unsafe_integers_before_serialization(
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        JsonValueModel.model_validate({"value": value})


def test_json_value_model_keeps_booleans_distinct_from_integers() -> None:
    model = JsonValueModel.model_validate({"value": True})

    assert model.value is True
    assert type(model.value) is bool


def test_accepts_exact_size_limit_and_rejects_oversize_input() -> None:
    at_limit = b'"' + b"a" * (MAX_CANONICAL_JSON_BYTES - 2) + b'"'

    assert loads_canonical_json(at_limit) == "a" * (MAX_CANONICAL_JSON_BYTES - 2)
    with pytest.raises(CanonicalJsonError, match="exceeds 2 MiB"):
        loads_canonical_json(at_limit + b" ")


def test_rejects_oversize_canonical_output() -> None:
    with pytest.raises(CanonicalJsonError, match="exceeds 2 MiB"):
        dumps_canonical_json("a" * (MAX_CANONICAL_JSON_BYTES - 1))


def test_sha256_hex() -> None:
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
