# Tetrabench Implementation Plan

## Status

- Project state: P0 through P3 are complete for local contract evidence. The P4 detached-controller foundation now has real Modal 1.5.4 App construction, distribution-installed controller images, Environment creation, descriptor-safe artifact collection, terminal-first startup reconciliation, terminal-commit fencing, bounded failure evidence, the E-019 Harbor environment subclass, and a real injectable child observer. No controller was deployed and Harbor was not run; AWS conditional writes, nested Modal, real Volume semantics, and live cleanup remain `unproven`.
- Current action: `controller info` and confirmed `controller deploy` expose exact profile resource names without secret values. Submission targets the profile Modal environment. The deployed Function deliberately uses an unavailable-runner sentinel until P5 rather than claiming Harbor execution works.
- Next action: implement the real HarborRunner in P5, then run AWS, paid Modal, interruption, terminal-publication, Volume, nested-authentication, and running-cancellation smokes.
- Canonical record updated: 2026-08-28.

## Systems-Design Working Record

| Field | v1 contract |
| --- | --- |
| Behavioral contract | Submit a strict resolved run plan, execute Harbor through Docker locally or a deployed Modal Function in detached cloud mode, and publish native Harbor artifacts durably without inventing trace or result semantics. |
| Atomic unit | One conditional write of `runs/<run-id>/admission.json`; the record binds one run ID and request/plan digest pair and retains every revision. Immutable request, event, artifact, and terminal objects remain independently published. |
| Largest real shape | A batch of independent runs whose plans are each under 2 MiB and whose explicitly selected context files are individually content-addressed. No batch-wide transaction is promised. |
| Authority | The admission record owns coordination and one FunctionCall owner; Modal FunctionCall owns active execution; Harbor job files own trace/result semantics; the named Modal Volume owns in-progress files; immutable terminal objects own terminal proof; local state is evidence/cache only. |
| Trusted inputs | Strict typed configuration, a canonical resolved-plan JSON document, and explicitly selected local context files. Credentials are obtained from provider chains and are never serialized into plans. |
| Lifecycle owners | The submitter publishes the request, creates prepared admission, and spawns without claiming it. The winning controller CAS-claims running with its actual FunctionCall ID, owns Harbor execution, and acknowledges terminal proof. The submitter may CAS cancellation; Harbor owns task/trial/session/sandbox lifecycle. |
| Native primitives | Harbor v0.22.0 artifacts and execution backends, Modal deployed Functions/FunctionCall/Volume/Sandbox primitives, S3 object storage, and Docker for attached local execution. |
| Capability gaps | Modal has no call idempotency key. Tigris CAS is live-proven but other cross-region observations may lag. AWS CAS is documentation-backed and live-unproven. Nested Modal, Volume, Harbor execution, and child cleanup lack faithful live evidence. |
| Planned faithful evidence | Fake CAS concurrency and stale-ETag tests, retained Tigris live proof, AWS live CAS proof, a deployed Modal smoke with interruption/reconciliation, cancellation evidence, and Harbor artifact inspection. |

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

### Frozen canonical JSON profile (E-013)

Canonical documents are RFC 8785 UTF-8 bytes produced by `rfc8785==0.1.4`. Values are strict Pydantic-compatible nulls, booleans, I-JSON safe integers, strings, arrays, and string-keyed objects, with no coercion or floats. Parsing rejects duplicate decoded keys, invalid UTF-8/JSON, non-canonical bytes, and documents over the inclusive 2 MiB limit; serialization applies the same limit. SHA-256 is the lowercase hex digest of the canonical bytes. Record schemas and unknown-field policy remain P1 work.

