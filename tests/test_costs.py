import json

import pytest

from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.costs import (
    CostEvidence,
    decimal_amount,
    native_cost_evidence,
    split_controller_costs,
    summarize_costs,
)


def evidence(name, events=(), cost=None, log=b""):
    stream = b"\n".join(json.dumps(event).encode() for event in events) + log
    return native_cost_evidence(
        harness=name,
        requested_model="provider/requested",
        scope="trial",
        result={"agent_result": {"cost_usd": cost}},
        streams={"agent/native.txt": stream},
        result_artifact="trial/result.json",
    )


@pytest.mark.parametrize(
    "value", [None, True, -1, float("nan"), float("inf"), "-0.1", "1e99999"]
)
def test_invalid_cost_is_missing(value):
    assert decimal_amount(value) is None


def test_missing_zero_and_estimate_are_distinct():
    missing = summarize_costs(evidence("opencode"))
    zero = summarize_costs(
        evidence("opencode", [{"type": "step_finish", "part": {"cost": 0}}])
    )
    estimated = summarize_costs(evidence("codex", cost=1.25))
    assert missing.model.amount_usd is None
    assert missing.model.coverage == "unknown"
    assert zero.model.amount_usd == "0"
    assert zero.model.sources == ("harness_reported",)
    assert zero.model.coverage == "partial"
    assert estimated.model.amount_usd == "1.25"
    assert estimated.model.sources == ("estimate",)
    assert estimated.infrastructure.amount_usd is None


def test_step_finish_not_added_to_harbor_aggregate():
    summary = summarize_costs(
        evidence(
            "opencode",
            [
                {"type": "step_finish", "part": {"cost": 0.1}},
                {"type": "step_finish", "part": {"cost": 0.2}},
            ],
            cost=0.3,
        )
    )
    assert summary.model.amount_usd == "0.3"
    assert summary.model.coverage == "partial"


def test_opencode_auxiliary_native_logs_are_not_silently_complete():
    rows = evidence(
        "opencode",
        [{"type": "step_finish", "part": {"cost": 0.1}}],
        log=b"\nINFO service=llm providerID=openai modelID=other agent=title stream\n",
    )
    assert rows[0].observed_models == ()
    assert rows[1].observed_models == ("other",)
    assert rows[1].amount_usd is None
    assert "observed" in rows[1].limitations[0]


def test_claude_native_result_beats_estimate_and_cumulative_aggregate():
    rows = evidence(
        "claude-code",
        [
            {"type": "result", "total_cost_usd": 0.3},
            {
                "type": "result",
                "total_cost_usd": 0.5,
                "modelUsage": {"claude-sonnet-4-6": {}},
            },
        ],
        cost=100,
    )
    assert rows[0].amount_usd == "0.5"
    assert rows[0].source == "harness_reported"
    assert rows[0].observed_models == ("claude-sonnet-4-6",)
    assert rows[1].amount_usd is None


def test_pi_message_end_ignores_updates_and_labels_compaction_gap():
    rows = evidence(
        "pi",
        [
            {"type": "message_update", "message": {"usage": {"cost": {"total": 100}}}},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "model": "actual",
                    "usage": {"cost": {"total": 0}},
                },
            },
            {"type": "auto_compaction_end"},
        ],
    )
    assert rows[0].amount_usd == "0"
    assert rows[0].observed_models == ("actual",)
    assert rows[0].coverage == "partial"
    assert any("compaction" in limitation for limitation in rows[0].limitations)
    assert "observed" in rows[1].limitations[0]


def test_unknown_harness_amount_is_not_assumed_provider_cost():
    assert evidence("custom", cost=12)[0].amount_usd is None


def test_duplicate_scope_cannot_doublecount():
    item = CostEvidence(
        scope="one",
        category="model",
        amount_usd="1",
        source="estimate",
        coverage="partial",
    )
    with pytest.raises(ValueError, match="double count"):
        summarize_costs([item, item])


def test_provider_cost_does_not_complete_other_categories():
    item = CostEvidence(
        scope="provider-receipt",
        category="model",
        amount_usd="1",
        source="provider_reported",
        coverage="complete",
    )
    summary = summarize_costs([item])
    assert summary.model.coverage == "complete"
    assert summary.auxiliary.coverage == "unknown"
    assert summary.infrastructure.amount_usd is None


def test_optional_controller_supplement_preserves_legacy_payload():
    old = {"schema_version": 2, "outcome": "succeeded"}
    data = dumps_canonical_json(old)
    assert split_controller_costs(data) == (data, None)
    summary = summarize_costs(evidence("codex", cost=1.25))
    new = dumps_canonical_json(old | {"costs": summary.model_dump(mode="json")})
    core, parsed = split_controller_costs(new)
    assert core == data
    assert parsed == summary


def test_cost_summary_bounds_large_attempt_sets_without_losing_totals():
    summary = summarize_costs(
        [
            CostEvidence(
                scope=f"trial-{i}",
                category="model",
                amount_usd="0.1",
                source="harness_reported",
                coverage="partial",
            )
            for i in range(8192)
        ]
    )
    assert summary.model.amount_usd == "819.2"
    assert len(summary.evidence) == 256
    assert summary.omitted_evidence_count == 7936
    assert len(dumps_canonical_json(summary.model_dump(mode="json"))) < 300 * 1024


def test_cost_reader_never_follows_native_log_symlinks(tmp_path):
    from tetrabench.costs import _read_native

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "opencode.txt").write_bytes(b"sensitive bytes")
    (tmp_path / "agent").symlink_to(outside, target_is_directory=True)
    assert _read_native(tmp_path / "agent/opencode.txt") is None


def test_local_cost_supplement_binding_and_missing(tmp_path):
    from tetrabench.authoring import initialize_project
    from tetrabench.canonical_json import sha256_hex
    from tetrabench.costs import read_local_costs
    from tetrabench.plan import canonical_model_bytes
    from tetrabench.submission import prepare_run

    root = initialize_project(tmp_path / "project")
    request = prepare_run(root, "example", run_id="run-one").request
    path = tmp_path / "controller-result.json"
    assert read_local_costs(path, request) is None
    summary = summarize_costs(evidence("opencode", cost=0.25))
    value = {
        "schema_version": 1,
        "run_id": request.run_id,
        "request_sha256": sha256_hex(canonical_model_bytes(request)),
        "plan_sha256": request.plan_sha256,
        "costs": summary.model_dump(mode="json"),
    }
    path.write_bytes(dumps_canonical_json(value))
    assert read_local_costs(path, request) == summary
    value["plan_sha256"] = "0" * 64
    path.write_bytes(dumps_canonical_json(value))
    invalid = read_local_costs(path, request)
    assert invalid is not None
    assert invalid.model.amount_usd is None
    assert "Invalid" in invalid.limitations[-1]
