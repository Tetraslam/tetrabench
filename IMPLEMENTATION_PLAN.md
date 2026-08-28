# Tetrabench Implementation Plan

## Status

- Project state: P0 is complete at public baseline commit [`5400d401467d2550334f375f324a212eae946dbf`](https://github.com/Tetraslam/tetrabench/commit/5400d401467d2550334f375f324a212eae946dbf); implementation has not started.
- Current action: freeze the RFC 8785/JCS profile and its golden-byte contract fixture in E-013 before serializer code.
- Next action: after E-013 passes, begin P1 with typed configuration and canonical serialization. Controller code remains blocked on the child-observation contract fixture in E-019.
- Canonical record updated: 2026-08-27.

## Systems-Design Working Record

| Field | v1 contract |
| --- | --- |
| Behavioral contract | Submit a strict resolved run plan, execute Harbor through Docker locally or a deployed Modal Function in detached cloud mode, and publish native Harbor artifacts durably without inventing trace or result semantics. |
| Atomic unit | One run ID bound to exactly one request digest. Each controller start has its own append-only attempt ID, event sequence, and Harbor jobs directory; one valid terminal record commits the run outcome. |
| Largest real shape | A batch of independent runs whose plans are each under 2 MiB and whose explicitly selected context files are individually content-addressed. No batch-wide transaction is promised. |
| Authority | Harbor job files own trace/result semantics and lifecycle; Modal FunctionCall owns active controller execution; the named Modal Volume owns in-progress cloud job files; immutable S3 records own durable publication; local state is a receipt/cache only. |
| Trusted inputs | Strict typed configuration, a canonical resolved-plan JSON document, and explicitly selected local context files. Credentials are obtained from provider chains and are never serialized into plans. |
| Lifecycle owners | The submitter publishes the request and spawns; the controller records attempts, reconciles, and publishes; Harbor owns task/trial/session/sandbox lifecycle; cancellation publishes intent, drives the controller to quiescence, then performs repeated child cleanup sweeps. |
| Native primitives | Harbor v0.22.0 artifacts and execution backends, Modal deployed Functions/FunctionCall/Volume/Sandbox primitives, S3 object storage, and Docker for attached local execution. |
| Capability gaps | Modal has no call idempotency key; Tigris visibility may lag across regions; nested Modal authentication, supported Harbor child observation, live AWS/Tigris behavior, and detached execution lack faithful evidence. |
| Planned faithful evidence | Version-matched unit/contract tests, a real Docker smoke, a deployed Modal smoke with interruption/reconciliation, AWS and Tigris publication smokes, cancellation evidence, and artifact inspection. |

Correctness-critical unknowns remain `unproven` until the corresponding evidence row passes.

## V1 Behavioral Contract

### Runtime and dependencies

- [D-001] Use Harbor v0.22.0 native ATIF, results, and job artifacts. Do not introduce OpenTraces.
- [D-002] Pin `harbor[modal]==0.22.0` and `modal==1.5.4` exactly.
- [D-003] Make a detached deployed Modal Function controller the default execution path.
- [D-004] Expose Docker as an explicit local development/execution capability. Never describe Docker execution as detached; Modal is the detached cloud path.

### Configuration and resolved plans

- [D-005] Use one strict typed S3-compatible storage configuration with explicit provider variants:
  - Tigris defaults to endpoint `https://t3.storage.dev` and region `auto`.
  - AWS uses the standard endpoint, region, and credential chain; it receives no Tigris defaults.
- [D-006] Serialize one strict canonical resolved-plan JSON document under 2 MiB. Reject unknown fields and non-canonical representations. Compute SHA-256 over the canonical bytes and verify size and digest before upload and again in the controller before execution.
- [D-007] Never serialize credentials or ambient environment state into the resolved plan.

The review corrections D-023..D-037 below supersede D-006 where they are more specific. P1 serializer work may not start until E-013 freezes the JCS profile and golden-byte fixture requirements.

### Submission and execution ownership

- [D-008] `superseded by D-029`: Submit in this order:
  1. atomically write a local `prepared` receipt;
  2. upload the immutable S3 request;
  3. spawn the deployed controller;
  4. atomically add the returned FunctionCall ID to the local receipt.
- [D-009] `superseded by D-029`: The controller records `current_function_call_id()` plus execution events to S3 and the named Volume.
- [D-010] `superseded by D-029`: A prepared request without controller evidence is never auto-resubmitted. Modal has no call idempotency key, so recovery requires an explicit operator decision.
- [D-011] `superseded by D-030..D-032`: Configure the controller with `retries=0`. Reconcile Modal preemption/replay by run ID and deterministic child Harbor session names rather than assuming exactly-once execution.
- [D-012] Local state is only a submission receipt/cache. Do not make it authoritative for active or terminal execution.

### Immutable context v1

- [D-013] `superseded by D-036`: Context is a manifest of explicitly selected files. Each entry contains a normalized destination, file mode, SHA-256 digest, byte size, and content-addressed S3 object key.
- [D-014] `superseded by D-036`: Reject absolute destinations, traversal, duplicate normalized destinations, unsupported file types, and digest or size mismatches.
- [D-015] Do not clone ambient home directories or canonicalize directories in v1. Directory and remote context are deferred.

### Storage and terminal state

- [D-016] `superseded by D-024..D-028`: Use content-addressed objects and append-only per-run records/events. The earlier conceptual layout was:

```text
objects/sha256/<digest>
runs/<run-id>/request.json
runs/<run-id>/events/<event-id>.json
runs/<run-id>/artifacts/...
runs/<run-id>/terminals/<terminal-sha>.json
```

- [D-017] `corrected and superseded by D-027..D-028`: Terminal publication is immutable. Zero terminal objects was previously described as incomplete, one as terminal, and more than one as conflict requiring inspection.
- [D-018] `superseded by D-024..D-028`: Do not add mutable `latest` pointers, rename protocols, or depend on unverified Tigris conditional writes.
- [D-019] The named Modal Volume stores in-progress Harbor job files. Controller-owned and Harbor-owned paths are disjoint, with explicit commit/reload boundaries. S3 receives durable publication.
- [D-020] Preserve native Harbor job files as the source of trace and result meaning. Do not add a custom run database.

### Cancellation

- [D-021] `superseded by D-030, D-032, and D-033`: Record child sandbox identities as append-only execution evidence.
- [D-022] `superseded by D-033`: Cancellation first discovers and terminates recorded child Harbor sandboxes, records the outcome, and only then cancels the controller FunctionCall.

### Review-corrected protocol contracts

- [D-023] Canonical JSON is RFC 8785/JCS produced and parsed through strict Pydantic models. Parsing rejects duplicate object keys and unknown fields. Serialized plans, requests, events, and terminals contain no floating-point values. A serialized plan is at most 2 MiB.
- [D-024] Content objects use `objects/sha256/<digest>`, and their bytes must hash to `<digest>`. Publishing an existing key is idempotent only after verifying the existing bytes. Different bytes at the same key are corruption and a run conflict.
- [D-025] A request is canonical JSON at `runs/<run-id>/requests/<request-sha>.json`. Exactly one distinct request digest may exist for a run ID. Reusing the run ID is an error except when recovering the same request; a second digest makes the run conflicted.
- [D-026] An event is a canonical, content-addressed diagnostic record at `runs/<run-id>/events/<sequence>-<event-sha>.json`. Sequence increases monotonically within one controller attempt. Reusing a sequence with a different hash is a conflict. Events never determine terminal authority.
- [D-027] A terminal is canonical JSON at `runs/<run-id>/terminals/<terminal-sha>.json`. It binds its schema, run ID, request digest, winning attempt ID, outcome, exact Harbor version, complete logical artifact inventory, Harbor config/lock/result digests, and evidence/warnings. Each inventory entry contains logical path, content-object digest and key, byte size, and media type. The controller uploads every object and verifies it by HEAD plus hash before it writes the terminal last. A failed run may publish a terminal only after it publishes the complete inventory of all evidence then available.
- [D-028] Readers validate all visible terminals. One valid terminal means terminal; more than one means conflict; zero means unknown or nonterminal and never proves incompletion. Readers retry for a bounded period and combine S3 observations with Modal call state because Tigris cross-region visibility may lag. V1 does not use mutable `latest`, rename, fixed-key overwrite, conditional PUT, or assumed Tigris global strong consistency.
- [D-029] Local receipts have `prepared`, `request-published`, and `submitted` phases and are replaced atomically with file and parent-directory `fsync`. The submitter publishes the request before `.spawn`; the controller records its call ID. If the submitter dies after spawn, status recovers the call ID from controller records after they become visible. An unresolved request without controller evidence is never automatically resubmitted.
- [D-030] Every controller start creates an append-only attempt ID and a distinct physical Harbor jobs directory. Before replay starts a new Harbor attempt, it reconciles terminal state, terminates surviving child sandboxes from earlier attempts, and records abandonment evidence. Tetrabench supplies deterministic Harbor job inputs and tags; it neither owns Harbor session IDs nor mutates Harbor internals.
- [D-031] The controller sets `retries=0`. Modal may still preempt and replay execution, so the controller reconciles from run and attempt records rather than assuming exactly-once execution.
- [D-032] Child sandbox IDs and tags must be observable through a supported, version-matched Harbor extension surface. The implementation mechanism remains `unproven`; no controller code may be written until a contract fixture proves observation through `JobConfig`, environment, plugin, or custom-environment surfaces.
- [D-033] Cancellation first publishes durable intent. At startup and phase boundaries, the controller checks the intent, stops creating children, terminates owned children, and publishes quiescence evidence. The canceller waits boundedly for quiescence; if none appears, it cancels the controller. After the controller stops, the canceller repeatedly sweeps children by persisted IDs or tags until none remain, or reports cleanup failure. This ordering closes the child-after-sweep race.
- [D-034] Submitter credentials permit context/request writes and status reads. Controller credentials permit event, artifact, terminal, and cleanup operations. Child agent and verifier sandboxes receive no S3 publication credentials unless a future task defines separate scoped access. Buckets are private and encrypted by default; bucket or prefix policy and run-ID/path validation enforce scope. V1 has no delete permission. Configuration and receipts contain no secret values.
- [D-035] Harbor logs and artifacts are sensitive and may contain workload-emitted secrets. Tetrabench guarantees only that it does not deliberately serialize credentials. It preserves native bytes in private storage; public sanitization and export are deferred.
- [D-036] Context v1 accepts at most 256 explicitly selected regular files, 16 MiB per file and 128 MiB total; configuration may only lower these limits. It rejects symlinks and special files. For each source, it opens once with no-follow semantics, compares `fstat` before and after reading, and rejects mutation. It normalizes the destination to one unique relative POSIX path with no absolute form, `..`, or collision, and normalizes mode to `0755` when any executable bit is set or `0644` otherwise. It uploads each file and the canonical manifest by content digest. Execution materializes only bytes whose size and digest verify.
- [D-037] Docker is explicit attached local development execution; detached Modal remains the default submission mode. Harbor owns task, trial, session, and sandbox lifecycle. Tetrabench uses supported `JobConfig`, environment, plugin, or custom-environment extension surfaces only when a contract fixture proves a native capability gap.

## Authorities, Lifecycle, and Security Boundaries

| Boundary | Authority and rule |
| --- | --- |
| Client to S3 | The client may prepare immutable requests and context objects. Upload completion is not execution evidence. |
| Client to Modal | The returned FunctionCall ID identifies active cloud execution. Missing call ID after spawn is an ambiguous failure, not permission to retry automatically. |
| Controller to Harbor | The controller supplies deterministic job inputs and tags. Harbor owns session IDs and task/trial/session/sandbox lifecycle. Child observation must use a proven supported extension surface. |
| Controller/Harbor to Volume | Each writer owns disjoint paths. Readers reload after a writer commits; no concurrent same-file writes are allowed. |
| Volume to S3 | Volume data is in progress; successful immutable S3 publication makes it durable. |
| Terminal readers | Readers validate visible terminal objects: one is terminal, many conflict, and zero is unknown. They retry boundedly and combine that view with Modal call state. |
| Credentials | Submitter and controller use separate scoped credentials. Children receive no S3 publication credentials. Plans, receipts, and configuration contain no secret values. Native private artifacts may contain workload-emitted secrets. |
| Context | Only manifest-listed regular files cross the local/cloud boundary. Destination normalization and digest verification are mandatory on both sides. |
| Cancellation | Durable intent precedes quiescence and controller cancellation. Repeated post-controller sweeps are scoped to persisted run-owned child IDs or tags. |

## Decision Ledger

### Accepted

| ID | Decision | Status | Rationale |
| --- | --- | --- | --- |
| D-001..D-004 | Harbor-native artifacts, exact pins, Modal default, Docker local only | accepted | Keeps execution and artifact semantics on pinned native interfaces. |
| D-005..D-007 | Typed provider config and canonical checked plans | accepted; D-006 narrowed by D-023 | Prevents provider-default leakage, ambiguous payloads, and credential serialization. |
| D-008..D-012 | Receipt/request/spawn/call-ID sequence and explicit ambiguous recovery | D-008..D-011 superseded by D-029..D-032; D-012 accepted | Preserves the original submission decision while the corrected contract adds durable phases and attempt recovery. |
| D-013..D-015 | Explicit immutable file manifest | D-013..D-014 superseded by D-036; D-015 accepted | Preserves the explicit-file boundary with race, type, path, mode, and size rules. |
| D-016..D-020 | Content addressing, append-only records, immutable terminals, native semantics | D-016..D-018 superseded by D-024..D-028; D-019..D-020 accepted | Keeps native semantics while correcting keys and reader authority. |
| D-021..D-022 | Child-first cancellation | superseded by D-030, D-032, and D-033 | The corrected protocol adds supported observation, durable intent, quiescence, and post-controller sweeps. |
| D-023..D-028 | RFC 8785 records, content-addressed objects, immutable request/event/terminal protocol, lag-aware readers | accepted; supersedes D-006 and D-016..D-018 where more specific | Defines byte identity, run conflict, publication order, and terminal authority without unsupported S3 primitives. |
| D-029..D-032 | Durable receipt phases, physical attempts, preemption reconciliation, child-observation proof gate | accepted; supersedes D-008..D-011 and D-021 where more specific | Makes ambiguous submission and replay recoverable without claiming Harbor-owned IDs. |
| D-033 | Intent-first cancellation, quiescence, controller cancellation, and repeated child sweeps | accepted; supersedes D-022 | Closes the child-after-sweep race. |
| D-034..D-035 | Split credentials, private encrypted storage, no delete, and sensitive native-byte handling | accepted | Limits publication authority without claiming tetrabench can sanitize workload output. |
| D-036..D-037 | Bounded immutable files and Harbor-owned lifecycle through proven extensions | accepted; supersedes D-013..D-014 and narrows D-004 | Defines the context race boundary and prohibits unsupported Harbor mutation. |

### Superseded or Rejected

| ID | Prior direction | Status | Replacement |
| --- | --- | --- | --- |
| S-001 | OpenTraces as a v1 trace format | superseded | Harbor v0.22.0 native ATIF/results/job artifacts, D-001. |
| S-002 | Present Docker as detached execution | superseded | Docker is attached local execution; deployed Modal is detached, D-003/D-004. |
| S-003 | Treat local state as run authority | superseded | Local state is a receipt/cache, D-012. |
| S-004 | Auto-resubmit a prepared request without a call ID | rejected | Require explicit operator recovery, D-029. |
| S-005 | Use mutable `latest`, object rename, or assumed conditional writes | rejected | Immutable terminal enumeration, D-024..D-028. |
| S-006 | Add a custom run database | rejected | Use native lifecycle owners and append-only object records, D-020. |
| S-007 | Clone ambient home or canonicalize whole directories in v1 | deferred | Explicit file manifest, D-015 and D-036. |
| S-008 | `runs/<run-id>/request.json` fixed request key | superseded | Content-addressed request path and one-request-digest run binding, D-025. |
| S-009 | `runs/<run-id>/events/<event-id>.json` without attempt sequence/hash rules | superseded | Canonical sequence-and-hash event records, D-026. |
| S-010 | Zero visible terminals proves an incomplete run | corrected | Zero is unknown/nonterminal and must be combined with bounded retry and Modal state, D-028. |
| S-011 | Deterministic child Harbor session names are owned by tetrabench | superseded | Harbor owns session IDs; tetrabench provides deterministic job inputs/tags, D-030 and D-037. |
| S-012 | One pre-controller child sweep is enough for cancellation | superseded | Intent, quiescence, controller stop, and repeated post-controller sweeps, D-033. |

## Unresolved and Unproven

- [ ] [U-001] `unproven`: live nested Harbor Modal authentication through the supported version-matched path. Resolve with a deployed smoke before claiming nested Modal execution works.
- [ ] [U-002] `unproven`: exact Harbor v0.22.0 native job directory and artifact paths consumed by publication. Resolve against the pinned package and a real job.
- [ ] [U-003] `unproven`: replay/preemption behavior with physical attempt reconciliation under a real interruption.
- [ ] [U-004] `unproven` and implementation-blocking: child sandbox IDs/tags can be observed through supported Harbor v0.22.0 extension surfaces. A contract fixture must prove this before controller code begins.
- [ ] [U-005] `unproven`: live AWS and Tigris behavior for all required v1 object operations, including existing-object byte verification and lag-aware reads. No conditional-write behavior is required.
- [ ] [U-006] `unproven`: frozen RFC 8785/JCS profile, integer/string limits, duplicate-key rejection, no-float schemas, and 2 MiB boundary tests. Freeze E-013 before P1 serializer work.
- [ ] [U-007] `unproven`: named Volume version, mount paths, and commit/reload sequence under the pinned Modal SDK.
- [ ] [U-008] `unproven`: detached Modal smoke covering disconnect, controller record visibility, and terminal publication.

## Planned Package Tree

The tree is a placement contract, not evidence that these modules exist.

```text
tetrabench/
├── __init__.py
├── cli.py
├── config.py
├── context.py
├── controller.py
├── docker.py
├── harbor.py
├── lifecycle.py
├── modal_app.py
├── plan.py
├── receipts.py
├── storage.py
└── tests/
    ├── test_config.py
    ├── test_context.py
    ├── test_lifecycle.py
    ├── test_plan.py
    ├── test_receipts.py
    └── test_storage.py
```

## Phased Checklist

### P0: Persistent planning surfaces

- [x] [P0-01] Add the repository agent contract with mandatory session-start reads.
- [x] [P0-02] Record current scope, authorities, decisions, unknowns, phases, and evidence requirements.
- [x] [P0-03] Establish append-only notes with the initial authoritative contract entry.
- [x] [P0-04] Replace the generated empty README with verified current-status documentation.
- [x] [P0-05] Add the relative `CLAUDE.md -> AGENTS.md` compatibility symlink.
- [x] [P0-06] Commit the reviewed planning surfaces as the durable baseline at [`5400d401467d2550334f375f324a212eae946dbf`](https://github.com/Tetraslam/tetrabench/commit/5400d401467d2550334f375f324a212eae946dbf).

Acceptance: complete. All planning surfaces exist in public commit [`5400d401467d2550334f375f324a212eae946dbf`](https://github.com/Tetraslam/tetrabench/commit/5400d401467d2550334f375f324a212eae946dbf), links and the `CLAUDE.md` symlink resolve, Markdown structure is present, diff whitespace checks pass, and no implementation is represented as complete. E-024 records the evidence.

### P1: Typed configuration and canonical plans

- [ ] [P1-01] Add exact dependency pins.
- [ ] [P1-02] Implement strict AWS/Tigris configuration variants.
- [ ] [P1-03] Freeze the RFC 8785/JCS profile and golden-byte contract fixture before serializer implementation.
- [ ] [P1-04] Implement strict canonical plans, requests, events, and terminals with duplicate-key rejection, no floats, plan size limit, and two-sided SHA-256 verification.
- [ ] [P1-05] Test unknown fields, provider separation, golden bytes, duplicate keys, float rejection, tampering, and boundary sizes.

Acceptance: E-013 is frozen before serializer code; version-matched tests prove D-002, D-005..D-007, and D-023; malformed or oversized inputs fail closed.

### P2: Immutable context and S3 records

- [ ] [P2-01] Implement explicit-file manifest construction and normalization.
- [ ] [P2-02] Implement verified content-object uploads and conflict handling.
- [ ] [P2-03] Implement content-addressed requests, sequenced events, complete artifact inventories, and terminal-last publication.
- [ ] [P2-04] Test one/many terminal interpretation, zero-as-unknown behavior, lag-aware retry, and conflicting records.

Acceptance: tests and AWS/Tigris smokes prove D-024..D-028 and D-036 without mutable pointers, fixed-key overwrite, conditional writes, or assumptions of global strong consistency.

### P3: Receipts and submission

- [ ] [P3-01] Implement atomic, fsynced `prepared`, `request-published`, and `submitted` receipt phases.
- [ ] [P3-02] Implement immutable request upload followed by deployed controller spawn.
- [ ] [P3-03] Make ambiguous prepared-without-evidence recovery explicit and non-automatic.
- [ ] [P3-04] Test crash boundaries around every submission step.

Acceptance: fault injection proves D-012 and D-029, including durable phase transitions, call-ID recovery, and no automatic duplicate spawn.

### P4: Harbor execution paths

- [ ] [P4-01] Implement attached local Docker execution.
- [ ] [P4-02] After E-019 passes, implement the deployed Modal controller with `retries=0` and self-recorded FunctionCall ID.
- [ ] [P4-03] After E-019 passes, integrate Harbor v0.22.0 with distinct attempt job directories, deterministic job inputs/tags, and supported child observation.
- [ ] [P4-04] Implement disjoint Volume paths and explicit commit/reload boundaries.
- [ ] [P4-05] Run Docker and deployed Modal smokes, including the nested authentication sentinel.

Acceptance: E-019 passes before controller implementation; real runs publish inspectable native Harbor artifacts; Docker is never claimed detached; U-001, U-002, U-007, and U-008 are resolved or remain visibly `unproven`.

### P5: Reconciliation, interruption, and cancellation

- [ ] [P5-01] Reconcile terminal and prior attempt state before creating a new physical Harbor attempt.
- [ ] [P5-02] Record child sandbox identities/tags and abandonment evidence through the proven extension surface.
- [ ] [P5-03] Implement intent-first cancellation, bounded quiescence wait, controller cancellation, and repeated post-controller child sweeps.
- [ ] [P5-04] Exercise preemption/replay, duplicate-controller conflict, partial publication, and cancellation failure boundaries.

Acceptance: faithful cloud tests prove D-030..D-033; physical attempts remain inspectable, cancellation closes the child-after-sweep race, and conflicting terminals remain visible instead of being overwritten.

### P6: Verified user surface

- [ ] [P6-01] Add CLI/API surfaces only after their underlying paths pass acceptance.
- [ ] [P6-02] Document only commands and behavior exercised in clean-environment smokes.
- [ ] [P6-03] Complete security review and secret scan.

Acceptance: a new user can follow the README against released or pinned components and reproduce the documented behavior.

## Evidence Table

| Evidence ID | Claim | Required evidence | State | Reference |
| --- | --- | --- | --- | --- |
| E-001 | Planning content was reviewed | File/symlink inspection, structure/link checks, `git diff --check`, secret scan | reviewed; does not complete P0 | Local validation on 2026-08-27; corrected by E-024 |
| E-002 | Exact pins resolve together | Clean dependency resolution and import/version assertions | unproven | P1 |
| E-003 | Plans are canonical, bounded, and checked twice | Golden bytes, round-trip, tamper, unknown-field, and 2 MiB boundary tests | superseded by E-013 | P1 |
| E-004 | AWS and Tigris share one typed implementation safely | Provider-specific config tests plus live object round trips | unproven | P2 |
| E-005 | Submission cannot silently duplicate after ambiguity | Fault injection at four submission boundaries | unproven | P3 |
| E-006 | Docker local execution produces native Harbor artifacts | Real pinned Docker smoke and artifact inspection | unproven | P4 |
| E-007 | Deployed Modal execution survives client disconnect | Real deployed Function spawn, disconnect, status, and publication smoke | unproven | P4 |
| E-008 | Volume boundaries preserve in-progress job files | Real disjoint-writer commit/reload smoke | unproven | P4 |
| E-009 | Nested Harbor Modal authentication works | Live sentinel smoke with pinned dependencies | unproven | U-001/P4 |
| E-010 | Replay reconciles without duplicate child sessions | Forced interruption/preemption test and run record inspection | superseded by E-018 | P5 |
| E-011 | Cancellation terminates children first | Live cancellation test with recorded child and controller identities | superseded by E-020 | P5 |
| E-012 | Terminal conflicts remain observable | Concurrent conflicting publication test yielding more than one terminal | unproven | P5 |
| E-013 | RFC 8785/JCS profile is frozen before serializer work | Written profile plus version-matched golden bytes for every serialized record, duplicate-key rejection, no-float model checks, and size boundaries | unproven; blocks P1 serializer code | U-006/P1 |
| E-014 | Content keys bind verified bytes | Existing-object same-byte and mismatched-byte contract tests plus live AWS/Tigris round trips | unproven | D-024/P2 |
| E-015 | A run ID admits one request digest | Idempotent same-request recovery and second-digest conflict tests | unproven | D-025/P2 |
| E-016 | Terminal-last publication and lag-aware reads preserve authority | Complete-inventory checks, HEAD/hash verification, zero/one/many tests, delayed-visibility fixture, and Modal-state combination | unproven | D-027..D-028/P2 |
| E-017 | Receipt phases recover post-spawn ambiguity | File and parent-directory fsync tests plus fault injection and delayed controller-record recovery | unproven | D-029/P3 |
| E-018 | Replay preserves physical attempts | Forced replay proves distinct job directories, prior-child termination, and abandonment evidence before a new Harbor attempt | unproven | D-030..D-031/P5 |
| E-019 | Supported Harbor surfaces expose child IDs/tags | Version-matched contract fixture using only supported `JobConfig`, environment, plugin, or custom-environment hooks | unproven; blocks all controller code | D-032/U-004/P4 |
| E-020 | Cancellation closes the child-after-sweep race | Fault injection creates a child near cancellation; intent/quiescence/controller stop/repeated sweeps leave none or report cleanup failure | unproven | D-033/P5 |
| E-021 | Credential boundaries prevent child publication | Policy tests for submitter/controller scopes, no-delete access, private encryption, path validation, and child environment inspection | unproven | D-034/P6 |
| E-022 | Immutable context rejects races and unsafe files | No-follow, before/after `fstat`, mutation, symlink/special-file, path, mode, count, per-file, total-size, and materialization tests | unproven | D-036/P2 |
| E-023 | Native sensitive bytes remain private without false sanitization claims | Security review, private-storage check, deliberate-credential serialization tests, and documented export deferral | unproven | D-035/P6 |
| E-024 | P0 has a durable baseline | Commit containing the planning surfaces plus link/symlink, whitespace, and secret checks against committed content | passed | [`5400d401467d2550334f375f324a212eae946dbf`](https://github.com/Tetraslam/tetrabench/commit/5400d401467d2550334f375f324a212eae946dbf) |
| E-025 | Review findings are dispositioned consistently | Decision, supersession, unresolved, phase, and evidence cross-check covering D-023..D-037 and S-008..S-012 | complete; durable in E-024 | 2026-08-27 review correction |

## Deferred Capabilities

- [ ] [F-001] Tailscale service access.
- [ ] [F-002] Filesystem profiles, mounts, and tmpfs.
- [ ] [F-003] Richer context directories and remotes.
- [ ] [F-004] Computer and browser use.
- [ ] [F-005] Full VM and desktop images.
- [ ] [F-006] Snapshots.
- [ ] [F-007] Detached Docker or other detached controllers.

These are tracked capabilities, not v1 implementation tasks.

## Task Catalogs

### Systems Design

This benchmark category is empty. It is not an implementation work queue.

### GitHub Workflow

This benchmark category is empty. It is not an implementation work queue.