E-013 proves only this shared foundation and its canonical JSON golden bytes. Golden bytes for each plan, request, event, and terminal schema remain an incomplete P1 acceptance item.

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
- [D-029] `superseded by D-042`: Local receipts have `prepared`, `request-published`, and `submitted` phases and are replaced atomically with file and parent-directory `fsync`. The submitter publishes the request before `.spawn`; the controller records its call ID. If the submitter dies after spawn, status recovers the call ID from controller records after they become visible. An unresolved request without controller evidence is never automatically resubmitted.
- [D-030] Every controller start creates an append-only attempt ID and a distinct physical Harbor jobs directory. Before replay starts a new Harbor attempt, it reconciles terminal state, terminates surviving child sandboxes from earlier attempts, and records abandonment evidence. Tetrabench supplies deterministic Harbor job inputs and tags; it neither owns Harbor session IDs nor mutates Harbor internals.
- [D-031] The controller sets `retries=0`. Modal may still preempt and replay execution, so the controller reconciles from run and attempt records rather than assuming exactly-once execution.
- [D-032] Child sandbox IDs and tags are observed through a Harbor v0.22.0 custom `ModalEnvironment` import path. The adapter overrides public `start` and `stop`, preserves Harbor's session ID, adds tetrabench run/attempt labels, resolves the child with public `Sandbox.from_name`, and sweeps with public tag-filtered `Sandbox.list`. Tetrabench rejects Modal sandbox v2 for this pinned path and does not fork `Trial` or read Harbor private fields.
- [D-033] `superseded by D-042`: Cancellation first publishes durable intent. At startup and phase boundaries, the controller checks the intent, stops creating children, terminates owned children, and publishes quiescence evidence. The canceller waits boundedly for quiescence; if none appears, it cancels the controller. After the controller stops, the canceller repeatedly sweeps children by persisted IDs or tags until none remain, or reports cleanup failure. This ordering closes the child-after-sweep race.
- [D-034] Submitter credentials permit context/request writes and status reads. Controller credentials permit event, artifact, terminal, and cleanup operations. Child agent and verifier sandboxes receive no S3 publication credentials unless a future task defines separate scoped access. Buckets are private and encrypted by default; bucket or prefix policy and run-ID/path validation enforce scope. V1 has no delete permission. Configuration and receipts contain no secret values.
- [D-035] Harbor logs and artifacts are sensitive and may contain workload-emitted secrets. Tetrabench guarantees only that it does not deliberately serialize credentials. It preserves native bytes in private storage; public sanitization and export are deferred.
- [D-036] Context v1 accepts at most 256 explicitly selected regular files, 16 MiB per file and 128 MiB total; configuration may only lower these limits. It rejects symlinks and special files. For each source, it opens once with no-follow semantics, compares `fstat` before and after reading, and rejects mutation. It normalizes the destination to one unique relative POSIX path with no absolute form, `..`, or collision, and normalizes mode to `0755` when any executable bit is set or `0644` otherwise. It uploads each file and the canonical manifest by content digest. Execution materializes only bytes whose size and digest verify.
- [D-037] Docker is explicit attached local development execution; detached Modal remains the default submission mode. Harbor owns task, trial, session, and sandbox lifecycle. Tetrabench uses supported `JobConfig`, environment, plugin, or custom-environment extension surfaces only when a contract fixture proves a native capability gap.
- [D-038] Record identifiers are 1–64 lowercase ASCII alphanumeric, dot, underscore, or hyphen characters and begin alphanumerically. Event keys use zero-padded safe-integer sequences. Requests embed and digest both the resolved plan and context manifest, terminals require the named config/lock/result digests to occur in the unique-path inventory, and zero visible terminals is represented explicitly as `unknown_or_nonterminal`. Context sealing retains one immutable `bytes` value per file rather than a concatenated bundle; transport may later stream those independently.
- [D-039] This decision supersedes D-026, D-027, D-036, and D-038 where more specific. Event keys are attempt-scoped at `runs/<run-id>/events/<attempt-id>/<sequence>-<event-sha>.json`, and sequence monotonicity is checked independently per attempt. Direct `ResolvedPlan` validation enforces 256 files, 16 MiB per file, 128 MiB total, safe-integer sizes, unique portable logical destinations, and UTF-8 byte limits of 255 per path component and 4095 per path; every S3 key is at most 1,024 UTF-8 bytes. On supported POSIX systems, context sources are traversed component by component with directory FDs and no-follow directory opens; the final component is opened no-follow and nonblocking before regular-file verification. Unsupported platforms fail explicitly. Each read is bounded by the remaining total before allocation. Terminal config, lock, and result fields bind both logical path and digest; successful terminals require all three, failed or cancelled terminals may omit artifacts that do not exist, and every present binding must match the inventory. Roles may share content digests.
- [D-040] Immutable request and event keys are published idempotently before bounded conflict discovery; LIST-before-PUT is never treated as exclusion. Terminal publication and run reads enumerate paginated visible requests, attempt-scoped events, and terminals across a bounded retry window. More than one request digest per run or event hash per attempt/sequence is conflict; all visible canonical records and terminal dependencies are validated, and corruption becomes conflict state on reads. Zero terminals remains unknown. A later read may conflict when a previously lagged record becomes visible. Existing content-addressed objects are GET-streamed and fully rehashed before reuse. File upload uses managed boto3 multipart transfer above a threshold, retains the full SHA-256 in key and metadata, verifies post-upload size and metadata, and does not compare a multipart composite service checksum with the full digest.
- [D-041] `doctor` is offline by default and does not construct a provider client, consult a credential chain, or call a provider. Explicit `doctor --online` uses the selected effective storage configuration to construct one client, call `HeadBucket`, and call `ListObjectsV2` with `MaxKeys=1` under the configured prefix. It never calls a mutation API and always reports storage writes as `unproven`. Human success uses Rich on stdout; `--json` emits canonical JSON on stdout, while errors remain on stderr.
- [D-042] This decision supersedes D-018, D-029, D-033, D-040, and S-004/S-005 where more specific. One frozen canonical `AdmissionRecord` lives at fixed key `runs/<run-id>/admission.json` as the explicit mutable exception to content-addressed publication. Revision zero is `prepared`; every overwrite carries the complete contiguous revision history, RFC 3339 UTC integer-second timestamps, run ID, request/plan digests, state, the one immutable owner FunctionCall ID once claimed, and a terminal digest only in `terminal`. Creation uses `PutObject(IfNoneMatch="*")`; update uses `PutObject(IfMatch=<read-etag>)`; 404/409/412 conditional failures are typed CAS conflicts. Allowed transitions are `prepared→running|cancelled`, `running→cancelling|terminal|failed`, `cancelling→cancelled|terminal|failed`, `failed→terminal`, and owned `cancelled→terminal` solely to reconcile already-published terminal proof after a cancellation race. An unowned prepared cancellation is final. `failed` means the owning controller ended without terminal proof; it is coordination state, never terminal authority and never permission to auto-recover. The submitter publishes the immutable request, creates or observes matching prepared admission, then spawns without claiming ownership. Multiple explicit submissions/recoveries may spawn while prepared; each controller must CAS its actual FunctionCall ID into `running`, and losers exit before Harbor. Cancellation needs no event write: prepared cancels directly; running advances to cancelling preserving owner, cancels and polls that FunctionCall, performs repeated child sweeps, then advances to cancelled only after the call is terminal and cleanup is complete. Terminal publication remains object-last; the controller publishes terminal proof before retrying CAS acknowledgement of its digest. Immutable terminal proof dominates admission and local evidence; conflicts dominate all status. Local receipts append spawn evidence only, and their absence or corruption is a warning. Tigris CAS is live-proven by E-032; AWS semantics are documentation-backed and live-unproven.

