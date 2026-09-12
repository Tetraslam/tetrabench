"""Conservative cost evidence from Harbor 0.22 and unchanged native streams.

Amounts are observations, not invoices. Aggregate and per-call evidence are
alternatives for a scope, never additive. No pricing lookup happens here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from tetrabench.models import FrozenRecord, RecordIdentifier, ResolvedPlan, Sha256

CostSource = Literal["harness_reported", "estimate", "provider_reported", "unknown"]
Coverage = Literal["partial", "unknown", "complete"]
Category = Literal["model", "auxiliary", "infrastructure"]
DecimalAmount = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$")]
MAX_COST_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_COST_SOURCE_BYTES = 32 * 1024 * 1024


class CostEvidence(FrozenRecord):
    scope: str
    category: Category
    amount_usd: DecimalAmount | None = None
    reported_amount_usd: DecimalAmount | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    source: CostSource = "unknown"
    coverage: Coverage = "unknown"
    source_artifacts: tuple[str, ...] = ()
    requested_model: str | None = None
    observed_models: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_amount(self) -> CostEvidence:
        if self.source == "unknown" and self.amount_usd is not None:
            raise ValueError("unknown cost cannot contain an amount")
        if self.amount_usd is None and self.coverage == "complete":
            raise ValueError("missing cost cannot have complete coverage")
        return self


class CostTotal(FrozenRecord):
    amount_usd: DecimalAmount | None = None
    coverage: Coverage = "unknown"
    sources: tuple[CostSource, ...] = ()


class CostSummary(FrozenRecord):
    schema_version: Literal[1] = 1
    model: CostTotal
    auxiliary: CostTotal
    infrastructure: CostTotal
    evidence: tuple[CostEvidence, ...] = ()
    omitted_evidence_count: Annotated[int, Field(ge=0)] = 0
    limitations: tuple[str, ...] = (
        "Known amounts are subtotals; missing evidence is not zero",
        "Harness prices and estimates are not a reconciled provider invoice",
    )


class LocalCostRecord(FrozenRecord):
    schema_version: Literal[1] = 1
    run_id: RecordIdentifier
    request_sha256: Sha256
    plan_sha256: Sha256
    costs: CostSummary


def read_local_costs(path: Path, request: Any) -> CostSummary | None:
    """Read a small bound supplement; missing/invalid costs never replace rewards."""
    from tetrabench.canonical_json import sha256_hex
    from tetrabench.plan import canonical_model_bytes, parse_canonical_model

    data = _read_native(path)
    if data is None:
        return None
    try:
        value = parse_canonical_model(data, LocalCostRecord)
        if (
            value.run_id != request.run_id
            or value.plan_sha256 != request.plan_sha256
            or value.request_sha256 != sha256_hex(canonical_model_bytes(request))
        ):
            raise ValueError("local cost binding mismatch")
        return value.costs
    except ValueError:
        return summarize_costs(
            [],
            limitations=(
                "Invalid local cost supplement; native reward evidence is independent",
            ),
        )


def decimal_amount(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if (
        not amount.is_finite()
        or amount < 0
        or amount.adjusted() > 12
        or (
            isinstance(amount.as_tuple().exponent, int)
            and int(amount.as_tuple().exponent) < -18
        )
    ):
        return None
    if not amount:
        return "0"
    return (
        format(amount, "f").rstrip("0").rstrip(".")
        if "." in format(amount, "f")
        else format(amount, "f")
    )


def _sum(amounts: Sequence[str]) -> str | None:
    if not amounts:
        return None
    with localcontext() as context:
        context.prec = 64
        return decimal_amount(sum((Decimal(amount) for amount in amounts), Decimal(0)))


def summarize_costs(
    evidence: Sequence[CostEvidence], *, limitations: Sequence[str] = ()
) -> CostSummary:
    if len({(item.scope, item.category) for item in evidence}) != len(evidence):
        raise ValueError("duplicate cost scope would double count")
    totals = {}
    for category in ("model", "auxiliary", "infrastructure"):
        entries = [entry for entry in evidence if entry.category == category]
        amounts = [
            entry.amount_usd for entry in entries if entry.amount_usd is not None
        ]
        totals[category] = CostTotal(
            amount_usd=_sum(amounts),
            coverage="complete"
            if entries and all(entry.coverage == "complete" for entry in entries)
            else "partial"
            if amounts
            else "unknown",
            sources=tuple(sorted({entry.source for entry in entries})),
        )
    from tetrabench.canonical_json import dumps_canonical_json

    retained = []
    retained_bytes = 0
    for entry in evidence:
        size = len(dumps_canonical_json(entry.model_dump(mode="json")))
        if len(retained) >= 256 or retained_bytes + size > 256 * 1024:
            break
        retained.append(entry)
        retained_bytes += size
    omitted = len(evidence) - len(retained)
    if omitted:
        limitations = (
            *limitations,
            "Scope detail truncated; totals cover all scopes, native sources retained",
        )
    return CostSummary(
        model=totals["model"],
        auxiliary=totals["auxiliary"],
        infrastructure=totals["infrastructure"],
        evidence=tuple(retained),
        omitted_evidence_count=omitted,
        limitations=CostSummary.model_fields["limitations"].default
        + tuple(limitations),
    )


def _events(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    events = []
    malformed = False
    for line in data.splitlines():
        if not line.strip().startswith(b"{"):
            continue
        try:
            event = json.loads(line, parse_float=Decimal)
        except (ValueError, UnicodeError, RecursionError):
            malformed = True
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, malformed


def _model(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) <= 256
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", value)
    ):
        return value
    return None


def native_cost_evidence(
    *,
    harness: str,
    requested_model: str | None,
    scope: str,
    result: Mapping[str, Any],
    streams: Mapping[str, bytes],
    result_artifact: str,
    transcript_artifacts: frozenset[str] | None = None,
    auxiliary_requested_model: str | None = None,
    pi_pricing: Mapping[tuple[str, str], bool] | None = None,
    harness_version: str | None = None,
    started_at: datetime | None = None,
    seeded_entry_ids: frozenset[str] = frozenset(),
    opencode_messages: Sequence[Mapping[str, Any]] | None = None,
    opencode_artifacts: tuple[str, ...] = (),
) -> tuple[CostEvidence, ...]:
    """Pure helper for one native trial/step, with bounded, already-read sources."""
    if (harness, harness_version) in {("pi", "0.85.1"), ("opencode", "1.18.30")}:
        current = _current_native_costs(
            harness=harness,
            scope=scope,
            streams=streams,
            transcripts=transcript_artifacts,
            requested_model=requested_model,
            auxiliary_model=auxiliary_requested_model,
            pi_pricing=pi_pricing or {},
            started_at=started_at,
            seeded_entry_ids=seeded_entry_ids,
            opencode_messages=opencode_messages,
            opencode_artifacts=opencode_artifacts,
        )
        if current is not None:
            return current
    amounts: list[str] = []
    aggregates: list[str] = []
    observed: set[str] = set()
    auxiliary_models: set[str] = set()
    sources: set[str] = set()
    limitations = []
    auxiliary_observed = False
    missing = False
    unpriced = False
    raw_pi_amounts = []
    for path, data in streams.items():
        if len(data) > MAX_COST_ARTIFACT_BYTES:
            limitations.append("Native cost artifact exceeded the bounded reader")
            continue
        events, malformed = _events(data)
        missing |= malformed
        for event in events:
            kind = event.get("type")
            amount = None
            relevant = False
            unpriced_event = False
            is_transcript = transcript_artifacts is None or path in transcript_artifacts
            if is_transcript and harness == "opencode" and kind == "step_finish":
                part = event.get("part")
                relevant = isinstance(part, dict)
                if relevant:
                    amount = decimal_amount(part.get("cost"))
            elif is_transcript and harness == "pi" and kind == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    relevant = True
                    usage = message.get("usage")
                    cost = usage.get("cost") if isinstance(usage, dict) else None
                    amount = (
                        decimal_amount(cost.get("total"))
                        if isinstance(cost, dict)
                        else None
                    )
                    if amount is not None:
                        raw_pi_amounts.append(amount)
                    model_id = message.get("model")
                    provider = message.get("provider")
                    pricing = (
                        (pi_pricing or {}).get((provider, model_id))
                        if isinstance(provider, str) and isinstance(model_id, str)
                        else None
                    )
                    if provider is None:
                        matches = [
                            known
                            for (_, identifier), known in (pi_pricing or {}).items()
                            if identifier == model_id
                        ]
                        pricing = matches[0] if len(matches) == 1 else None
                    used_tokens = isinstance(usage, dict) and any(
                        isinstance(usage.get(key), (int, Decimal)) and usage[key] > 0
                        for key in ("input", "output", "cacheRead", "cacheWrite")
                    )
                    if amount == "0" and (
                        pricing is False or (used_tokens and pricing is not True)
                    ):
                        unpriced = True
                        unpriced_event = True
                        amount = None
                    if model := _model(message.get("model")):
                        observed.add(model)
            elif is_transcript and harness == "claude-code" and kind == "result":
                relevant = True
                amount = decimal_amount(event.get("total_cost_usd"))
                if amount is not None:
                    aggregates.append(amount)
                usage = event.get("modelUsage")
                if isinstance(usage, dict):
                    observed.update(model for key in usage if (model := _model(key)))
            if relevant:
                sources.add(path)
                missing |= amount is None and not unpriced_event
                if amount is not None and not (
                    harness == "claude-code" and kind == "result"
                ):
                    amounts.append(amount)
            if harness == "codex" and kind == "turn_context":
                payload = event.get("payload")
                if isinstance(payload, dict) and (
                    model := _model(payload.get("model"))
                ):
                    observed.add(model)
                    sources.add(path)
            if harness == "opencode" and event.get("service") == "llm":
                auxiliary = event.get("agent") in {"title", "compaction", "summary"}
                auxiliary_observed |= auxiliary
                if model := _model(event.get("modelID")):
                    (auxiliary_models if auxiliary else observed).add(model)
                    sources.add(path)
            if kind in {
                "auto_compaction_start",
                "auto_compaction_end",
                "compaction",
                "branch_summary",
            }:
                auxiliary_observed = True
        if harness == "opencode":
            # OpenCode's native service=llm records include secondary agents that
            # the step_finish stream does not account for.
            for line in data.splitlines():
                if b"service=llm" in line:
                    auxiliary = any(
                        token in line
                        for token in (
                            b"agent=title",
                            b"agent=compaction",
                            b"agent=summary",
                        )
                    )
                    for model_bytes in re.findall(rb"modelID=([^\s]+)", line):
                        if model := _model(
                            model_bytes.decode("utf-8", errors="replace")
                        ):
                            (auxiliary_models if auxiliary else observed).add(model)
                            sources.add(path)
                    if auxiliary:
                        auxiliary_observed = True
    amount = aggregates[-1] if aggregates else _sum(amounts)
    source: CostSource = (
        "harness_reported" if amount is not None or unpriced else "unknown"
    )
    if len(aggregates) > 1:
        limitations.append(
            "Multiple aggregate results; using the last cumulative total"
        )
    if amount is None and not unpriced:
        native = result.get("agent_result")
        native_amount = (
            decimal_amount(native.get("cost_usd"))
            if isinstance(native, Mapping)
            else None
        )
        if native_amount is not None:
            amount = native_amount
            source = (
                "estimate"
                if harness == "codex"
                else "harness_reported"
                if harness in {"opencode", "pi", "claude-code"}
                else "unknown"
            )
            if source == "unknown":
                amount = None
            sources.add(result_artifact)
            limitations.append(
                "Harbor aggregate fallback; native per-call coverage is unresolved"
            )
            if harness == "claude-code":
                limitations.append(
                    "Harbor Claude amount may be native-reported or estimated; "
                    "raw source unavailable"
                )
            if harness == "pi" and amount == "0":
                unpriced = True
                raw_pi_amounts.append(amount)
                amount = None
    if source == "estimate":
        limitations.append(
            "Harbor 0.22 LiteLLM estimate; no pricing snapshot or provider settlement"
        )
    if missing:
        limitations.append("Some native usage records are missing or malformed")
    if harness == "pi":
        limitations.append(
            "Pi message_end usage excludes native compaction and branch-summary calls"
        )
        if unpriced:
            limitations.append(
                "Pi reported zero with absent or unverified model pricing; "
                "unpriced usage is not a known zero charge"
            )
    if harness == "claude-code":
        limitations.append(
            "Native aggregate may include auxiliary calls without separate allocation"
        )
    if not observed:
        limitations.append(
            "Observed model unavailable; requested identity is not observation"
        )
    if len(observed) > 64 or len(auxiliary_models) > 64:
        limitations.append(
            "Observed model list truncated; native sources retain details"
        )
    primary = CostEvidence(
        scope=scope,
        category="model",
        amount_usd=amount,
        reported_amount_usd=_sum(raw_pi_amounts) if unpriced else None,
        source=source,
        coverage="partial" if amount is not None else "unknown",
        source_artifacts=tuple(sorted(sources)),
        requested_model=requested_model,
        observed_models=tuple(sorted(observed)[:64]),
        limitations=tuple(limitations),
    )
    auxiliary = CostEvidence(
        scope=scope,
        category="auxiliary",
        requested_model=auxiliary_requested_model,
        observed_models=tuple(sorted(auxiliary_models)[:64]),
        source_artifacts=tuple(sorted(streams)) if auxiliary_observed else (),
        limitations=(
            "Auxiliary calls observed; separate cost unavailable"
            if auxiliary_observed
            else "Auxiliary usage is not independently covered",
        ),
    )
    return primary, auxiliary


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
        elif isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(float(value) / 1000, UTC)
        else:
            return None
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _unique_records(records):
    """Repeated native identities are alternatives; contradictory copies are unknown."""
    unique = {}
    conflicts = set()
    for identity, path, record in records:
        previous = unique.get(identity)
        if previous is not None and previous[1] != record:
            conflicts.add(identity)
        else:
            unique[identity] = (path, record)
    return [value for key, value in unique.items() if key not in conflicts], bool(
        conflicts
    )


def _native_bucket(
    records,
    *,
    harness,
    category,
    scope,
    requested_model,
    pi_pricing,
    notes: Sequence[str] = (),
    extra_paths: Sequence[str] = (),
):
    amounts = []
    reported = []
    models = set()
    paths = set(extra_paths)
    unpriced = False
    missing = False
    for path, record in records:
        paths.add(path)
        model = _model(record.get("model" if harness == "pi" else "modelID"))
        if model:
            models.add(model)
        if harness == "pi":
            usage = record.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            cost = usage.get("cost")
            amount = (
                decimal_amount(cost.get("total")) if isinstance(cost, dict) else None
            )
            if amount is not None:
                reported.append(amount)
            pricing = pi_pricing.get((_model(record.get("provider")), model))
            used = any(
                isinstance(usage.get(key), (int, Decimal)) and usage[key] > 0
                for key in ("input", "output", "cacheRead", "cacheWrite")
            )
            if amount == "0" and (pricing is False or (used and pricing is not True)):
                unpriced = True
                continue
        else:
            amount = decimal_amount(record.get("cost"))
            if amount is not None:
                reported.append(amount)
            tokens = record.get("tokens")
            tokens = tokens if isinstance(tokens, dict) else {}
            cache = tokens.get("cache")
            cache = cache if isinstance(cache, dict) else {}
            if amount == "0" and any(
                isinstance(value, (int, Decimal)) and value > 0
                for value in (
                    *(tokens.get(key) for key in ("input", "output", "reasoning")),
                    cache.get("read"),
                    cache.get("write"),
                )
            ):
                # 1.18.30 Session.getUsage defaults absent catalog rates to zero.
                # No rate snapshot is present in the message/part itself.
                unpriced = True
                continue
        missing |= amount is None
        if amount is not None:
            amounts.append(amount)
    limitations = list(notes)
    limitations.append(
        "Native money is harness-reported/catalog-priced, not provider settlement"
    )
    if unpriced:
        limitations.append("Native unpriced zero is not a known zero charge")
    if missing:
        limitations.append("Some native usage records are missing or malformed")
    if category == "auxiliary":
        limitations.append(
            "Observed native text summaries only; opaque OpenAI compaction "
            "and other background calls are not covered"
        )
    if not models:
        limitations.append(
            "Observed model unavailable; requested identity is not observation"
        )
    amount = _sum(amounts)
    return CostEvidence(
        scope=scope,
        category=category,
        amount_usd=amount,
        reported_amount_usd=_sum(reported) if unpriced else None,
        source="harness_reported" if amounts or unpriced else "unknown",
        coverage="partial" if amount is not None else "unknown",
        requested_model=requested_model,
        observed_models=tuple(sorted(models)[:64]),
        source_artifacts=tuple(sorted(paths)),
        limitations=tuple(limitations),
    )


def _current_native_costs(
    *,
    harness,
    scope,
    streams,
    transcripts,
    requested_model,
    auxiliary_model,
    pi_pricing,
    started_at,
    seeded_entry_ids,
    opencode_messages,
    opencode_artifacts,
):
    # Native print streams emit live events, not the contents of loaded sessions.
    # Choose one stdout copy; journals, message snapshots and totals never add to it.
    candidates = sorted(
        path
        for path in streams
        if (path.endswith(".txt") if transcripts is None else path in transcripts)
    )
    path = candidates[0] if candidates else ""
    data = streams.get(path, b"")
    events, malformed = (
        _events(data) if len(data) <= MAX_COST_ARTIFACT_BYTES else ([], True)
    )
    notes = []
    if malformed:
        notes.append("Native stream is malformed or exceeds the bounded reader")
    if len(candidates) > 1:
        notes.append("Selected one canonical stdout artifact; other copies not added")
    primary = []
    auxiliary = []
    extra_paths = ()
    if harness == "pi":
        sessions = {
            e["id"]
            for e in events
            if e.get("type") == "session"
            and e.get("version") == 3
            and _model(e.get("id"))
        }
        journals = []
        excluded = False
        for source, raw in sorted(streams.items()):
            if (
                not source.endswith(".jsonl")
                or source == path
                or len(raw) > MAX_COST_ARTIFACT_BYTES
            ):
                continue
            entries, invalid = _events(raw)
            if (
                not entries
                or entries[0].get("type") != "session"
                or entries[0].get("version") != 3
            ):
                continue
            session = _model(entries[0].get("id"))
            if not session or (sessions and session not in sessions):
                continue
            if invalid:
                notes.append("Malformed native journal; retained records are partial")
            for entry in entries[1:]:
                if _model(entry.get("type")) not in {
                    "message",
                    "compaction",
                    "branch_summary",
                }:
                    continue
                identifier = _model(entry.get("id"))
                timestamp = _timestamp(entry.get("timestamp"))
                if (
                    not identifier
                    or identifier in seeded_entry_ids
                    or started_at is None
                    or timestamp is None
                    or timestamp < started_at
                ):
                    excluded = True
                    continue
                # Native branch exports preserve entry IDs and timestamps. A
                # copied entry is not newly charged even under another header.
                journals.append(((identifier, timestamp), source, entry))
        if journals:
            unique, conflict = _unique_records(journals)
            for source, entry in unique:
                if entry["type"] == "message":
                    message = entry.get("message")
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        primary.append((source, message))
                else:
                    auxiliary.append((source, entry))
            notes.append(
                "Selected native session entries; stdout and aggregates not added"
            )
        else:
            live = []
            for index, event in enumerate(events):
                if event.get("type") == "message_end":
                    message = event.get("message")
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        identity = _model(message.get("responseId")) or ("line", index)
                        live.append((identity, path, message))
                elif (
                    event.get("type") == "compaction_end"
                    and event.get("aborted") is False
                ):
                    result = event.get("result")
                    if isinstance(result, dict):
                        auxiliary.append((path, result))
            primary, conflict = _unique_records(live)
            notes.append("Selected live stdout; snapshots and aggregates not added")
        if excluded:
            notes.append(
                "Imported, pre-execution or undated entries excluded "
                "from current-run cost"
            )
    else:
        live = []
        for index, event in enumerate(events):
            if event.get("type") == "step_finish" and isinstance(
                event.get("part"), dict
            ):
                part = event["part"]
                identity = (
                    _model(part.get("sessionID")),
                    _model(part.get("id")) or index,
                )
                live.append((identity, path, part))
        primary, conflict = _unique_records(live)
        live_ids = {
            (_model(p.get("sessionID")), _model(p.get("messageID"))) for _, p in primary
        }
        sessions = {session for session, _ in live_ids if session}
        selected = []
        for row in (opencode_messages or ()) if opencode_artifacts else ():
            record = row.get("data")
            if not isinstance(record, dict) or record.get("role") != "assistant":
                continue
            identity = (_model(row.get("session_id")), _model(row.get("id")))
            if None in identity:
                continue
            time = record.get("time")
            created = (
                _timestamp(time.get("created")) if isinstance(time, dict) else None
            )
            if identity not in live_ids and not (
                identity[0] in sessions
                and started_at is not None
                and created is not None
                and created >= started_at
            ):
                continue
            selected.append(
                (
                    identity,
                    opencode_artifacts[0] if opencode_artifacts else path,
                    record,
                )
            )
        if selected:
            unique, db_conflict = _unique_records(selected)
            conflict |= db_conflict
            # A partial DB can classify only some streamed messages. Keep unmatched
            # live parts once rather than silently losing those observed charges.
            selected_ids = {identity for identity, _, _ in selected}
            primary = [
                (source, part)
                for source, part in primary
                if (_model(part.get("sessionID")), _model(part.get("messageID")))
                not in selected_ids
            ]
            for source, record in unique:
                bucket = (
                    auxiliary
                    if record.get("summary") is True
                    or _model(record.get("agent")) in {"compaction", "summary", "title"}
                    else primary
                )
                bucket.append((source, record))
            extra_paths = opencode_artifacts
            notes.append(
                "Selected native message costs; matching step_finish parts "
                "and Harbor aggregates not added"
            )
        else:
            notes.append(
                "OpenCode stdout can include summaries; separate allocation unavailable"
                " without native message metadata"
            )
    if conflict:
        notes.append(
            "Conflicting duplicate native identities excluded; partial coverage"
        )
    if (
        not primary
        and not auxiliary
        and not conflict
        and not (harness == "pi" and excluded)
        and not seeded_entry_ids
    ):
        return None
    return tuple(
        _native_bucket(
            records,
            harness=harness,
            category=category,
            scope=scope,
            requested_model=requested,
            pi_pricing=pi_pricing,
            notes=notes,
            extra_paths=extra_paths,
        )
        for records, category, requested in (
            (primary, "model", requested_model),
            (auxiliary, "auxiliary", auxiliary_model),
        )
    )


def _read_native(
    path: Path, *, max_bytes: int = MAX_COST_ARTIFACT_BYTES
) -> bytes | None:
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.absolute().parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
    except OSError:
        return None
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        after = os.fstat(fd)
        unchanged = all(
            getattr(info, key) == getattr(after, key)
            for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        )
        return data if len(data) <= max_bytes and unchanged else None
    finally:
        os.close(fd)


def _opencode_messages(agent_directory: Path, *, max_bytes: int):
    """Read the 1.18.30 message table on a private copy, including committed WAL."""
    database = agent_directory / "opencode/xdg-data/opencode/opencode.db"
    contents = {}
    for suffix in ("", "-wal"):
        path = Path(str(database) + suffix)
        data = _read_native(path, max_bytes=min(MAX_COST_ARTIFACT_BYTES, max_bytes))
        if data is None:
            if not suffix or path.exists() or path.is_symlink():
                return None, ()
            continue
        contents[path] = data
        max_bytes -= len(data)
    try:
        with TemporaryDirectory(prefix="tetrabench-cost-") as temporary:
            copy = Path(temporary) / "opencode.db"
            for path, data in contents.items():
                (Path(temporary) / path.name).write_bytes(data)
            with closing(
                sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
            ) as connection:
                try:
                    connection.setlimit(
                        sqlite3.SQLITE_LIMIT_LENGTH, MAX_COST_ARTIFACT_BYTES
                    )
                    connection.execute("PRAGMA trusted_schema=OFF")
                    connection.execute("PRAGMA query_only=ON")
                    ticks = 0

                    def bounded_query():
                        nonlocal ticks
                        ticks += 1
                        return ticks > 1000

                    connection.set_progress_handler(bounded_query, 1000)
                    rows = []
                    size = 0
                    for identifier, session, raw in connection.execute(
                        "SELECT id, session_id, data FROM message LIMIT 65537"
                    ):
                        if not isinstance(raw, str):
                            return None, ()
                        size += len(raw.encode())
                        if len(rows) >= 65536 or size > MAX_COST_ARTIFACT_BYTES:
                            return None, ()
                        rows.append(
                            {
                                "id": identifier,
                                "session_id": session,
                                "data": json.loads(raw, parse_float=Decimal),
                            }
                        )
                    return rows, tuple(contents)
                finally:
                    connection.set_progress_handler(None, 0)
    except (OSError, sqlite3.Error, ValueError, RecursionError):
        return None, ()


def _supplemental_logs(agent_directory: Path):
    """Native log/session trees, bounded and without following directory links."""
    count = 0
    scanned = 0
    for tree in ("opencode", "sessions", "pi/sessions"):
        base = agent_directory / tree
        if base.is_symlink():
            continue
        for directory, directories, files in os.walk(base, followlinks=False):
            scanned += len(directories) + len(files)
            if scanned > 512:
                return
            directories.sort()
            for name in sorted(files):
                if name.endswith((".log", ".jsonl")):
                    yield Path(directory) / name
                    count += 1
                    if count >= 32:
                        return


def pi_configured_pricing(native: Mapping[str, Any]) -> dict[tuple[str, str], bool]:
    """Custom model definitions default to zero; complete explicit prices do not."""
    prices = {}
    models_file = native.get("models")
    providers = models_file.get("providers") if isinstance(models_file, dict) else None
    if not isinstance(providers, dict):
        return prices

    def complete_price(value):
        return isinstance(value, dict) and all(
            decimal_amount(value.get(field)) is not None
            for field in ("input", "output", "cacheRead", "cacheWrite")
        )

    for provider, config in providers.items():
        if not isinstance(config, dict):
            continue
        definitions = config.get("models", [])
        for definition in definitions if isinstance(definitions, list) else []:
            if isinstance(definition, dict) and isinstance(definition.get("id"), str):
                prices[(provider, definition["id"])] = complete_price(
                    definition.get("cost")
                )
        overrides = config.get("modelOverrides", {})
        for model_id, override in (
            overrides.items() if isinstance(overrides, dict) else ()
        ):
            if isinstance(override, dict) and complete_price(override.get("cost")):
                prices[(provider, model_id)] = True
    return prices


def summarize_native_costs(
    plan: ResolvedPlan,
    artifacts: Any,
    *,
    root: Path | None = None,
    logical_prefix: str = "",
) -> CostSummary:
    """Collect known native stream files after Harbor artifact validation."""
    root = root or artifacts.job_directory.parent
    harness = plan.harness.name if plan.harness else plan.harbor.agent_name
    model = plan.harness.model if plan.harness else plan.harbor.model_name
    auxiliary_model = None
    if plan.harness is not None:
        if plan.harness.ancillary_models == "primary":
            auxiliary_model = model
        elif harness == "opencode":
            from tetrabench.harnesses import parse_native

            auxiliary_model = _model(
                parse_native(plan.harness.native_config).get("small_model")
            )
    evidence = []
    pi_pricing = {}
    seeded_entry_ids = frozenset()
    if harness == "pi" and plan.harness is not None:
        from tetrabench.harnesses import parse_native

        native = parse_native(plan.harness.native_config)
        pi_pricing = pi_configured_pricing(native)
        if plan.harness.options.get("model_api"):
            pi_pricing[("harbor-endpoint", plan.harness.model.split("/", 1)[-1])] = (
                False
            )
        if plan.harness.session and plan.harness.session.load_trajectory:
            seed = plan.harness.session.load_trajectory.removeprefix("resource:")
            for resource in plan.harness.resources:
                if resource.destination == seed:
                    entries, _ = _events(resource.text.encode())
                    seeded_entry_ids = frozenset(
                        identifier
                        for entry in entries
                        if (identifier := _model(entry.get("id")))
                    )
    for trial in artifacts.trials:
        steps = trial.result.step_results
        # Multistep trial agent_result may aggregate the steps. Process the steps
        # instead, never both levels of the same trial.
        segments = (
            [(trial.directory, trial.result)]
            if not steps
            else [(trial.directory / "steps" / step.step_name, step) for step in steps]
        )
        for directory, result in segments:
            streams = {}
            remaining = MAX_COST_SOURCE_BYTES
            filename = {
                "opencode": "opencode.txt",
                "pi": "pi.txt",
                "claude-code": "claude-code.txt",
                "codex": "codex.txt",
            }.get(harness)
            if filename is not None:
                path = directory / "agent" / filename
                data = _read_native(path)
                if data is not None:
                    streams[logical_prefix + path.relative_to(root).as_posix()] = data
                    remaining -= len(data)
            transcripts = frozenset(streams)
            for path in _supplemental_logs(directory / "agent"):
                if remaining <= 0:
                    break
                data = _read_native(
                    path, max_bytes=min(MAX_COST_ARTIFACT_BYTES, remaining)
                )
                if data is not None:
                    streams[logical_prefix + path.relative_to(root).as_posix()] = data
                    remaining -= len(data)
            messages, database_paths = None, ()
            version = plan.harness.version if plan.harness else None
            if harness == "opencode" and version == "1.18.30":
                messages, database_paths = _opencode_messages(
                    directory / "agent", max_bytes=remaining
                )
            execution = getattr(result, "agent_execution", None)
            started_at = getattr(execution, "started_at", None)
            if not isinstance(started_at, datetime) or started_at.tzinfo is None:
                started_at = None
            evidence.extend(
                native_cost_evidence(
                    harness=harness,
                    requested_model=model,
                    scope=directory.relative_to(root).as_posix(),
                    result={
                        "agent_result": {
                            "cost_usd": getattr(
                                getattr(result, "agent_result", None), "cost_usd", None
                            )
                        }
                    },
                    streams=streams,
                    transcript_artifacts=transcripts,
                    auxiliary_requested_model=auxiliary_model,
                    pi_pricing=pi_pricing,
                    harness_version=version,
                    started_at=started_at,
                    seeded_entry_ids=seeded_entry_ids,
                    opencode_messages=messages,
                    opencode_artifacts=tuple(
                        logical_prefix + path.relative_to(root).as_posix()
                        for path in database_paths
                    ),
                    result_artifact=logical_prefix
                    + trial.result_path.relative_to(root).as_posix(),
                )
            )
    evidence.append(
        CostEvidence(
            scope="run",
            category="infrastructure",
            limitations=(
                "Infrastructure billing is not collected by the Harbor runner",
            ),
        )
    )
    limitations = (
        ()
        if plan.harness or harness in {"oracle", "nop"}
        else ("Legacy harness is unpinned; settings and version were not controlled",)
    )
    return summarize_costs(evidence, limitations=limitations)


def costs_from_controller_result(value: Mapping[str, Any]) -> CostSummary | None:
    """Read only a validated small controller record, never its native blobs."""
    costs = value.get("costs")
    return (
        CostSummary.model_validate_json(json.dumps(costs))
        if costs is not None
        else None
    )


def split_controller_costs(data: bytes) -> tuple[bytes, CostSummary | None]:
    """Validate/extract the optional supplement before legacy result parsing."""
    from tetrabench.canonical_json import dumps_canonical_json, loads_canonical_json

    value = loads_canonical_json(data)
    if not isinstance(value, dict):
        raise ValueError("controller result must be an object")
    costs = costs_from_controller_result(value)
    core = {key: item for key, item in value.items() if key != "costs"}
    return dumps_canonical_json(core), costs


def human_cost_lines(costs: CostSummary | None) -> tuple[str, ...]:
    if costs is None:
        return ("Costs: unavailable (legacy or incomplete evidence; not zero)",)
    lines = []
    for name in ("model", "auxiliary", "infrastructure"):
        value = getattr(costs, name)
        amount = f"${value.amount_usd}" if value.amount_usd is not None else "unknown"
        lines.append(
            f"{name.capitalize()} cost: {amount}; coverage {value.coverage}; "
            f"source {', '.join(value.sources) or 'unknown'}"
        )
    requested = sorted(
        {entry.requested_model for entry in costs.evidence if entry.requested_model}
    )
    observed = sorted(
        {model for entry in costs.evidence for model in entry.observed_models}
    )
    if requested:
        lines.append("Requested models: " + ", ".join(requested[:4]))
    lines.append("Observed native models: " + (", ".join(observed[:8]) or "unknown"))
    notes = tuple(
        dict.fromkeys(note for entry in costs.evidence for note in entry.limitations)
    )
    return tuple(lines) + costs.limitations + notes[:4]
