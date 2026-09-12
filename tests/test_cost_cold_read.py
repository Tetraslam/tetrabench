from pathlib import Path

import pytest
from test_costs import current_costs, opencode_streams, pi_streams
from test_costs import opencode_capture as opencode_capture
from test_costs import pi_capture as pi_capture
from test_integrity import _run
from test_remote import _content

from tetrabench.canonical_json import (
    dumps_canonical_json,
    loads_canonical_json,
    sha256_hex,
)
from tetrabench.costs import summarize_costs
from tetrabench.plan import canonical_model_bytes
from tetrabench.records import ArtifactInventoryEntry
from tetrabench.remote import RemoteResultService
from tetrabench.s3 import S3Store
from tetrabench.storage import terminal_key


@pytest.mark.parametrize("harness", ["pi", "opencode"])
def test_cold_cost_read_uses_only_bound_summary_without_native_files(
    monkeypatch, harness, pi_capture, opencode_capture
):
    store, client, request, terminal = _run(artifacts=100)
    controller_entry = next(
        item
        for item in terminal.artifacts
        if item.logical_path.endswith("controller-result.json")
    )
    value = loads_canonical_json(client.objects[controller_entry.content.key].body)
    assert isinstance(value, dict)
    costs = summarize_costs(
        current_costs(
            harness,
            pi_streams(pi_capture)
            if harness == "pi"
            else opencode_streams(opencode_capture),
            opencode_messages=opencode_capture if harness == "opencode" else None,
        )
    )
    payload = dumps_canonical_json(value | {"costs": costs.model_dump(mode="json")})
    descriptor = _content(payload)
    entries = tuple(
        item.model_copy(update={"content": descriptor})
        if item == controller_entry
        else item
        for item in terminal.artifacts
    )
    # Bind every native source in the inventory, then deliberately leave its
    # object unavailable. Routine results must not attempt to retrieve it.
    entries += tuple(
        ArtifactInventoryEntry(
            logical_path=path, content=_content(b"private native source")
        )
        for path in sorted(
            {path for row in costs.evidence for path in row.source_artifacts}
        )
    )
    old_key = terminal_key(request.run_id, sha256_hex(canonical_model_bytes(terminal)))
    terminal = terminal.model_copy(update={"artifacts": entries})
    del client.objects[old_key]
    # No native/input blob is available, even from a cache or previous hash pass.
    for key in list(client.objects):
        if key.startswith("objects/"):
            del client.objects[key]
    client.seed(descriptor.key, payload)
    client.seed(
        terminal_key(request.run_id, sha256_hex(canonical_model_bytes(terminal))),
        canonical_model_bytes(terminal),
    )
    client.operations.clear()
    fresh = S3Store(store.storage, client, sleep=lambda _: None)
    with monkeypatch.context() as context:
        context.setattr(
            Path,
            "read_bytes",
            lambda *_: (_ for _ in ()).throw(
                AssertionError("result opened a local file")
            ),
        )
        context.setattr(
            Path,
            "read_text",
            lambda *_, **__: (_ for _ in ()).throw(
                AssertionError("result opened a local file")
            ),
        )
        report = RemoteResultService(fresh).result(request.run_id)
    assert report.state == "terminal"
    assert report.costs == costs
    assert report.costs.model.amount_usd == (
        "0.564712999999999995" if harness == "pi" else "0.506897"
    )
    assert report.costs.auxiliary.amount_usd == (
        "0.097947500000000004" if harness == "pi" else "0.1908275"
    )
    assert report.costs.model.coverage == "partial"
    assert report.payload_integrity == "unchecked"
    assert [
        (operation, key)
        for operation, key in client.operations
        if key.startswith("objects/")
    ] == [("get", descriptor.key)]