- [D-043] A controller invocation includes run, request, plan, request-key, and resolved storage identity. Before ownership CAS, the controller reparses that invocation, fetches the exact immutable request, validates its canonical digest and embedded plan, and requires admission/request/invocation run-request-plan agreement. Before terminal publication, it repeats those reads, requires the exact admission owner in `running` or `cancelling`, and requires the terminal to bind the same run and request. Only then may immutable artifact verification and terminal-object-last publication occur, followed by admission CAS acknowledgement. Modal inspection/API/auth failures are unknown execution state; cancellation treats only successful return, remote execution failure, and expired output as evidence that a FunctionCall has stopped.
- [D-044] Status and cancellation revalidate every active admission against its immutable request and configured storage before trust or mutation; terminal controller calls without proof require attention, final cancellation CAS races reread authority, and each newly created receipt directory component is parent-fsynced before descent.
- [D-045] The P4 controller deployment is profile-isolated by a Modal environment and named Volume while retaining configured App, Function, and Secret names. The Function receives only canonical invocation bytes and their SHA-256 digest, claims with `current_function_call_id()` before attempt work, and records tetrabench, Harbor, and Modal versions in controller plan/result artifacts. The runtime accepts an injected `HarborRunner`; the deployed foundation uses an explicit unavailable sentinel until P5. Child observation uses immutable attempt events plus run-tagged public `Sandbox.list` in Harbor's `__harbor__` App, with public persisted-ID lookup, terminate-wait, and repeated empty sweeps.
- [D-046] Dynamic profile Apps use Modal 1.5.4's `serialized=True` nested-function contract. The image pip-installs the local project as a distribution after copying it into a build layer, with exact Harbor and Modal pins. Deployment hydrates `Environment.from_name(..., create_if_missing=True)` before App deployment. Controller startup validates and reconciles immutable terminal proof before claim or attempt creation, including owned failed/cancelling/cancelled admission states. Once terminal publication begins, no later immutable evidence is emitted; a completed terminal write is authoritative even when admission acknowledgement remains pending. Harbor output is untrusted: artifact collection traverses from a no-follow attempt-root descriptor, rejects links and special files, holds regular-file descriptors through streaming upload, and checks identity before and after. Failure evidence contains only bounded type, phase, and code fields.

## Authorities, Lifecycle, and Security Boundaries

