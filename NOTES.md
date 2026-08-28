# Tetrabench Notes

## Append-Only Policy

- Append entries in chronological order with an ISO 8601 timestamp and provenance.
- Do not edit or delete an existing entry after it has informed implementation.
- Append corrections that name the corrected entry.
- Promote durable decisions to [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) without removing the source note.
- Distinguish user contracts, primary-source facts, observations, and inference. Never place secrets here.

## 2026-08-27T18:31:53-07:00: Initial v1 contract

Provenance: user clarification in the planning request for this repository, corroborated where applicable by the primary sources below. The user clarification is authoritative for project scope; source links document upstream interfaces, not completed tetrabench validation.

Recorded contracts:

- Harbor v0.22.0 native ATIF, results, and job artifacts own trace/result semantics; OpenTraces and a custom run database are outside v1.
- Dependencies are exactly `harbor[modal]==0.22.0` and `modal==1.5.4`.
- A deployed Modal Function controller is the detached default. Docker is attached local development/execution only.
- Active, in-progress, durable, semantic, and local-cache authority belong respectively to Modal FunctionCall, a named Modal Volume, S3, Harbor job files, and local receipts.
- Resolved plans use strict canonical JSON under 2 MiB with SHA-256 verification by submitter and controller.
- Submission writes a prepared receipt, uploads the immutable request, spawns the controller, then records its call ID. Missing controller evidence never triggers automatic resubmission because Modal offers no call idempotency key.
- Context v1 includes only explicitly selected files with normalized destinations, modes, digests, sizes, and content-addressed objects.
- S3 records are content-addressed or append-only. Terminal state is determined by enumerating `runs/<run-id>/terminals/<terminal-sha>.json`: zero is incomplete, one is terminal, and many is conflict.
- The controller uses `retries=0`, deterministic child Harbor session names, replay reconciliation, disjoint Volume paths, and explicit commit/reload boundaries.
- Cancellation terminates recorded child sandboxes before cancelling the controller.
- The nested Harbor Modal authentication sentinel workaround remains `unproven` pending a live smoke.
- Deferred work includes Tailscale service access, filesystem profiles/mounts/tmpfs, richer contexts, computer/browser use, VM/desktop images, snapshots, and other detached controllers.

Primary sources consulted:

- Harbor v0.22.0 release: https://github.com/harbor-framework/harbor/releases/tag/v0.22.0
- Harbor documentation: https://harborframework.com/docs
- Modal Function spawn API: https://modal.com/docs/sdk/py/latest/Function#spawn
- Modal Volume commit/reload and consistency semantics: https://modal.com/docs/guide/volumes
- Modal preemption guidance: https://modal.com/docs/guide/preemption
- Modal FunctionCall API: https://modal.com/docs/sdk/py/latest/FunctionCall
- Tigris S3 API documentation: https://www.tigrisdata.com/docs/sdks/s3/
- AWS SDK credential and region settings: https://docs.aws.amazon.com/sdkref/latest/guide/settings-reference.html

Planning outcome: the contracts were promoted into decision IDs D-001 through D-022 in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Only planning phase P0 is complete; all implementation evidence remains `unproven`.

## 2026-08-27T19:45:00-07:00: Review correction to the initial v1 contract

Provenance: user-provided review contracts on 2026-08-27. This entry corrects the initial v1 contract above without altering it. The user contracts are authoritative; implementation mechanisms remain unproven until their evidence rows pass.

Corrections and accepted consequences:

