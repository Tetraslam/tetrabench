import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tetrabench.canonical_json import dumps_canonical_json
from tetrabench.costs import (
    CostEvidence,
    decimal_amount,
    native_cost_evidence,
    split_controller_costs,
    summarize_costs,
    summarize_native_costs,
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


# Field-only redactions of the 2026-09-11 native text-summary proof. No prompts,
# summaries, tool content, paths or credentials are retained. IDs and monetary
# spellings below are from the captured bytes, not calculated from REPORT.json.
# Pi 0.85.1 session JSONL SHA256:
# 456b645048b9d271edf78a0368814c74caa8205a91067b6c98981d9de7c8e2dc
# OpenCode 1.18.30 stdout SHA256 (message metadata from its accompanying SQLite):
# 628d01b93e23a153572f9506f0bf2f5bd97b16f98864550ac242a607335c56c1
PI_SESSION = "01a08edd-d8c0-7366-8d8e-d16e7d3cd7f6"
OC_SESSION = "ses_f7126a9fdffelrNHrixeObtEIE"
START = datetime.fromisoformat("2026-09-11T05:00:00Z")


@pytest.fixture
def pi_capture():
    # Session entry id, timestamp, native usage.cost.total. Other monetary
    # components are omitted rather than recomputed/rounded.
    captured = [
        ("2011a60b", "05:08:21.278", "0.017855000000000003"),
        ("ae88e0a8", "05:08:23.478", "0.12146"),
        ("766cbdc8", "05:08:25.206", "0"),
        ("c297faff", "05:08:34.727", "0.008012499999999999"),
        ("e5b916a1", "05:08:36.525", "0.121521"),
        ("d455aa64", "05:08:37.963", "0.129363"),
        ("e1eef7f1", "05:08:55.284", "0.012937500000000001"),
        ("e92fe45f", "05:08:57.908", "0.12245099999999999"),
        ("9e1b4504", "05:09:00.956", "0.018084000000000003"),
        ("53b772ec", "05:09:02.021", "0.013028999999999999"),
    ]
    entries = [
        {
            "type": "session",
            "version": 3,
            "id": PI_SESSION,
            "timestamp": "2026-09-11T05:08:18.753Z",
        },
        *[
            {
                "type": "message",
                "id": identifier,
                "timestamp": f"2026-09-11T{timestamp}Z",
                "message": {
                    "role": "assistant",
                    "provider": "openrouter",
                    "model": "openai/gpt-6-astra",
                    "usage": {"cost": {"total": float(cost)}},
                },
            }
            for identifier, timestamp, cost in captured
        ],
        {
            "type": "compaction",
            "id": "a75a69c0",
            "parentId": "766cbdc8",
            "timestamp": "2026-09-11T05:08:33.118Z",
            "fromHook": False,
            "usage": {
                "input": 3,
                "output": 388,
                "cacheRead": 0,
                "cacheWrite": 1572,
                "reasoning": 0,
                "totalTokens": 1963,
                "cost": {"total": 0.039080000000000004},
            },
        },
        {
            "type": "compaction",
            "id": "4ecff618",
            "parentId": "daa32889",
            "timestamp": "2026-09-11T05:08:49.091Z",
            "fromHook": False,
            "usage": {
                "input": 3,
                "output": 699,
                "cacheRead": 0,
                "cacheWrite": 1911,
                "reasoning": 0,
                "totalTokens": 2613,
                "cost": {"total": 0.0588675},
            },
        },
    ]
    return [entries[0], *sorted(entries[1:], key=lambda entry: str(entry["timestamp"]))]


@pytest.fixture
def opencode_capture():
    captured = [
        ("msg_08ed95a33001kyhFlc5fvitrjQ", "0.066255", False, 1789103004211),
        ("msg_08ed961fe0014cPSW8pO5vWzsw", "0.1262755", False, 1789103006206),
        ("msg_08ed96dd50010bj7M6SaFGZd9c", "0.0539675", True, 1789103009237),
        ("msg_08ed994bc001uzF5S79lWkC1MK", "0.015377", False, 1789103019196),
        ("msg_08ed99c54001j6nZYQEnkOdxdK", "0.1271745", False, 1789103021140),
        ("msg_08ed9a5f2001fMdLIHWWOf8iGZ", "0.0683425", True, 1789103023602),
        ("msg_08ed9d12b001tfCAotun1DISEJ", "0.017152", False, 1789103034667),
        ("msg_08ed9d806001w3oSFbUbOBZVQq", "0.1269665", False, 1789103036422),
        ("msg_08ed9e2c4001DbW6sx32pYC4kp", "0.0685175", True, 1789103039172),
        ("msg_08eda0cbf001lhXKr3WNFN6tTQ", "0.0196645", False, 1789103049919),
        ("msg_08eda1599001RGulDuAfs4J8pj", "0.008032", False, 1789103052185),
    ]
    return [
        {
            "id": identifier,
            "session_id": OC_SESSION,
            "data": {
                "role": "assistant",
                "agent": "compaction" if summary else "build",
                **({"summary": True} if summary else {}),
                "modelID": "openai/gpt-6-astra",
                "providerID": "openrouter",
                "cost": float(cost),
                "time": {"created": timestamp},
            },
        }
        for identifier, cost, summary, timestamp in captured
    ]


def jsonl(entries):
    return b"\n".join(json.dumps(entry).encode() for entry in entries)


def pi_streams(entries):
    # The real print stream repeats the header and emits live message_end and
    # compaction_end; it does not emit loaded entries as newly completed calls.
    events = [entries[0]]
    for entry in entries[1:]:
        if entry["type"] == "message":
            events.append({"type": "message_end", "message": entry["message"]})
        elif entry["type"] == "compaction":
            events.append(
                {
                    "type": "compaction_end",
                    "reason": "threshold",
                    "aborted": False,
                    "result": {"usage": entry.get("usage")},
                }
            )
    return {
        "agent/pi.txt": jsonl(events),
        "agent/pi/sessions/native.jsonl": jsonl(entries),
    }


def opencode_streams(messages):
    # A reduced real step_finish, including its genuine native part ID.
    first = messages[0]
    return {
        "agent/opencode.txt": jsonl(
            [
                {
                    "type": "step_finish",
                    "timestamp": 1789103006176,
                    "sessionID": OC_SESSION,
                    "part": {
                        "type": "step-finish",
                        "id": "prt_08ed961d30014ZBmn46yQHP1pD",
                        "messageID": first["id"],
                        "sessionID": OC_SESSION,
                        "cost": first["data"]["cost"],
                    },
                }
            ]
        )
    }


def current_costs(harness, streams, **kwargs):
    if kwargs.get("opencode_messages") is not None:
        kwargs.setdefault("opencode_artifacts", ("agent/opencode/opencode.db",))
    return native_cost_evidence(
        harness=harness,
        harness_version={"pi": "0.85.1", "opencode": "1.18.30"}[harness],
        requested_model="openrouter/openai/gpt-6-astra",
        scope="trial",
        result={"agent_result": {"cost_usd": 999}},
        result_artifact="trial/result.json",
        streams=streams,
        transcript_artifacts=frozenset(
            path for path in streams if path.endswith(".txt")
        ),
        started_at=START,
        **kwargs,
    )


def test_pi_captured_main_and_text_summaries_count_once(pi_capture):
    streams = pi_streams(pi_capture)
    streams["agent/pi/sessions/duplicate.jsonl"] = streams[
        "agent/pi/sessions/native.jsonl"
    ]
    rows = current_costs("pi", streams)
    summary = summarize_costs(rows)
    assert summary.model.amount_usd == "0.564712999999999995"
    assert summary.auxiliary.amount_usd == "0.097947500000000004"
    assert Decimal(summary.model.amount_usd) + Decimal(
        summary.auxiliary.amount_usd
    ) == Decimal("0.662660499999999999")
    assert summary.auxiliary.coverage == "partial"
    assert summary.auxiliary.sources == ("harness_reported",)
    assert rows[1].observed_models == ()  # Pi summary entries do not identify a model.
    assert "agent/pi.txt" not in rows[1].source_artifacts
    assert "trial/result.json" not in rows[0].source_artifacts


def test_opencode_captured_message_costs_allocate_summaries_once(opencode_capture):
    streams = opencode_streams(opencode_capture)
    streams["agent/opencode.txt"] += b"\n" + streams["agent/opencode.txt"]
    rows = current_costs(
        "opencode",
        streams,
        opencode_messages=opencode_capture + opencode_capture,
        opencode_artifacts=(
            "agent/opencode/opencode.db",
            "agent/opencode/opencode.db-wal",
        ),
    )
    summary = summarize_costs(rows)
    assert summary.model.amount_usd == "0.506897"
    assert summary.auxiliary.amount_usd == "0.1908275"
    assert Decimal(summary.model.amount_usd) + Decimal(
        summary.auxiliary.amount_usd
    ) == Decimal("0.6977245")
    assert rows[1].observed_models == ("openai/gpt-6-astra",)
    assert rows[1].coverage == "partial"


@pytest.mark.parametrize(
    "cost,used,known",
    [(None, True, None), ("0", True, None), ("0", False, "0"), ("0.01", True, "0.01")],
)
def test_pi_summary_missing_unpriced_zero_and_known_amount(
    pi_capture, cost, used, known
):
    summary = next(entry for entry in pi_capture if entry["type"] == "compaction")
    summary["usage"] = {"input": int(used), "cost": {"total": cost}}
    rows = current_costs("pi", pi_streams([pi_capture[0], summary]))
    assert (
        rows[0].amount_usd is None
    )  # Harbor aggregate is not added to auxiliary evidence.
    assert rows[1].amount_usd == known
    assert rows[1].coverage == ("partial" if known is not None else "unknown")
    if cost == "0" and used:
        assert rows[1].reported_amount_usd == "0"


def test_pi_seed_and_old_snapshot_are_not_new_charges(pi_capture):
    first = next(entry for entry in pi_capture if entry["type"] == "compaction")
    old = deepcopy(first)
    old.update(id="old", timestamp="2026-09-10T00:00:00Z")
    undated = deepcopy(first)
    undated.update(id="undated", timestamp="not-a-time")
    seeded = deepcopy(first)
    seeded["id"] = "seeded"
    entries = [pi_capture[0], old, undated, seeded, first]
    rows = current_costs(
        "pi", pi_streams(entries), seeded_entry_ids=frozenset({"seeded"})
    )
    assert rows[1].amount_usd == "0.039080000000000004"
    assert any("excluded" in note for note in rows[1].limitations)
    # A real resumed stdout doesn't replay these costs. Remove the artificial
    # print events made by pi_streams, leaving the imported snapshot only.
    streams = {
        "agent/pi.txt": jsonl([pi_capture[0]]),
        "agent/pi/sessions/native.jsonl": jsonl([pi_capture[0], old, undated, seeded]),
    }
    rows = current_costs("pi", streams, seeded_entry_ids=frozenset({"seeded"}))
    assert rows[0].amount_usd is None
    assert rows[1].amount_usd is None


def test_pi_conflicting_entry_is_unknown_not_arbitrary_money(pi_capture):
    entry = next(entry for entry in pi_capture if entry["type"] == "compaction")
    conflicting = deepcopy(entry)
    conflicting["usage"]["cost"]["total"] = "999"
    rows = current_costs("pi", pi_streams([pi_capture[0], entry, conflicting]))
    assert rows[1].amount_usd is None
    assert any("Conflicting" in note for note in rows[1].limitations)


def test_opencode_missing_metadata_preserves_unallocated_stdout(opencode_capture):
    rows = current_costs("opencode", opencode_streams(opencode_capture))
    assert rows[0].amount_usd == "0.066255"
    assert rows[1].amount_usd is None
    assert any("allocation unavailable" in note for note in rows[0].limitations)


def test_opencode_old_or_other_session_rows_do_not_count(opencode_capture):
    old = deepcopy(opencode_capture[0])
    old.update(id="old")
    old["data"]["time"]["created"] = 0
    other = deepcopy(opencode_capture[0])
    other["session_id"] = "other"
    rows = current_costs(
        "opencode",
        opencode_streams(opencode_capture),
        opencode_messages=[opencode_capture[0], old, other],
    )
    assert rows[0].amount_usd == "0.066255"


def write_opencode_db(agent, records, *, wal=False):
    path = agent / "opencode/xdg-data/opencode/opencode.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
    connection.executemany(
        "INSERT INTO message VALUES (?, ?, ?)",
        [(r["id"], r["session_id"], json.dumps(r["data"])) for r in records],
    )
    connection.commit()
    return path, connection


def test_opencode_bounded_private_sqlite_copy_includes_wal(tmp_path, opencode_capture):
    from tetrabench.costs import MAX_COST_SOURCE_BYTES, _opencode_messages

    path, connection = write_opencode_db(tmp_path, opencode_capture, wal=True)
    try:
        original = path.read_bytes()
        wal = path.with_name(path.name + "-wal")
        original_wal = wal.read_bytes()
        messages, sources = _opencode_messages(
            tmp_path, max_bytes=MAX_COST_SOURCE_BYTES
        )
        assert messages == json.loads(json.dumps(opencode_capture), parse_float=Decimal)
        assert sources == (path, wal)
        assert path.read_bytes() == original
        assert wal.read_bytes() == original_wal
        assert _opencode_messages(tmp_path, max_bytes=len(original)) == (None, ())
    finally:
        connection.close()


@pytest.mark.parametrize("seeded", [False, True])
def test_publication_collects_native_summaries(
    tmp_path, pi_capture, opencode_capture, seeded
):
    from tetrabench.authoring import initialize_project
    from tetrabench.harness_config import (
        HarnessConfig,
        HarnessSession,
        ResourceSource,
        SealedResource,
    )
    from tetrabench.harnesses import seal_harness
    from tetrabench.submission import prepare_run

    root = initialize_project(tmp_path / "project")
    plan = prepare_run(root, "example", run_id="cost-proof").request.plan
    for harness, version, expected in [
        ("pi", "0.85.1", "0.097947500000000004"),
        ("opencode", "1.18.30", "0.1908275"),
    ]:
        directory = tmp_path / harness
        directory.mkdir()
        streams = (
            pi_streams(pi_capture)
            if harness == "pi"
            else opencode_streams(opencode_capture)
        )
        for source, data in streams.items():
            path = directory / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if harness == "opencode":
            _, connection = write_opencode_db(directory / "agent", opencode_capture)
            connection.close()
        session = None
        resources: list[ResourceSource | SealedResource] = []
        if seeded and harness == "pi":
            entry = next(e for e in pi_capture if e["type"] == "compaction")
            (root / "seed.jsonl").write_bytes(jsonl([pi_capture[0], entry]))
            session = HarnessSession(load_trajectory="resource:seed.jsonl")
            resources = [ResourceSource(source="seed.jsonl", destination="seed.jsonl")]
            expected = "0.0588675"
        native_plan = plan.model_copy(
            update={
                "harness": seal_harness(
                    HarnessConfig(
                        name=harness,
                        version=version,
                        model="openrouter/openai/gpt-6-astra",
                        session=session,
                        resources=resources,
                    ),
                    root,
                )
            }
        )
        result = SimpleNamespace(
            step_results=[],
            agent_execution=SimpleNamespace(started_at=START),
            agent_result=SimpleNamespace(cost_usd=999),
        )
        artifacts = SimpleNamespace(
            trials=[
                SimpleNamespace(
                    directory=directory,
                    result=result,
                    result_path=directory / "result.json",
                )
            ]
        )
        summary = summarize_native_costs(
            native_plan, artifacts, root=tmp_path, logical_prefix="attempt/"
        )
        assert summary.auxiliary.amount_usd == expected
        assert all(
            path.startswith("attempt/")
            for e in summary.evidence
            for path in e.source_artifacts
        )


def test_native_pi_branch_entry_and_missing_usage(pi_capture):
    # Captured offline from Pi 0.85.1 SessionManager.branchWithSummary on
    # 2026-09-11. Usage supplied from the real compaction capture above.
    # This proves the native journal format, not a live paid branch operation.
    entry = {
        "type": "branch_summary",
        "id": "20a1c89a",
        "parentId": None,
        "timestamp": "2026-09-11T05:57:24.131Z",
        "fromId": "root",
        "fromHook": False,
        "usage": {
            "input": 3,
            "output": 388,
            "cacheRead": 0,
            "cacheWrite": 1572,
            "reasoning": 0,
            "totalTokens": 1963,
            "cost": {"total": 0.039080000000000004},
        },
    }
    rows = current_costs("pi", pi_streams([pi_capture[0], entry]))
    assert rows[1].amount_usd == "0.039080000000000004"
    del entry["usage"]
    rows = current_costs("pi", pi_streams([pi_capture[0], entry]))
    assert rows[1].amount_usd is None
    assert rows[1].source == "unknown"


def test_partial_auxiliary_subtotal_retains_missing_cost(pi_capture):
    entries = [e for e in pi_capture if e["type"] in {"session", "compaction"}]
    del entries[1]["usage"]["cost"]
    rows = current_costs("pi", pi_streams(entries))
    assert rows[1].amount_usd == "0.0588675"
    assert rows[1].coverage == "partial"
    assert any("missing" in note for note in rows[1].limitations)


def test_pi_stdout_fallback_is_not_added_to_duplicate_sources(pi_capture):
    streams = pi_streams(pi_capture)
    del streams["agent/pi/sessions/native.jsonl"]
    streams["agent/duplicate.txt"] = streams["agent/pi.txt"]
    rows = current_costs("pi", streams)
    assert rows[0].amount_usd == "0.564712999999999995"
    assert rows[1].amount_usd == "0.097947500000000004"
    assert rows[0].source_artifacts == ("agent/duplicate.txt",)


def test_pi_branch_snapshot_copy_is_not_a_second_charge(pi_capture):
    copy = deepcopy(pi_capture)
    copy[0]["id"] = "new-session-id"
    rows = current_costs(
        "pi",
        {
            "agent/pi/sessions/old.jsonl": jsonl(pi_capture),
            "agent/pi/sessions/branch.jsonl": jsonl(copy),
        },
    )
    assert rows[0].amount_usd == "0.564712999999999995"
    assert rows[1].amount_usd == "0.097947500000000004"


def test_pi_aborted_compaction_does_not_assert_zero_or_success(pi_capture):
    streams = pi_streams(pi_capture)
    events = [json.loads(line) for line in streams["agent/pi.txt"].splitlines()]
    for event in events:
        if event["type"] == "compaction_end":
            event["aborted"] = True
    rows = current_costs("pi", {"agent/pi.txt": jsonl(events)})
    assert rows[0].amount_usd == "0.564712999999999995"
    assert rows[1].amount_usd is None


def test_opencode_missing_rates_do_not_turn_positive_usage_into_free_money(
    opencode_capture,
):
    summary = next(row for row in opencode_capture if row["data"].get("summary"))
    summary["data"].update(cost=0, tokens={"input": 3, "output": 622})
    rows = current_costs(
        "opencode", opencode_streams(opencode_capture), opencode_messages=[summary]
    )
    assert rows[1].amount_usd is None
    assert rows[1].reported_amount_usd == "0"
    assert rows[1].coverage == "unknown"


def test_opencode_corrupt_or_linked_wal_is_not_silently_ignored(
    tmp_path, opencode_capture
):
    from tetrabench.costs import MAX_COST_SOURCE_BYTES, _opencode_messages

    path, connection = write_opencode_db(tmp_path, opencode_capture)
    connection.close()
    wal = path.with_name(path.name + "-wal")
    secret = tmp_path / "private"
    secret.write_bytes(b"must not be opened")
    wal.symlink_to(secret)
    assert _opencode_messages(tmp_path, max_bytes=MAX_COST_SOURCE_BYTES) == (None, ())
    wal.unlink()
    path.write_bytes(b"not sqlite")
    assert _opencode_messages(tmp_path, max_bytes=MAX_COST_SOURCE_BYTES) == (None, ())


def test_native_reader_detects_mutation(tmp_path, monkeypatch):
    from tetrabench import costs

    path = tmp_path / "native.txt"
    path.write_bytes(b"initial")
    fstat = costs.os.fstat
    calls = 0

    def mutate(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"changed")
        return fstat(fd)

    monkeypatch.setattr(costs.os, "fstat", mutate)
    assert costs._read_native(path) is None