| Boundary | Authority and rule |
| --- | --- |
| Client to S3 | The client publishes immutable context/request objects and creates or cancellation-CASes admission. Admission is coordination, not execution or terminal proof. |
| Client to Modal | A returned FunctionCall ID is local spawn evidence. Only prepared→running CAS makes that actual call ID the owner; the CLI never claims it. |
| Controller to Harbor | The controller supplies deterministic job inputs and tags. Harbor owns session IDs and task/trial/session/sandbox lifecycle. Child observation must use a proven supported extension surface. |
| Controller/Harbor to Volume | Each writer owns disjoint paths. Readers reload after a writer commits; no concurrent same-file writes are allowed. |
| Volume to S3 | Volume data is in progress; successful immutable S3 publication makes it durable. |
| Terminal readers | Readers validate all visible requests, events, terminals, dependencies, and admission. Conflicts dominate; one valid terminal is terminal even if admission lags; admission `terminal` without its immutable object is not terminal proof. |
| Credentials | Submitter and controller use separate scoped credentials. Children receive no S3 publication credentials. Plans, receipts, and configuration contain no secret values. Native private artifacts may contain workload-emitted secrets. |
| Context | Only manifest-listed regular files cross the local/cloud boundary. Destination normalization and digest verification are mandatory on both sides. |
| Cancellation | Admission CAS is the durable intent. Prepared cancels directly. Running preserves its owner through cancelling, owner-call cancellation/polling, and repeated run-scoped child sweeps before cancelled. |

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
| D-029..D-032 | Durable receipt phases, physical attempts, preemption reconciliation, child-observation proof gate | D-029 superseded by D-042; D-030..D-032 accepted | Admission replaces host-local submission phases and ownership recovery; attempt reconciliation and the child-observation gate remain. |
| D-033 | Intent-first cancellation, quiescence, controller cancellation, and repeated child sweeps | superseded by D-042 | Admission CAS replaces cancellation events and quiescence authority while retaining owner-call cancellation and repeated child sweeps. |
| D-034..D-035 | Split credentials, private encrypted storage, no delete, and sensitive native-byte handling | accepted | Limits publication authority without claiming tetrabench can sanitize workload output. |
| D-036..D-037 | Bounded immutable files and Harbor-owned lifecycle through proven extensions | accepted; supersedes D-013..D-014 and narrows D-004 | Defines the context race boundary and prohibits unsupported Harbor mutation. |
| D-038 | Safe record identifiers, canonical key sequence formatting, embedded request bindings, terminal digest inventory, explicit unknown read state, and per-file retained context bytes | accepted | Makes direct deserialization and local byte ownership closed and testable before storage transport exists. |
| D-039 | Attempt-scoped events, closed direct-plan bounds, dir-FD context traversal, portable UTF-8 key/path limits, and path-plus-digest terminal bindings | accepted; supersedes D-026, D-027, D-036, and D-038 where more specific | Closes direct-deserialization, parent-symlink, FIFO blocking, retained-byte, cross-attempt conflict, and artifact-role ambiguity gaps. |
| D-040 | Publish-then-discover immutable records, conflict-complete bounded reads, lag-aware verification, and managed multipart content transport | accepted; supersedes D-024..D-028 where more specific | Avoids false exclusion from LIST-before-PUT, preserves later conflict discovery, converts read corruption to state, and keeps large-file transport bounded without confusing multipart checksums with full-object digests. |
| D-041 | Offline-by-default doctor with explicit read-only provider checks and canonical machine output | accepted | Keeps credential and provider access opt-in, proves only bucket/prefix reads, and prevents diagnostics from implying write capability. |
| D-042 | Fixed-key canonical admission with conditional create/update, controller-owned claim, CAS cancellation, and terminal acknowledgement | accepted; supersedes D-018, D-029, D-033, D-040, and S-004/S-005 where more specific | Live Tigris evidence proves the missing primitive. One durable compare-and-swap record closes cross-host ownership and cancellation races without replacing immutable terminal authority. |
| D-043 | Full invocation/request/admission binding before claim and owner-gated terminal publication; inspection failures are not stop evidence | accepted; narrows D-042 terminal and cancellation authorization | Prevents stale calls from claiming, non-owners from creating terminal proof, and provider/auth failures from falsely completing cancellation. |
| D-045 | Profile-specific deployed-controller foundation, injected HarborRunner boundary, versioned metadata artifacts, and public child observation/cleanup | accepted | Keeps deployment and orchestration testable without claiming P5 Harbor execution or paid Modal evidence. |
| D-046 | Supported dynamic App/image/Environment construction, descriptor-safe artifacts, and terminal-commit fencing | accepted | Closes P4 SDK, packaging, credential-boundary, and post-terminal lifecycle findings without adding a control plane. |

### Superseded or Rejected

| ID | Prior direction | Status | Replacement |
| --- | --- | --- | --- |
| S-001 | OpenTraces as a v1 trace format | superseded | Harbor v0.22.0 native ATIF/results/job artifacts, D-001. |
| S-002 | Present Docker as detached execution | superseded | Docker is attached local execution; deployed Modal is detached, D-003/D-004. |
| S-003 | Treat local state as run authority | superseded | Local state is a receipt/cache, D-012. |
| S-004 | Auto-resubmit a prepared request without a call ID | superseded, not erased | Recovery remains explicit, but durable prepared admission rather than host-local ambiguity now gates another spawn, D-042. |
| S-005 | Use mutable `latest`, object rename, or assumed conditional writes | superseded, not erased | `latest` and rename remain rejected. Live Tigris proof now justifies the narrowly scoped fixed admission CAS exception; terminals remain immutable, D-042/E-032. |
| S-006 | Add a custom run database | rejected | Use native lifecycle owners and append-only object records, D-020. |
| S-007 | Clone ambient home or canonicalize whole directories in v1 | deferred | Explicit file manifest, D-015 and D-036. |
| S-008 | `runs/<run-id>/request.json` fixed request key | superseded | Content-addressed request path and one-request-digest run binding, D-025. |
| S-009 | `runs/<run-id>/events/<event-id>.json` without attempt sequence/hash rules | superseded | Canonical sequence-and-hash event records, D-026. |
| S-010 | Zero visible terminals proves an incomplete run | corrected | Zero is unknown/nonterminal and must be combined with bounded retry and Modal state, D-028. |
| S-011 | Deterministic child Harbor session names are owned by tetrabench | superseded | Harbor owns session IDs; tetrabench provides deterministic job inputs/tags, D-030 and D-037. |
| S-012 | One pre-controller child sweep is enough for cancellation | superseded | Owner-call cancellation/polling and repeated post-controller sweeps, D-042. |