- Canonical JSON means RFC 8785/JCS over strict Pydantic models. Parsing rejects duplicate object keys and unknown fields; serialized plans, requests, events, and terminals contain no floats. Plans are at most 2 MiB. The JCS profile and golden bytes must be frozen before P1 serializer work.
- Content bytes live at `objects/sha256/<digest>` and must hash to the key. Existing same-key bytes require verification; different bytes are corruption and conflict.
- Requests use `runs/<run-id>/requests/<request-sha>.json`. One run ID admits one distinct request digest; only same-request recovery is idempotent.
- Events use `runs/<run-id>/events/<sequence>-<event-sha>.json`. Their sequence is monotonic within an attempt, collisions conflict, and events are diagnostic rather than terminal authority.
- A terminal binds the request and winning attempt to the exact Harbor version, outcome, complete logical artifact inventory, Harbor config/lock/result digests, and evidence/warnings. The controller uploads and HEAD/hash-verifies every content object before writing the terminal last. Failed runs may publish a terminal only after all available evidence is inventoried and published.
- One valid visible terminal is terminal and more than one is conflict. Zero is unknown/nonterminal, not proof of incompletion. Readers retry boundedly and combine S3 observations with Modal call state because Tigris cross-region visibility may lag.
- Receipts have atomically replaced and fsynced `prepared`, `request-published`, and `submitted` phases. Request publication precedes `.spawn`; status can recover the controller call ID from controller records once visible. An unresolved request without controller evidence is never automatically resubmitted.
- Every controller start gets an append-only attempt ID and distinct Harbor jobs directory. Before starting a replay attempt, it reconciles terminal state, terminates surviving prior children, and records abandonment. `retries=0` remains required, but Modal preemption replay is expected.
- Tetrabench provides deterministic Harbor job inputs and tags. Harbor owns session IDs and all task/trial/session/sandbox lifecycle. Child IDs/tags must be observed through supported Harbor extension surfaces. This mechanism is `unproven`, and controller code is blocked until a version-matched contract fixture proves it.
- Cancellation publishes intent first. The controller checks it before child creation and at phase boundaries, stops new children, terminates owned children, and publishes quiescence. The canceller waits boundedly, cancels the controller if needed, then repeatedly sweeps persisted child IDs/tags until none remain or cleanup failure is reported.
- Submitter and controller use separate scoped credentials. Children receive no S3 publication credentials. Buckets are private and encrypted by default, paths and run IDs are validated, v1 has no delete permission, and no secret values belong in configuration or receipts.
- Harbor artifacts may contain workload-emitted secrets. Tetrabench promises only not to deliberately serialize credentials; it preserves native bytes privately. Public sanitization/export remains deferred.
- Immutable context accepts explicit regular files only, rejects links and special files, opens once without following links, compares `fstat` before and after reading, normalizes unique relative POSIX destinations and modes, and verifies content before materialization. Limits are 256 files, 16 MiB per file, and 128 MiB total, configurable only downward.
- Docker remains explicit attached local development execution. Detached Modal remains the default submission path. Tetrabench uses supported Harbor `JobConfig`, environment, plugin, or custom-environment surfaces only after a proven native capability gap.
- The planning files were reviewed but are untracked, so the earlier statement that P0 was complete was incorrect. P0 remains incomplete until a committed planning baseline exists. Checked creation items record reviewed content, not durability.
- The Systems Design and GitHub Workflow task catalogs are empty benchmark categories, not implementation queues.

Unproven evidence after this correction: nested Modal authentication, supported Harbor child observation, live AWS/Tigris object behavior, and detached Modal smoke. The plan records these as U-001, U-004, U-005, and U-008, with child observation blocking controller code.

## 2026-08-27T18:46:37-07:00: Notes-policy and timestamp correction

Provenance: local clock check during the 2026-08-27 review-correction session.

The heading timestamp `2026-08-27T19:45:00-07:00` on the preceding review-correction entry was recorded incorrectly. This entry was appended after it at `2026-08-27T18:46:37-07:00`. File order preserves creation order despite the incorrect earlier timestamp.

The append-only policy is corrected as follows: every entry is immutable immediately after creation, whether or not it has informed implementation. Correct conflicts only by appending another dated, provenanced entry.

## 2026-08-27T18:50:46-07:00: Public P0 planning baseline

Provenance: user-provided public commit URL, corroborated on 2026-08-27 by the public GitHub commit page, `origin/master`, local commit inspection, and validation of the committed planning surfaces.

