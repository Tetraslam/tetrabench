import json
from pathlib import Path

from test_integrity import _run
from test_remote import _content

from tetrabench.canonical_json import (
    dumps_canonical_json,
    loads_canonical_json,
    sha256_hex,
)
from tetrabench.costs import native_cost_evidence, summarize_costs
from tetrabench.plan import canonical_model_bytes
from tetrabench.remote import RemoteResultService
from tetrabench.s3 import S3Store
from tetrabench.storage import terminal_key


def test_cold_cost_read_uses_only_bound_summary_without_native_files(monkeypatch):
    store, client, request, terminal = _run(artifacts=100)
    controller_entry = next(
        item
        for item in terminal.artifacts
        if item.logical_path.endswith("controller-result.json")
    )
    value = loads_canonical_json(client.objects[controller_entry.content.key].body)
    assert isinstance(value, dict)
    events = b"\n".join(
        json.dumps({"type": "step_finish", "part": {"cost": cost}}).encode()
        for cost in (0.1, 0.2)
    )
    costs = summarize_costs(
        native_cost_evidence(
            harness="opencode",
            requested_model="openrouter/openai/gpt-6-astra",
            scope="trial",
            result={"agent_result": {"cost_usd": 0.3}},
            streams={"job/result.json": events},
            result_artifact="job/result.json",
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
    assert report.costs.model.amount_usd == "0.3"
    assert report.costs.model.coverage == "partial"
    assert report.payload_integrity == "unchecked"
    assert [
        (operation, key)
        for operation, key in client.operations
        if key.startswith("objects/")
    ] == [("get", descriptor.key)]