## Unresolved and Unproven

- [ ] [U-001] `unproven`: live nested Harbor Modal authentication through the supported version-matched path. Resolve with a deployed smoke before claiming nested Modal execution works.
- [ ] [U-002] `unproven`: exact Harbor v0.22.0 native job directory and artifact paths consumed by publication. Resolve against the pinned package and a real job.
- [ ] [U-003] `unproven`: replay/preemption behavior with physical attempt reconciliation under a real interruption.
- [x] [U-004] Resolved by E-019: a version-matched contract fixture proves child identity and tag observation through the supported custom-environment import path and public Modal APIs. Live nested behavior remains U-001/P4 smoke evidence.
- [ ] [U-005] `partially proven`: Tigris `If-None-Match` create, duplicate rejection, current/stale `If-Match`, and same-ETag concurrent single-winner behavior passed E-032. Tigris immutable publication/lag behavior and all live AWS operations remain `unproven`.
- [x] [U-006] Resolved by E-013: the shared profile, safe-integer range, duplicate-key and float rejection, and inclusive 2 MiB boundary are executable and golden-byte tested. Record-specific schemas remain P1 work.
- [ ] [U-007] `unproven`: named Volume version, mount paths, and commit/reload sequence under the pinned Modal SDK.
- [ ] [U-008] `unproven`: detached Modal smoke covering disconnect, controller record visibility, and terminal publication.
- [ ] [U-009] `unproven`: live AWS `PutObject` conditional create/update behavior. AWS documents the required 200/404/409/412 semantics, but no AWS mutation was run.

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

- [x] [P1-01] Add exact Harbor and Modal dependency pins while retaining the Pydantic and JCS pins.
- [x] [P1-02] Implement strict AWS/Tigris configuration variants.
- [x] [P1-03] Freeze the RFC 8785/JCS profile and golden-byte contract fixture before serializer implementation.
- [x] [P1-04] Implement strict canonical plans, requests, events, and terminals with duplicate-key rejection, no floats, plan size limit, and two-sided SHA-256 verification.
- [x] [P1-05] Test unknown fields, provider separation, golden bytes, duplicate keys, float rejection, tampering, and boundary sizes.
- [x] [P1-06] Add strict project/profile/catalog/controller/execution/context/task-selection schemas with narrow project, profile, and typed-override precedence.
- [x] [P1-07] Add deterministic empty-section planning plus the read-only `sections`, `plan`, and `doctor` CLI surface.

Acceptance: complete. E-013 was frozen before serializer code; version-matched tests prove D-002, D-005..D-007, D-023, and D-038; malformed or oversized inputs fail closed; plan, request, event, terminal, and context-manifest schemas have exact golden bytes and digests. E-027 records the evidence.

### P2: Immutable context and S3 records

- [x] [P2-01] Implement explicit-file manifest construction and normalization.
- [x] [P2-02] Implement verified content-object uploads and conflict handling.
- [x] [P2-03] Implement content-addressed requests, sequenced events, complete artifact inventories, and terminal-last publication.
- [x] [P2-04] Test one/many terminal interpretation, zero-as-unknown behavior, lag-aware retry, and conflicting records.
- [x] [P2-05] Add offline-by-default doctor diagnostics and optional read-only AWS/Tigris bucket/prefix checks with Rich and canonical JSON output.

Acceptance at P2 completion: local immutable transport evidence passed with 170 tests; no mutable pointer or conditional write had been added. D-042/E-032 later supersede that no-conditional-write premise for the fixed admission record only. Immutable publication and terminal authority are unchanged.

### P3: Receipts and submission

- [x] [P3-01] Implement fixed-key canonical admission CAS plus atomic, append-only local spawn receipts, including newly created receipt-root parent `fsync`.
- [x] [P3-02] Implement immutable request upload, prepared admission creation/observation, then deployed controller spawn without CLI ownership claim.
- [x] [P3-03] Make recovery explicit and permit it only while durable admission remains prepared.
- [x] [P3-04] Test crash boundaries, cross-host submission, stale ETags, and duplicate spawned calls with one controller claim winner.