Commit [`5400d401467d2550334f375f324a212eae946dbf`](https://github.com/Tetraslam/tetrabench/commit/5400d401467d2550334f375f324a212eae946dbf) is the durable P0 planning baseline. It contains `AGENTS.md`, the relative `CLAUDE.md -> AGENTS.md` symlink, `IMPLEMENTATION_PLAN.md`, `NOTES.md`, and `README.md`. The links and symlink resolve, Markdown structure is present, committed-content whitespace and secret checks passed, and the baseline represents no implementation as complete. P0-06 and E-024 are therefore complete.

Implementation has not started. Serializer code remains blocked until E-013 freezes the RFC 8785/JCS profile and version-matched golden bytes. Controller code remains independently blocked until E-019 proves supported Harbor v0.22.0 child ID/tag observation.

## 2026-08-28T13:45:28-07:00: E-013 JCS dependency

Provenance: PyPI metadata, the Trail of Bits repository, and local Python 3.12 tests. `rfc8785==0.1.4` is the maintained pure-Python RFC 8785 implementation selected for E-013; its repository remained active in August 2026, and the pinned release passed tetrabench's strict golden-byte contract.

## 2026-08-28T13:54:47-07:00: E-019 Harbor child observation

Provenance: version-matched contract fixture and pinned upstream source inspection under `~/.local/share/opencode/tetrabench-research/` using `harbor[modal]==0.22.0` and `modal==1.5.4`.

E-019 passed. Harbor's supported custom-environment import path constructs an `ObservedModalEnvironment` while preserving Harbor's session ID. Its public `start`/`stop` overrides record run/attempt labels and observe the child through public `Sandbox.from_name`; public tag-filtered `Sandbox.list` supports cleanup sweeps. The selected contract rejects sandbox v2 and does not fork `Trial` or depend on Harbor private fields. The persistent fixture is in `proof/`; pinned sources are `harbor-v0.22.0/src/harbor/environments/{factory.py,modal.py}` and `modal-1.5.4/modal/sandbox.py`. This clears the controller design gate. Live nested Modal behavior remains `unproven` and belongs to P4 smoke evidence.

## 2026-08-28T14:10:09-07:00: P1 planning foundation

Provenance: local implementation and validation on Python 3.12. The package now pins Harbor 0.22.0 and Modal 1.5.4, validates strict project/profile/provider/catalog schemas, resolves selected regular context files into path-free content records, and emits canonical resolved plan/request skeletons. The local catalog remains intentionally empty, and the CLI reports its plans as non-runnable. Forty-three tests, Ruff, ty, the uv lock check, package build, isolated wheel entrypoint smoke, whitespace validation, and a credential-pattern scan passed. No provider call, submission, benchmark task, S3 publication, or controller behavior was added.

## 2026-08-28T14:57:48-07:00: Record-contract and local context decisions

Provenance: user contract and local implementation/validation on Python 3.12.

Canonical run and attempt identifiers use a conservative 1–64 character lowercase key-safe profile. Event keys zero-pad safe-integer sequences so lexical listing preserves sequence order. Requests bind canonical digests of both their embedded resolved plan and embedded context manifest. Terminal records require unique logical artifact paths and require their explicit Harbor config, lock, and result digests to occur in the inventory. Zero visible terminals is represented as `unknown_or_nonterminal`, not as proof of incompletion.

Context sealing retains one immutable byte value per selected regular file, checks no-follow open plus before/after descriptor identity, mode, ctime, mtime, and size, and never constructs one concatenated bundle. These local bytes and descriptors are suitable inputs for later independent streaming uploads. S3 clients, existing-object verification, publication ordering, delayed-visibility retries, and live AWS/Tigris evidence remain unimplemented.

## 2026-08-28T15:12:40-07:00: Record and context review correction

Provenance: user-provided review findings and local implementation/validation on Python 3.12.

Event identity is scoped by controller attempt, including the object-key path and independent per-attempt monotonic sequence. Direct resolved-plan deserialization now owns the same absolute count, per-file, total-byte, safe-integer, uniqueness, and portable destination bounds as producer paths. Logical paths use 255-byte UTF-8 components and a 4095-byte total; S3 keys use the service's 1,024-byte UTF-8 limit.

Local context sealing now requires POSIX directory-FD and no-follow/nonblocking open support, traverses every parent component through pinned directory descriptors, opens the final component nonblocking before type verification, and fails explicitly elsewhere. A file's pre-read size must fit both its per-file limit and the remaining total budget, so retained content never crosses the configured total.

Terminal Harbor config, lock, and result references now bind logical path plus digest. Success requires all three. Failed and cancelled outcomes may omit bindings for artifacts that were never created; each present binding must match one inventory entry. Different roles may legitimately have equal content digests. Focused regressions and the 98-test suite passed; S3 transport and live provider behavior remain unimplemented and unproven.

## 2026-08-28T15:46:28-07:00: Immutable S3 transport semantics

Provenance: user-provided S3 review findings and local implementation/validation on Python 3.12.

Request and event publication now writes the immutable content-addressed key before bounded conflict discovery; LIST-before-PUT is not an exclusion mechanism. Terminal publication and run reads enumerate paginated visible requests, attempt events, and terminals, validate canonical bodies and terminal dependencies, and treat visible request/event identity collisions or corruption as conflict. A bounded clean observation does not prove permanent uniqueness: lagged conflicts may surface on later reads, while zero visible terminals remains unknown.

Existing content-addressed objects are GET-streamed and fully rehashed before reuse. Files above the configured threshold use boto3 managed multipart upload with bounded chunks; the full digest remains in the key and metadata. Multipart verification checks size and metadata without comparing a composite service checksum to the full digest. The AWS/Tigris-parameterized fake exercises independent HEAD/GET/LIST lag, pagination, concurrent writers, corrupted bytes and metadata, bounded reads, delayed conflicts, and a mocked size above 5 GiB. All 153 tests, Ruff check/format, ty, uv lock check, and package build passed locally. Live AWS and Tigris behavior remains U-005 `unproven`.

## 2026-08-28T15:49:19-07:00: S3 evidence-count correction

Provenance: local follow-up validation after strengthening visible-identity accounting.

The preceding entry's 153-test count is superseded by 155 passing tests. Visible request, event, and terminal key identities now remain part of conflict accounting even when the corresponding body is corrupt; corruption and the independent identity collision are both reported.

## 2026-08-28T16:08:06-07:00: P2 read-only doctor completion

Provenance: user contract and local Python 3.12 validation. `doctor` remains offline without client construction or credential lookup; explicit `--online` performs only `HeadBucket` and `ListObjectsV2(MaxKeys=1)` for the effective AWS or Tigris profile and reports writes as `unproven`. The 170-test suite, Ruff, ty, lock and diff checks, package inspection, isolated installed-CLI smoke, and audited secret scan passed. Live AWS/Tigris reads, publication, and writes remain U-005 `unproven`.