Acceptance: complete for local contract evidence. Fault injection covers request/admission/receipt/spawn boundaries; fake CAS covers cross-host races and explicit prepared recovery; canonical receipts append call evidence but never owner claims. Tigris CAS passed E-032. Deployed controller entrypoint and live call-ID claim remain P4 work.

### P4: Harbor execution paths

- [ ] [P4-01] Implement attached local Docker execution.
- [x] [P4-02] Implement the deployed Modal controller with `retries=0` and self-recorded FunctionCall ID. Local construction and orchestration passed; live deployment remains E-007/U-008 `unproven`.
- [ ] [P4-03] Integrate Harbor v0.22.0 with distinct attempt job directories, deterministic job inputs/tags, and the E-019 custom-environment observation path.
- [ ] [P4-04] Implement disjoint Volume paths and explicit commit/reload boundaries.
- [ ] [P4-05] Run Docker and deployed Modal smokes, including the nested authentication sentinel.

Acceptance: E-019 passes before controller implementation; real runs publish inspectable native Harbor artifacts; Docker is never claimed detached; U-001, U-002, U-007, and U-008 are resolved or remain visibly `unproven`.

P4 foundation milestone: local implementation is complete for the detached Modal controller boundary, supported App/image/Environment construction, custom Harbor environment, child observer, attempt orchestration, artifact security, terminal reconciliation, and deployment CLI. P4 acceptance remains incomplete because no real HarborRunner, deployed Function, paid Modal call, or real Volume smoke has run.

### P5: Reconciliation, interruption, and cancellation

- [ ] [P5-01] Reconcile terminal and prior attempt state before creating a new physical Harbor attempt.
- [ ] [P5-02] Record child sandbox identities/tags and abandonment evidence through the proven extension surface.
- [x] [P5-03] Implement CAS cancellation: prepared→cancelled or running→cancelling, owner FunctionCall cancellation/polling, repeated child sweeps, and cancelling→cancelled. Real Harbor observation remains P4/P5 integration work.
- [x] [P5-04] Exercise fake duplicate-controller ownership, prepared/running cancellation, stale ETags, cleanup failure, and controller-terminal/cancel races. Real preemption and cloud cancellation remain unproven.

Acceptance: faithful cloud tests prove D-030..D-033; physical attempts remain inspectable, cancellation closes the child-after-sweep race, and conflicting terminals remain visible instead of being overwritten.

### P6: Verified user surface

- [x] [P6-01] Add local-contract CLI/API surfaces after fake-backed acceptance; prepared cancellation works, while running cancellation refuses before mutation until the real child observer is installed.
- [ ] [P6-02] Document only commands and behavior exercised in clean-environment smokes.
- [ ] [P6-03] Complete security review and secret scan.

Acceptance: a new user can follow the README against released or pinned components and reproduce the documented behavior.

## Evidence Table

| Evidence ID | Claim | Required evidence | State | Reference |
| --- | --- | --- | --- | --- |
| E-001 | Planning content was reviewed | File/symlink inspection, structure/link checks, `git diff --check`, secret scan | reviewed; does not complete P0 | Local validation on 2026-08-27; corrected by E-024 |
| E-002 | Exact Harbor, Modal, Pydantic, and JCS pins resolve together | Clean dependency resolution and import/version assertions | passed | `uv lock --check`, package build, and isolated Python 3.12 wheel install/smoke on 2026-08-28 |
| E-003 | Plans are canonical, bounded, and checked twice | Golden bytes, round-trip, tamper, unknown-field, and 2 MiB boundary tests | superseded by E-013 | P1 |
| E-004 | AWS and Tigris share one typed implementation safely | Provider-specific config tests plus live object round trips | typed config passed; live behavior unproven | `tests/test_models.py`; live round trips remain P2/U-005 |
| E-005 | Submission cannot silently duplicate after ambiguity | Fault injection at four submission boundaries | unproven | P3 |
| E-006 | Docker local execution produces native Harbor artifacts | Real pinned Docker smoke and artifact inspection | unproven | P4 |
| E-007 | Deployed Modal execution survives client disconnect | Real deployed Function spawn, disconnect, status, and publication smoke | unproven | P4 |
| E-008 | Volume boundaries preserve in-progress job files | Real disjoint-writer commit/reload smoke | unproven | P4 |
| E-009 | Nested Harbor Modal authentication works | Live sentinel smoke with pinned dependencies | unproven | U-001/P4 |
| E-010 | Replay reconciles without duplicate child sessions | Forced interruption/preemption test and run record inspection | superseded by E-018 | P5 |
| E-011 | Cancellation terminates children first | Live cancellation test with recorded child and controller identities | superseded by E-020 | P5 |
| E-012 | Terminal conflicts remain observable | Concurrent conflicting publication test yielding more than one terminal | unproven | P5 |
| E-013 | RFC 8785/JCS foundation is frozen before serializer work | Written shared profile plus version-matched canonical JSON golden bytes, direct-model safe-integer validation, duplicate-key and float rejection, and size boundaries | passed | `src/tetrabench/canonical_json.py`; `tests/test_canonical_json.py`; Python 3.12 validation on 2026-08-28. Per-record schema golden bytes remain P1. |
| E-014 | Content keys bind verified bytes | Existing-object same-byte and mismatched-byte contract tests plus live AWS/Tigris round trips | local transport passed; live behavior unproven | `src/tetrabench/s3.py`; `tests/test_s3.py`; D-024/D-040/P2 |
| E-015 | A run ID admits one request digest | Idempotent same-request recovery and second-digest conflict tests | local publish-then-discover behavior passed; live behavior unproven | `src/tetrabench/s3.py`; `tests/test_s3.py`; D-025/D-040/P2 |
| E-016 | Terminal-last publication and lag-aware reads preserve authority | Complete-inventory checks, HEAD/hash verification, zero/one/many tests, delayed-visibility fixture, and Modal-state combination | local publication and retry passed; Modal combination and live behavior unproven | `src/tetrabench/s3.py`; `tests/test_s3.py`; D-027..D-028/D-040/P2 |
| E-017 | Receipt phases recover post-spawn ambiguity | File and parent-directory fsync tests plus fault injection and delayed controller-record recovery | unproven | D-029/P3 |
| E-018 | Replay preserves physical attempts | Forced replay proves distinct job directories, prior-child termination, and abandonment evidence before a new Harbor attempt | unproven | D-030..D-031/P5 |
| E-019 | Supported Harbor surfaces expose child IDs/tags | Version-matched contract fixture using a custom-environment import path and public Harbor/Modal lifecycle and lookup APIs | passed; controller design gate cleared | Persistent proof: `~/.local/share/opencode/tetrabench-research/proof/`; pinned sources: `harbor-v0.22.0/src/harbor/environments/{factory.py,modal.py}` and `modal-1.5.4/modal/sandbox.py`; D-032/U-004/P4 |
| E-020 | Cancellation closes the child-after-sweep race | Fault injection creates a child near cancellation; intent/quiescence/controller stop/repeated sweeps leave none or report cleanup failure | superseded by E-032/E-033 | D-042/P5 replaces event/quiescence authority with admission CAS, owner-call polling, and repeated sweeps. |
| E-021 | Credential boundaries prevent child publication | Policy tests for submitter/controller scopes, no-delete access, private encryption, path validation, and child environment inspection | unproven | D-034/P6 |
| E-022 | Immutable context rejects races and unsafe files | No-follow, before/after `fstat`, mutation, symlink/special-file, path, mode, count, per-file, total-size, and materialization tests | local sealing passed; cloud materialization remains P2 transport work | `src/tetrabench/context.py`; `tests/test_context.py`; D-036/P2 |
| E-023 | Native sensitive bytes remain private without false sanitization claims | Security review, private-storage check, deliberate-credential serialization tests, and documented export deferral | unproven | D-035/P6 |
| E-024 | P0 has a durable baseline | Commit containing the planning surfaces plus link/symlink, whitespace, and secret checks against committed content | passed | [`5400d401467d2550334f375f324a212eae946dbf`](https://github.com/Tetraslam/tetrabench/commit/5400d401467d2550334f375f324a212eae946dbf) |
| E-025 | Review findings are dispositioned consistently | Decision, supersession, unresolved, phase, and evidence cross-check covering D-023..D-037 and S-008..S-012 | complete; durable in E-024 | 2026-08-27 review correction |
| E-026 | P1 planning foundation is strict, deterministic, and read-only | Full tests, Ruff check/format, ty, lock check, build, isolated wheel CLI smoke, whitespace check, and secret scan | passed | 59 tests on Python 3.12, including direct record invariants, deep immutability, typed patch precedence, provider endpoints, task selection, and doctor; wheel/sdist content inspection; installed CLI plan/doctor smoke; Ruff, ty, lock, diff, and audited secret scan on 2026-08-28 |
| E-027 | P1 record contracts and local P2 context sealing are canonical and immutable | Exact schema bytes/digests, direct-deserialization invariants, deep mutation rejection, unsafe key/path rejection, context race/type/limit tests, full validation suite | passed locally; no storage-provider claim | 94 tests on Python 3.12; exact request/event/terminal/context-manifest goldens; Ruff check/format, ty, lock check, package build, diff check, and secret scan on 2026-08-28 |
| E-028 | Review-corrected record and context boundaries fail closed | Direct 257-file/per-file/total/safe-integer plan tests; parent-symlink and real-FIFO tests; per-attempt sequence reuse; pre-read remaining-budget test; UTF-8 key/path boundaries; successful/failed terminal binding tests; full local validation | passed locally; provider transport remains unproven | 98 tests on Python 3.12; Ruff check/format, ty, lock check, and package build on 2026-08-28; `src/tetrabench/{context.py,models.py,records.py,storage.py}` and focused tests |
| E-029 | Immutable S3 transport preserves content identity and exposes visible conflicts | One AWS/Tigris-parameterized behavioral suite covering publish-then-discover races, delayed conflict emergence, terminal conflict blocking, corruption-to-conflict reads, independent HEAD/GET/LIST lag, pagination, bounded stream reads, managed multipart selection above 5 GiB without allocation, and multipart checksum semantics; full local validation | passed locally; live providers remain unproven | 170 tests on Python 3.12; Ruff check/format, ty, uv lock check, package build and content inspection, isolated installed-CLI smoke, diff check, and audited secret scan on 2026-08-28; `src/tetrabench/s3.py`; `tests/test_s3.py`; D-040/P2 |
| E-030 | Doctor keeps provider access explicit and read-only | Injected store/client tests for offline and online success, authentication and missing-bucket errors, AWS/Tigris display, canonical JSON, stdout/stderr separation, and exact provider calls; isolated installed-wheel CLI inspection | passed locally; live provider reads and all writes unproven | 170 tests on Python 3.12; only `HeadBucket` and `ListObjectsV2(MaxKeys=1)` occur in online fixtures; offline fixtures reject client construction; `src/tetrabench/{cli.py,s3.py}`; `tests/test_cli.py`; D-041/P2 |
| E-031 | Local detached control preserves crash, status, and cancellation boundaries | Fault injection at every submission boundary, concurrent submitters, canonical append-only receipt inspection, Modal API fakes, S3/receipt/Modal status precedence, output expiry, child-after-sweep race, secret absence, and CLI stream/exit tests | passed locally; deployed controller, Harbor, and live cleanup unproven | 199 tests on Python 3.12; Ruff, ty, lock, build, and installed-CLI evidence recorded on 2026-08-28; `src/tetrabench/{controller,lifecycle,receipts,submission}.py`; P3/P5/P6 |
| E-032 | Fixed-key admission CAS provides durable cross-host ownership | Live private Tigris fork: `If-None-Match: *` create and duplicate 412, stale `If-Match` 412, current-ETag update, and two same-ETag updates with exactly one winner; fake stale/concurrent CAS and lifecycle races; AWS documentation review | passed on Tigris; AWS live-unproven | Report `~/.local/share/opencode/tetrabench-research/tigris-cas/2026-08-28-report.json`, SHA-256 `889c2f0fbeb1c44a4a9686a14cf3c64e58834da17019f5516d59de6c3ea96416`; temporary fork and access key removed; AWS [conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) and [PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html) semantics reviewed 2026-08-28. |
| E-033 | Admission-based detached control passes full local validation and packages cleanly | Full tests, Ruff check/format, ty, lock check, package build, isolated Python 3.12 wheel install/CLI smoke, wheel content inspection, diff check, and audited secret scan | passed locally; deployed controller/Harbor remain unimplemented | 218 tests on Python 3.12 on 2026-08-28; isolated wheel contained all six control/storage modules and ran `tetrabench --version`/`--help`; detect-secrets findings were only committed SHA-256 fixture goldens and the explicit `secret-reference-not-value` negative-test string. |
| E-034 | Controller claim, terminal publication, and cancellation fail closed at their authority boundaries | Full-identity request/admission tests, stale invocation and non-owner publication regressions, inspection-failure cancellation regression, full tests, Ruff, ty, lock check, package build, isolated wheel CLI smoke, diff check, and audited secret scan | passed locally; live Modal inspection and controller execution remain unproven | 222 tests on Python 3.12 on 2026-08-28; `src/tetrabench/{controller,lifecycle,s3,submission}.py`; focused regressions in `tests/test_{controller,lifecycle}.py`; detect-secrets findings were fixture digests and explicit non-secret Secret-name references. |
| E-035 | Detached Modal controller foundation preserves claim, attempt, Volume, publication, deployment, and child-cleanup boundaries | Fake App/Image/Volume/S3/runner/observer tests, duplicate and cancellation races, verified context materialization, replay cleanup, success terminal-last, failure evidence without false terminal, deployment confirmation dry path, full local validation and package smoke | passed locally; package/API and all live Modal/Harbor/Volume behavior remain unproven | 248 tests on Python 3.12 on 2026-08-28; Ruff check/format, ty, lock check, package build, isolated installed CLI, diff check, and audited secret scan; `src/tetrabench/{controller_runtime,harbor,modal_app}.py`. |
| E-036 | P4 review corrections preserve SDK, package, artifact, and terminal boundaries | Real Modal 1.5.4 App construction; image/project-install and distribution-metadata contract tests; no-follow symlink/special/mutation regressions; terminal-CAS and startup-reconciliation races; missing Harbor App and Environment ensure tests; secret-bearing exception regression; full local validation, build, and isolated wheel CLI | passed locally; paid Modal/Harbor/Volume behavior remains unproven | 261 tests on Python 3.12 on 2026-08-28; Ruff, ty, lock, wheel/sdist build, isolated wheel install and CLI/metadata smoke, real SDK object-construction smoke, diff check, and audited secret scan; D-046. |

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
