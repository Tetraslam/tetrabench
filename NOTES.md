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

## 2026-08-28T17:18:22-07:00: Durable admission CAS correction

Provenance: user contract; live Tigris proof on a private copy-on-write fork; AWS S3 conditional-write and PutObject documentation; local Python 3.12 implementation and validation.

This entry supersedes the prior no-conditional-write and host-local no-resubmit decisions without deleting them. One canonical mutable coordination exception now lives at `runs/<run-id>/admission.json`. It retains complete contiguous revision history, request/plan identity, integer-second UTC timestamps, one immutable owner FunctionCall ID after claim, and terminal digest acknowledgement. Conditional create uses `If-None-Match: *`; every transition uses the read ETag with `If-Match`. The CLI publishes request and prepared admission before spawn and never claims ownership. Explicit submissions or recovery may create duplicate calls while prepared, but only the controller whose actual call ID wins prepared→running CAS may enter Harbor. Cancellation is admission-based and needs no event-write permission. Immutable terminal publication remains terminal-object-last and authoritative; conflicts dominate, while local receipt absence or corruption is only a warning.

Live Tigris evidence proved exclusive create, duplicate and stale-ETag 412 responses, current-ETag update, and exactly one winner from two concurrent updates sharing an ETag. The retained report is `~/.local/share/opencode/tetrabench-research/tigris-cas/2026-08-28-report.json`, SHA-256 `889c2f0fbeb1c44a4a9686a14cf3c64e58834da17019f5516d59de6c3ea96416`. Its temporary fork and access key were removed. AWS documents the required conditional semantics, but AWS remains live-unproven. The 218-test suite, Ruff, ty, lock check, package build, isolated wheel smoke/content inspection, diff check, and audited secret scan passed. Deployed controller, Harbor, Volume, real child cleanup, and live Modal/AWS races remain unimplemented or unproven.

## 2026-08-28T17:33:13-07:00: Controller authority-boundary correction

Provenance: user-provided CAS control findings and local Python 3.12 implementation and validation.

Controller invocations now carry the plan digest in addition to run, request, key, and resolved storage identity. Claim reparses the invocation, fetches and validates the immutable request, and requires exact run/request/plan agreement with admission before CAS. Terminal publication repeats those checks, requires the exact owner while admission is running or cancelling, validates the terminal binding, publishes the terminal object last, and then CAS-acknowledges its digest. A non-owner or stale invocation cannot publish terminal proof. Cancellation now treats successful return, remote execution failure, and expired output as stopped; Modal API, authentication, and other inspection failures remain unknown and cannot finalize cancellation.

The 221-test suite, Ruff check/format, ty, lock check, package build, isolated wheel CLI smoke, diff check, and audited secret scan passed. Live deployed-controller and Modal behavior remain `unproven`.

## 2026-08-28T17:34:23-07:00: Controller evidence-count correction

Provenance: local follow-up validation after adding explicit resolved-storage identity coverage.

The preceding entry's 221-test count is superseded by 222 passing tests. The added regression proves that an invocation whose resolved storage differs from the immutable request cannot claim admission.

## 2026-08-28T18:08:10-07:00: P4 detached controller foundation

Provenance: user contract, pinned Modal 1.5.4 and Harbor 0.22.0 APIs, the retained E-019 proof, and local Python 3.12 implementation tests.

Profile deployments retain configured App, Function, and Secret names and isolate the Modal environment and named controller Volume with a deterministic profile key. The deployed Function receives canonical invocation bytes plus their SHA-256 digest, obtains its current FunctionCall ID, and enters a decorator-independent runtime that claims admission before attempt work. Attempts use exclusive directories under `/tetrabench/controller`, explicit Volume commit/reload boundaries, verified S3 request/context materialization, native job-directory publication, terminal-last CAS acknowledgement, and failure evidence without fabricated terminal completeness. Controller plan/result artifacts record the deployed tetrabench, Harbor, and Modal versions.

The supported `TetrabenchModalEnvironment` import path preserves Harbor's session ID, adds run/attempt/plan labels, rejects sandbox v2, and records public `Sandbox.from_name` identity after start and on failure. Cleanup combines persisted immutable child events with run-tagged public `Sandbox.list` under Harbor's `__harbor__` App, terminate-wait, and repeated empty sweeps. The HarborRunner remains an injected protocol, and the deployed foundation uses an explicit unavailable sentinel until P5. No paid Modal call, deployment, real Harbor run, or real Volume smoke occurred; package/API and live behavior remain `unproven`.

## 2026-08-28T19:10:00-07:00: P4 controller review correction

Provenance: user-provided P4 review findings, Modal 1.5.4 SDK inspection, and local Python 3.12 implementation and validation.

Dynamic profile Apps now use Modal's supported serialized nested-function contract. The controller image copies the local project into a build layer and pip-installs it as a distribution with metadata and exact Harbor/Modal pins; deployment hydrates the configured Environment with `create_if_missing=True` before App deployment. Real Modal 1.5.4 object construction passes without deployment.

Harbor output is treated as untrusted. Artifact collection is rooted at a descriptor-opened attempt directory, traverses every component with no-follow directory FDs, rejects links and special files, streams through held regular-file descriptors, and checks identity before and after upload. Startup checks terminal proof before claim or attempt creation and reconciles owned failed, cancelling, or cancelled admission. A published terminal remains the committed result when its admission CAS cannot be acknowledged, and no later immutable artifacts or events are emitted. Missing `__harbor__` means no children. Failure evidence records bounded type, phase, and code fields without provider messages.

The 261-test suite, Ruff, ty, lock check, wheel/sdist build, isolated wheel install and CLI/metadata smoke, real Modal App-construction smoke, diff check, and audited secret scan passed. No cloud deployment or paid Modal/Harbor execution occurred; nested authentication, real Volume behavior, and live cleanup remain `unproven`.

## 2026-08-28T18:32:00-07:00: P4 correction timestamp

Provenance: local clock check. The preceding entry's `19:10:00-07:00` heading was recorded incorrectly; its actual creation time was approximately `18:31:00-07:00`. File order preserves append order.

## 2026-08-28T18:55:07-07:00: Real Harbor 0.22 runner boundary

Provenance: user contract, pinned Harbor 0.22.0 source/API inspection, local Python 3.12 implementation, and a real attached Docker fixture run.

The production controller now uses a narrow adapter over `JobConfig`, `TaskConfig`, `AgentConfig`, `EnvironmentConfig`, `Job.create`, and `job.run`. Resolved plans carry opaque Harbor agent/model strings plus positive attempts and concurrency. Task identities resolve only below the materialized context root. Docker uses Harbor's Docker environment; Modal compiles the E-019 `TetrabenchModalEnvironment` import path with run, attempt, and plan labels plus the child-event observation path. Tetrabench does not add a provider registry.

Harbor's complete native job directory remains unchanged. The runner validates job and per-trial config, lock, and result files and validates `agent/trajectory.json` with Harbor's ATIF model when present. Missing ATIF is warning evidence rather than a synthesized trace. The integration-only oracle fixture lives under `tests/fixtures`, is absent from benchmark catalogs and installed wheels, and completed through local Docker with one verifier reward of `1.0`. No Modal resource was deployed or invoked; nested Modal authentication, Volume behavior, and live child cleanup remain `unproven`.

## 2026-08-28T19:30:00-07:00: Harbor composition boundary correction

Provenance: user-provided composition findings, Harbor 0.22 model/source inspection, focused regressions, and a real attached-Docker run through `ControllerRuntime`.

The controller now constructs its S3 store before hiding AWS/Tigris credential, token, profile, and credential-file environment sources for the entire runtime. Child lifecycle publication uses a random invocation-scoped controller-process registry key and the already-created store, with reliable cleanup. Persisted Harbor job/trial config, lock, and result models own semantic outcomes; transport objects must agree. Plans and artifact collection are bounded, and ATIF roots plus continuation files are discovered from the secure inventory. The real fixture requested standard AWS interpolation variables, received only unavailable defaults in the child, published a succeeded 14-artifact terminal with reward `1.0`, and recorded missing ATIF without fabrication. Deployed Modal, real Volume, live IAM, and nested authentication remain `unproven`.

## 2026-08-28T20:01:23-07:00: First live detached Modal and Tigris smoke

Provenance: three paid deployed runs using the source-only oracle fixture, fresh-process tetrabench/Modal/Tigris inspection, downloaded named-Volume files, native Harbor 0.22 model validation, and the retained redacted verifier/report under `~/.local/share/opencode/tetrabench-research/modal-tigris-smoke/2026-08-28/`.

E-039 passed. Final run `smoke-20260828-003` claimed admission with its actual FunctionCall, created one tagged nested Harbor Modal sandbox, retained the complete Volume attempt/job tree, published 14 verified artifacts before one terminal object, acknowledged admission revision 2 as terminal, and produced one completed trial with reward `1.0`. The fixture's credential-boundary verifier passed, Tigris remained queryable after controller completion, and no run-tagged child remained. Live evidence found and corrected two pinned-SDK mismatches: built-in `TimeoutError` from nonblocking `FunctionCall.get`, and `App.app_id` for `Sandbox.list`. All temporary keys and the temporary read policy were deleted; the private baseline bucket/prefix, non-delete controller policy/key, Modal Environment/App/Volume/Secret, and run evidence were retained. Tigris did not enforce the attempted prefix condition on `ListBucket`, so listing remains bucket-wide while object reads/writes are prefix-scoped. AWS, forced replay, and live cancellation remain `unproven`; cancellation was not improvised because the helper has no bounded long-running mode.

## 2026-08-28T20:16:51-07:00: Live-smoke review correction

Provenance: user-provided review findings, pinned Modal 1.5.4 exception inheritance inspection, and focused local regressions.

`modal.exception.FunctionTimeoutError` inherits `modal.exception.TimeoutError`, so call inspection now classifies the function timeout as terminal failure before classifying Modal or built-in nonblocking timeout exceptions as running. Plan evidence now states the six AWS credential/profile selectors the live fixture requested and the controller-only Tigris publication boundary instead of calling the children generically credential-free. E-039 no longer publishes the exact FunctionCall ID, while retaining its run, attempt, and terminal identifiers. Its retained executable verifier is qualified as requiring ambient Tigris read credentials; no dependency was added.

## 2026-08-28T20:19:16-07:00: E-040 validation

Provenance: local Python 3.12 validation after the live-smoke review correction.

All 283 tests passed, including the real attached-Docker Harbor fixture. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, an isolated wheel install with CLI/version/metadata/content inspection, and `git diff --check` passed. No live provider call or dependency change occurred.

## 2026-08-28T21:28:00-07:00: Bounded live cancellation

Provenance: retained baseline Tigris/Modal resources, one prepared cancellation, three bounded real Harbor cancellation runs, fresh-process verification, Modal billing, and local Python 3.12 validation.

E-041 passed. Final run `cancel-live-20260828-003` was observed in running admission with one persisted and tag-visible Harbor child before a fresh process invoked the real cancellation CLI. Admission retained `prepared→running→cancelling→cancelled`; the owner FunctionCall reached Modal's failed terminal state; two cancellation sweeps reached empty; repeated verifier sweeps remained empty; no immutable terminal existed; and both baseline Apps reported zero tasks and zero active containers. Prepared run `cancel-prepared-20260828-001` cancelled before spawn with no cloud compute.

Live evidence exposed two defects. The terminated controller could race durable cancelling intent to failed from its exception handler, so failure marking is now limited to running admission. Modal shutdown also exceeded the former 0.5-second owner poll window, so the bounded default is now ten seconds and remains resumable. A source-only helper copies the oracle fixture and inserts a validated 0–300-second hold without mutating the fixture, benchmark catalogs, or wheel.

The final one-shot cancellation took 7.38 seconds. All three discovery/retry runs cost `$0.00147863` in Modal's 21:00 billing interval. The temporary submitter key was deleted; the existing controller key/policy and private run evidence were retained. All 289 tests, Ruff check/format, ty, lock check, wheel/sdist build, source-only wheel exclusion, diff check, and an audited secret scan passed. Forced replay/preemption remains `unproven`.

## 2026-08-29T00:21:39-07:00: E-041 checkpoint correction

Provenance: user-provided checkpoint findings and local Python 3.12 validation.

Invalid source-only smoke `--hold-seconds` values now fail at the argparse boundary with concise stderr and exit status 2 before configuration, credentials, S3, or Modal are consulted. Subprocess regressions exercise the actual script entrypoint for negative, oversized, and non-integer values. The systems-design record now reflects E-039 nested Harbor child and named-Volume evidence plus E-041 live cancellation and repeated cleanup; forced interruption/preemption replay remains `unproven`.

All 292 tests passed, including the real attached-Docker Harbor fixture. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, isolated-wheel CLI/version/metadata and source-only exclusion checks, `git diff --check`, and an audited detect-secrets scan passed. The scan findings remain committed SHA-256 fixtures and explicit non-secret Secret-name/negative-test strings. No dependency or credential behavior changed; `botocore[crt]` was not added.

## 2026-08-29T00:36:08-07:00: Explicit detached-controller recovery state

Provenance: user-provided recovery protocol and local implementation review.

`recovering` is the durable handoff state for an owner whose Modal FunctionCall is proven terminal but whose stale Harbor children are not yet proven quiescent. Recovery may enter it only from owned `running` or `failed`, never from terminal or cancellation states and never after a running or inspection-unknown owner. Two consecutive empty child sweeps permit `recovering→prepared`; that transition clears the current owner while the immutable revision history retains prior owner evidence. A successor obtains authority only through the existing `prepared→running` claim. Terminal proof dominates at every boundary, CAS losers reread authority, cleanup failure stays resumable in `recovering`, and local recovery intent/spawn receipts remain evidence rather than authority. This decision is D-050.

## 2026-08-29T00:52:42-07:00: Recovery cleanup and replay correction

Provenance: user-provided recovery review findings on 2026-08-29.

Terminal authority stops successor work but does not prove Harbor child cleanup. Every terminal observation during recovery must still complete bounded quiescence; failure remains explicit and cleanup can be retried without spawning. Recovery on an already terminal run is cleanup-only and does not mutate terminal or admission authority. The prepared handoff does not make Modal spawn exactly once: concurrent callers may spawn multiple FunctionCalls, while only one fresh `prepared→running` CAS claimant owns the controller and may run Harbor. A new physical attempt that finds the same owner already running fails closed before Harbor; explicit recovery must later prove that owner terminal and clean its children. Actual Modal preemption behavior remains `unproven`. This correction is D-051 and supersedes D-030, D-031, and D-050 where more specific.

## 2026-08-29T03:13:25-07:00: E-043 forced-controller recovery

Provenance: retained baseline Modal/Tigris resources, direct Modal FunctionCall termination without tetrabench cancellation intent, same-region fresh-process recovery, fresh cross-region final verification, named-Volume inspection, Modal billing, and final local validation.

E-043's forced-interruption portion passed after the owner reached failed with a tagged child still visible. Admission remained active and status reported attention with no local receipt. A fresh empty-state process recovered through `failed→recovering→prepared`; two sweeps quiesced the stale child, and a successor claimed admission. Distinct old and winning attempts remained in the named Volume. Terminal publication followed all 14 artifacts, carried reward `1.0`, and advanced admission revision 6 to terminal. A second fresh empty-state process reread status and native Harbor results from S3, found no receipt, and confirmed both controller calls terminal with no tagged children. Final Modal inspection found no active container. Full identifiers remain in the retained private evidence path below.

The final run took 121 seconds from prepared admission to terminal. The full discovery session cost `$0.04201218` across the baseline controller, Harbor, and temporary smoke-driver Apps; Tigris did not report separate cost. Every temporary Tigris key was deleted, while private run and Volume evidence remain under the existing smoke prefix. The retained report and executable verification helpers are under `~/.local/share/opencode/tetrabench-research/modal-tigris-recovery/2026-08-29/`.

Live evidence exposed three recovery boundaries. Tigris Global cross-region reads returned stale admission revision zero for much longer than the documented typical sub-second window, so the mutating recovery ran in a fresh `us-east` process within the controller's consistency region; a later west-coast process verified final S3 authority. Direct `terminate_containers=True` continued tearing down the old container after its FunctionCall became terminal and could kill an immediate successor, so recovery now waits 30 seconds before mutation or spawn. Modal reused interrupted controller containers with open Volume files, so successors commit the mounted Volume before attempt setup and avoid the redundant reload that Modal rejects in that state. Child cleanup now polls listed and persisted sandboxes before treating them as running or terminating them. Cleanup observations pause between sweeps, and bounded phase/error diagnostics remain free of provider messages.

Final validation passed with 329 tests, including the real attached-Docker Harbor fixture, plus Ruff check/format, ty, `uv lock --check`, wheel/sdist build, isolated-wheel CLI/metadata/source-only exclusion, `git diff --check`, and an audited changed-file detect-secrets scan. Findings were existing non-secret Secret-name fixtures and the deliberate exception-message redaction test. True provider-initiated preemption and all live AWS behavior remain `unproven`.

## 2026-08-29T03:47:15-07:00: Recovery and provider-topology correction

Provenance: user correction, Tigris bucket-location and consistency documentation, local provider fakes, and read-only live inspection of the retained Tigris organization.

The preceding E-043 entry proves forced interruption followed by explicit recovery; it does not prove provider-initiated Modal preemption. Terminal owner proof gates one bounded 30-second settling window. Repeated child sweeps establish quiescence, while durable recovery keeps a stopped cleanup attempt resumable.

Mutable admission coordination now requires an online bucket-location preflight before submit, recovery, or cancellation mutates provider state. Standard regional AWS buckets are accepted. Tigris Single-region and Multi-region locations are accepted; Global, Dual-region, missing, and unknown locations are rejected. Tigris retains endpoint `https://t3.storage.dev` and SDK region `auto`. Status and result reads remain available for legacy Global-bucket runs.

Read-only live inspection found only Global buckets in the retained Tigris organization and confirmed the baseline location as `global`. No bucket was created or migrated, so live Single-region Tigris consistency remains `unproven`. A source-only opt-in probe can exercise conditional create and a two-writer stale-ETag race against an existing accepted AWS or Tigris bucket without recording credentials or object identity.

## 2026-08-29T03:53:52-07:00: E-044 validation

Provenance: final local validation and isolated installed-wheel inspection.

All 347 tests passed, including the real attached-Docker Harbor fixture. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, isolated-wheel installation, version/metadata and source-only probe exclusion, and `git diff --check` passed. Changed-file secret scanning reported only existing explicit Secret-name test fixtures; the provider probe accepts ambient credentials only and records neither credentials nor object identity.

## 2026-08-29T03:56:18-07:00: E-044 validation-count correction

Provenance: final regression added at the S3 mutation boundary and a repeated full local validation.

The preceding E-044 validation count is superseded by 348 passing tests. The added regression proves that direct `S3Store.create_admission` cannot bypass an unsafe bucket-topology preflight. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, and `git diff --check` passed again.

## 2026-08-29T04:07:43-07:00: Consistency-gate and probe correction

Provenance: user-provided consistency-gate/probe findings and local implementation review. No provider or live IAM mutation was authorized.

D-053 is narrowed to mutable admission and complete run-mutation entrypoints. Admission create/update remains intrinsically gated, and submit, recovery, cancellation, and controller execution preflight before creating any new run object. Generic immutable publication remains valid on Global Tigris with bounded eventual-read semantics, including retained legacy evidence. An AWS bucket-location response accepts only documented null, legacy `EU`, or pinned-SDK region values; an empty string fails closed. Moving coordination requires a newly provisioned Single-region bucket followed by copy/cutover, not an in-place location migration.

The source-only probe now uses distinct clients synchronized at the stale-ETag race, attempts deletion after every conditional-create attempt including ambiguous timeout, and boundedly verifies object absence. Probe-work and cleanup failures remain separately inspectable. Normal submitter/controller identities add `GetBucketLocation` but remain no-delete; only a temporary probe-prefix credential needs delete. Tests and policy documentation change, but no live resource does.

## 2026-08-29T04:10:53-07:00: E-045 local validation

Provenance: full local Python 3.12 validation after the consistency-gate/probe correction. No provider was called and no live resource or IAM policy was changed.

All 359 tests passed, including the real attached-Docker Harbor fixture. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, isolated-wheel installation plus CLI version and distribution metadata checks, `git diff --check`, and a changed-file detect-secrets scan passed. The probe remains source-only and excluded from the wheel by the existing package fixture.

## 2026-08-29T04:29:53-07:00: E-046 Single-region coordination cutover

Provenance: authenticated Tigris CLI and S3 APIs, Modal 1.5.4 deployment and billing APIs, current uncommitted source, a fresh empty-state verifier process, native Harbor 0.22 models, and final local Python 3.12 validation.

A new uniquely named private Tigris Single-region `iad` bucket is now the authoritative tetrabench coordination and artifact baseline. The retained Global bucket and its objects were neither modified nor copied; a safe `legacy-global` user profile preserves read access while `baseline` selects the new bucket and prefix with the retained App, Volume, and Secret names. The authenticated local credential provider required the Botocore CRT extra, so `botocore[crt]==1.43.83` and its locked `awscrt` dependency were added.

Separate policies and keys were created for the no-delete controller and disposable probe. Both received `GetBucketLocation`; object permissions were prefix-scoped. Tigris accepted but did not enforce the attempted `s3:prefix` condition on `ListBucket`, so listing remains bucket-wide. The probe passed `iad` admission, immediate GET/HEAD/LIST, conditional create, a synchronized two-client update race with one winner, and verified HEAD/LIST cleanup with zero residue. Its key and policy were deleted. A separate temporary no-delete submitter key/policy drove deployment and the smoke, then was deleted.

The retained Modal Secret was replaced without printing values, and the current source controller was redeployed to the retained App/Environment/Volume. The final smoke returned from its launcher in 10.21 seconds and reached fresh-process terminal proof 65.43 seconds after launch. Its winning attempt published 14 artifacts before terminal, completed one Harbor trial with reward `1.0`, retained its named-Volume attempt, recorded the expected nested-child lifecycle, and left zero active child or controller containers. Modal billed `$0.00136689` to the controller and Harbor Apps in that hourly interval; Tigris reported no separate cost. Full identifiers and provider evidence remain under `~/.local/share/opencode/tetrabench-research/tigris-single-region-cutover/2026-08-29/`.

Final validation passed with 359 tests, including the real attached-Docker Harbor fixture, plus Ruff check/format, ty, `uv lock --check`, wheel/sdist build, and `git diff --check`. AWS live behavior and actual provider-initiated Modal preemption remain `unproven`.

## 2026-08-29T04:51:48-07:00: E-047 final recovery/cutover correction

Provenance: user-provided final findings, local Python 3.12 validation, package inspection, and a bounded full-changed-content secret scan.

Terminal-state recovery now applies the same terminal/admission/request/plan binding checks as status before any child sweep. A mismatch raises conflict without cleanup mutation. The provider probe cleans up only after a successful create or an ambiguous transport outcome; a definitive create precondition rejection leaves the pre-existing object untouched. Public current-state evidence no longer carries raw execution identifiers, while retained private evidence paths preserve the full records. Recovery wording now names the implemented terminal owner proof, one bounded settling window, and repeated child sweeps. P6 remains incomplete because the broader security review has not been performed.

All 363 tests passed, including the real attached-Docker Harbor fixture. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, isolated-wheel CLI/version/metadata/content checks, and `git diff --check` passed. No detect-secrets or gitleaks executable was available, so no dependency was installed solely for scanning. A bounded standard-library scanner read all 27 changed and untracked files (about 1.05 MB) and checked common provider/token/private-key/credential-URL patterns, generic secret assignments, and high-entropy literals. Its sole initial match was the deliberate synthetic provider-secret fixture used to prove exception redaction; after auditing that exact value, the allowlisted scan reported zero findings. The scan did not inspect Git history, ignored files, binary content, or provider-side secret stores.

## 2026-08-29T05:20:51-07:00: Production attached local execution CLI

Provenance: user contract, local Python 3.12 implementation, temporary catalog built from the existing source-only Harbor fixture, real Docker daemon runs, isolated wheel installation, and a real subprocess SIGINT.

`tetrabench run SECTION --profile PROFILE --output DIR [--json]` now accepts only an effective local-controller/Docker profile before mutation, validates selected task directories beneath the project root, exclusively creates a previously absent output path, and calls the production `HarborRunner` without constructing S3, Modal, admission, receipt, or other cloud clients. Harbor's native directory remains under the output path. Results expose native outcome, standard mean reward as a decimal string, and path; failed or cancelled outcomes exit 1. SIGINT after native job creation exits 130 with canonical interrupted evidence and retains the partial job without claiming terminal success.

All 370 tests passed, including the pre-existing real Docker composition and the new temporary-catalog CLI Docker run. Ruff check/format, ty, lock check, wheel/sdist build, and diff whitespace checks passed. An isolated installed wheel completed the temporary catalog with reward `1.0`; a separate installed-CLI process was interrupted after native job creation and retained its reported evidence path. The checked-in benchmark catalogs remain empty.

## 2026-08-29T05:35:39-07:00: Local validation and privacy correction

Provenance: user-provided review findings, Harbor 0.22's exported `Task` model, local Python 3.12 regressions, real Docker runs, and isolated installed-wheel execution.

Every selected local task now passes through Harbor's non-mutating public `Task` constructor before tetrabench creates the requested output. Invalid fixture structure or native task configuration therefore leaves no destination. The output reservation is exclusive and is chmoded to exact `0700` after creation, so the result is owner-only under both permissive and fully restrictive process umasks. A failure before Harbor creates `harbor-job` removes the reservation; failures and interrupts after native job creation retain evidence.

All 374 tests passed, including both real Docker suite paths. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, and `git diff --check` passed. An isolated installed wheel completed the real fixture with reward `1.0` and exact `0700` output. A second installed-CLI process received SIGINT after native config creation, exited 130, emitted canonical interruption evidence, retained that path, and kept the destination at `0700`.

## 2026-08-29T05:41:30-07:00: Local reservation cleanup ownership correction

Provenance: user-provided cleanup-race contract and local implementation review.

Pre-job local execution cleanup may remove only a truly empty reserved output directory with a non-recursive directory-removal primitive. If concurrent or injected content appears, or cleanup otherwise races or fails, tetrabench retains the path and preserves the original execution exception. This correction is D-056; E-050 tracks final validation and installed-wheel Docker/SIGINT evidence.

## 2026-08-29T05:46:55-07:00: E-050 validation

Provenance: full local Python 3.12 validation, fresh isolated-wheel installation, real Docker execution, and direct subprocess SIGINT.

All 375 tests passed, including both real Docker suite paths and regressions proving that injected pre-job content is retained with the original exception while a truly empty reservation is removed. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, and `git diff --check` passed. A fresh installed wheel completed the fixture with reward `1.0` and exact `0700` output. A separate installed-wheel process received SIGINT after native Harbor config creation, exited 130, emitted canonical interruption evidence, and retained the native directory at exact `0700`.

## 2026-08-29T05:53:43-07:00: Permanent local reservation evidence correction

Provenance: user-provided lifecycle contract, local Python 3.12 regressions, full validation, fresh isolated-wheel installation, real Docker execution, and direct subprocess SIGINT.

This entry corrects the cleanup behavior recorded at 05:35 and 05:41 without altering those historical entries. Harbor-native task validation still completes before output creation and leaves no destination on failure. Once tetrabench creates the output reservation, it never deletes it. After exact `0700` is established, empty and populated failures remain private owned evidence; replacement races retain both the moved private reservation and replacement content without deletion. D-057 supersedes D-055 and D-056 where more specific.

All 376 tests passed, including both real Docker suite paths and regressions for pre-validation absence plus empty, content, and replacement-race retention. Ruff check and format, ty, `uv lock --check`, wheel/sdist build, and `git diff --check` passed. A fresh installed wheel completed the fixture with reward `1.0` and exact `0700` output. A separate installed-wheel process received SIGINT after native config creation, exited 130 with canonical interruption evidence, and retained the native directory at exact `0700`.

## 2026-08-29T06:19:32-07:00: Remote result and artifact user loop

Provenance: user contract, local AWS/Tigris-parameterized provider fakes, POSIX filesystem race regressions, full Python 3.12 validation, real attached Docker regressions, and a fresh isolated-wheel installation.

Remote result and run listing now use the configured S3 namespace without receipts, Modal inspection, an index, or a database. Result interpretation starts with `S3Store.read_run_state`, validates terminal/admission/request/plan/storage binding, parses the bound native Harbor result for the standard reward, and keeps stable unknown, nonterminal, terminal, and conflict output plus exits. Remote listing paginates the complete configured `runs/` namespace, accepts only exact admission/request/event/terminal layouts, deduplicates valid run IDs, and surfaces every malformed key in sorted order.

Artifact pull deliberately admits only successful bound terminals. It validates the complete logical inventory before creating output, then roots all writes in opened no-follow directory descriptors, verifies parent/destination/nested identities, uses exclusive no-follow file creation, enforces directory `0700` and file `0600`, and rejects traversal, duplicates, prefix conflicts, corruption, replacements, symlinks, and injected content. A created destination is never deleted. Failed and cancelled inventories remain visible through `result` rather than materializable. Read and pull paths use no provider delete operation. Cancellation now confirms before constructing provider services; JSON cancellation requires `--yes`.

E-052 passed with 415 tests, including both real Docker paths, plus Ruff check/format, ty, `uv lock --check`, wheel/sdist build, `git diff --check`, and fresh installed-wheel root/result/runs/artifacts/cancel help and import/version checks. The suite emitted only the existing Harbor checksum deprecation and pytest cleanup warnings from deliberately retained replacement-race evidence.

## 2026-08-29T06:37:02-07:00: Remote authority and extraction correction

Provenance: user-provided review findings, local Python 3.12 provider/filesystem regressions, full validation, real attached Docker execution, and a fresh isolated-wheel installation.

Terminal request, plan, run, and configured-storage validation no longer depends on an admission record. Admission validation composes with the same authoritative validator and adds its expected plan digest. Result, artifact pull, status, and terminal recovery retain conflict reporting through that shared path; absent-admission wrong-storage terminals now fail closed.

Artifact pull shares the controller's default 10,000-file, 64-MiB-per-file, and 1-GiB-total policy and preflights every limit before destination reservation. S3 bodies stream directly into exclusive destination descriptors while hashing and counting, with no pull-side `read_content` buffering. Verified files and every created directory entry are fsynced in durability order. Corrupt partial private evidence is retained and receives best-effort fsync without masking its original exception. Paginated listing rejects malformed truncation state and missing, non-string, or repeated continuation tokens.

E-053 passed with 433 tests, including both real Docker paths. Ruff check/format, ty, `uv lock --check`, wheel/sdist build, and `git diff --check` passed. A fresh isolated wheel passed root/result/runs/artifact CLI help, completed the real Docker fixture with reward `1.0`, and retained exact-`0700` native evidence after installed-CLI SIGINT exit 130. The full suite emitted only the existing Harbor checksum deprecation and deliberate retained-evidence cleanup warnings.

## 2026-08-29T06:47:46-07:00: Response-body and artifact-fd cleanup correction

Provenance: user-provided P2 findings, local Python 3.12 close-spy and fsync-failure regressions, full validation, real attached-Docker tests, and a fresh isolated-wheel installation.

S3 GET response validation, direct streaming, and final digest/length checks now share one body cleanup guard. Rejected metadata closes an available body, and a simultaneous close failure cannot replace the original validation or streaming exception. Artifact writing enters its failure-state guard immediately after exclusive file creation, before parent fsync. Parent-fsync and later failures attempt file and parent durability plus descriptor close without masking the first exception; successful materialization still requires successful close. D-060 records the lifecycle contract.

E-054 passed with 438 tests, including both real Docker paths. Ruff check/format, ty, `uv lock --check`, wheel/sdist build, and `git diff --check` passed. A fresh isolated wheel passed version, root/result/runs/artifact help, import, and distribution-metadata smoke checks. The suite emitted only the existing Harbor checksum deprecation and deliberate retained-evidence cleanup warnings. No provider call occurred.

## 2026-08-29T06:56:31-07:00: Artifact directory descriptor cleanup correction

Provenance: user-provided lifecycle finding, local Python 3.12 fault-injection regressions, full validation, real attached-Docker tests, and a fresh isolated-wheel installation.

Artifact pull now registers every successfully opened parent-anchor, destination-root, and nested-directory descriptor with one cleanup owner before fchmod, fsync, fstat, or type/identity validation. Failure unwind best-effort fsyncs all acquired artifact directories and their parent, then closes each descriptor once. Durability and close failures remain suppressed only while another exception is active, so they cannot replace the setup failure.

E-055 passed with 446 tests, including both real Docker paths. The root/nested regression matrix injects fchmod, fsync, fstat, and type-validation failures while close itself also fails after releasing the descriptor; every case retains the first exception, records one close, and proves the descriptor is closed. Ruff check/format, ty, `uv lock --check`, wheel/sdist build, `git diff --check`, and a fresh isolated-wheel version/root/result/runs/artifact-help plus import/metadata smoke passed. No provider call occurred.

## 2026-08-29T06:58:07-07:00: Directory-close wording correction

Provenance: immediate local review of the preceding append-only entry against the implemented cleanup path.

The preceding statement that close failures are suppressed only while another exception is active is too narrow. Artifact directory closure is unconditionally best effort on both success and failure. The proven failure-path claim is that a close error cannot replace the original setup exception.

## 2026-08-29T07:05:22-07:00: Artifact final-mode trust-boundary correction

Provenance: user-provided final mode finding, local Python 3.12 mode-race regressions, full validation, and fresh isolated-wheel inspection.

This entry corrects earlier artifact-pull wording where private-tree and fail-closed claims could imply isolation from a malicious process running as the same UID. Tetrabench's enforceable boundary is descriptor-rooted exclusive no-follow creation and pathname/symlink overwrite defense. Artifact finalization now reapplies exact `0600` to each file and exact `0700` to each destination directory, then checks retained type, identity, and exact mode through `fstat` before final fsync/close. Failure unwind attempts the same restoration for safely retained partial evidence without replacing its original error. A same-UID process remains authoritative and may mutate the tree during or after pull; post-return immutability is not promised. D-062 records the correction.

E-056 passed with 449 tests, including both real Docker paths and regressions for mid-pull root/directory/file widening, corrupt partial-evidence restoration, and post-`fchmod` mode-change rejection. Ruff check/format, ty, `uv lock --check`, wheel/sdist build, `git diff --check`, and a fresh isolated-wheel version/artifact-help plus import/metadata smoke passed. No provider call occurred.

## 2026-08-29T07:19:33-07:00: GitHub Actions CI baseline

Provenance: user contract; official GitHub release refs for checkout v7.0.1, setup-python v7.0.0, and setup-uv v10.0.1; the official Gitleaks v8.30.1 GHCR manifest; local Python 3.12, Docker, actionlint, pip-audit, packaging, and Gitleaks validation.

One least-privilege workflow now covers pushes to `master` and pull requests. It cancels superseded branch runs, uses only Python 3.12, installs from `uv.lock`, requires the two explicitly marked real-Docker tests before the complete suite, builds wheel and sdist, installs the wheel over a hash-checked locked runtime export in an isolated environment, and checks its entrypoint, metadata, and package contents. The runtime dependency export is audited without dependency resolution. A separate job fetches full history and runs the official Gitleaks image by immutable digest with redacted output. Third-party actions are pinned to release-resolved commit SHAs. No repository or cloud secret, write permission, or artifact upload is used.

E-057 passed locally with 449 tests and two separately required Docker tests. Ruff check/format, ty, lock and locked-sync checks, wheel/sdist build, isolated wheel install and entrypoint/metadata/content checks, actionlint, and diff checks passed. pip-audit found no known vulnerability in the locked runtime export. Gitleaks v8.30.1 scanned all 16 commits and found no leak after an audited ignore for one historical documentation phrase misclassified as a generic API key; findings remained redacted. The first hosted GitHub Actions run is pending because these changes are intentionally uncommitted.

## 2026-08-29T07:22:12-07:00: CI baseline wording correction

Provenance: immediate editorial review of the preceding append-only entry.

The CI job installs from `uv.lock`, requires both Docker-marked tests before the complete suite, builds both distributions, and installs the wheel over a hash-checked runtime export. It then checks the installed entrypoint, metadata, and package contents. pip-audit examines that same locked runtime export without resolving dependencies. Release-resolved commit SHAs pin the actions, while the Gitleaks image uses the official v8.30.1 manifest digest. The workflow receives read-only repository permission and no repository or cloud secret. The preceding entry's final semicolon means the same Gitleaks finding output remained redacted; it does not establish a separate causal qualification.

## 2026-08-29T07:32:25-07:00: CI dependency and wheel-metadata review correction

Provenance: user-provided CI review findings and full local workflow parity on Python 3.12.

The dependency gate now generates one deterministic hash-bearing requirements document with `uv export --locked --all-groups --no-emit-project` and passes it to pip-audit without resolution. This covers runtime, development, build tooling, and their selected transitive dependencies without a duplicate runtime-only audit. Two independent exports were byte-identical; the 1,019-line document had SHA-256 `c8d0a1cf6053e3c5c362e1f5f4c7e09d34872a970e3be23845c1aec647c731fe`, and pip-audit found no known vulnerability.

The isolated installed-wheel smoke reads `Requires-Python` from distribution metadata and compares `packaging.specifiers.SpecifierSet` values against `>=3.12,<3.13`, so equivalent ordering is accepted while changed semantics fail. All 449 tests passed, including both separately required Docker tests. Lock/sync, Ruff check/format, ty, Docker daemon, wheel/sdist build, isolated install and metadata/content checks, actionlint, `git diff --check`, and Gitleaks over all 16 commits also passed. The existing Harbor deprecation and deliberate retained-evidence pytest cleanup warnings remain unchanged. The first hosted run remains pending because the workflow is uncommitted.

## 2026-08-29T07:55:19-07:00: E-058 credential-free security baseline

Provenance: local Python 3.12 source/API review, provider fakes, real attached Docker execution, a fresh isolated-wheel Docker composition, locked security tools, and retained E-046 evidence. No live provider API or credential was used.

Production and source-only tool code contains no `assert`; runtime invariants now raise explicit domain or integrity errors. Bandit 1.9.4 is locked in the development group and scans `src` and `tools` in CI with no skip. All durable key families are tested beneath the configured prefix, unsafe run IDs/digests/paths fail before provider calls, and the normal S3 fake rejects any `DeleteObject` call. The controller and attached local paths hide the complete configured AWS/Tigris credential and profile-selector set while Harbor validates and runs. The real Docker fixture observed only explicit unavailable defaults, and controller payload/failure tests found neither selector material nor raw SDK credential messages in durable evidence.

The README now contains the bounded submitter/controller/Harbor-child action matrix and Tigris's bucket-wide `ListBucket` limitation. E-021 and E-023 pass for static/local evidence only. Current IAM denials, bucket privacy, and effective encryption remain U-011 `unproven`; the README names the exact future role, cross-prefix, delete, bucket-policy/public-access, encryption, and probe-object checks.

Final validation passed with 460 tests, including both real Docker regressions, plus Ruff check/format, ty, `uv lock --check`, wheel/sdist build, a clean Bandit source/tools scan, pip-audit of every locked group, actionlint, `git diff --check`, and Gitleaks over all 17 commits. A fresh isolated wheel passed `pip check`, version and content inspection, and real Docker composition with a succeeded terminal. An additional worktree directory scan reported only the already-audited historical `S3/receipt/Modal` prose false positive; it found no new credential material. The existing Harbor deprecation and deliberate retained-evidence pytest cleanup warnings remain unchanged.

## 2026-08-29T08:15:14-07:00: CLI and Harbor isolation security correction

Provenance: user-provided security review findings, local Python 3.12 regressions, pinned Botocore/Harbor/Modal source inspection, real attached Docker execution, and full local CI/security parity. No live provider API or credential was used.

Every remote command now routes `ClientError` through one public failure renderer that emits only fixed type `provider_error`, fixed message `provider request failed`, and exit 2. Parameterized human and canonical-JSON regressions cover submit, recover, status, cancel, result, artifact pull, and remote runs with credential-like provider code, message, and operation fields. Doctor uses the same sanitization. Local typed integrity and configuration messages remain available, and the existing controller failure-record regression still excludes raw provider material.

Harbor execution now removes process-environment names by case-insensitive namespace rather than a closed credential list: every `AWS_` and `TIGRIS_` member, including `AWS_ACCOUNT_ID`, plus reviewed `BOTO_CONFIG` and `BOTOCORE_TCP_KEEPALIVE`. Independent adversarial tests seed lowercase, mixed-case, and alternate names without importing the production matcher. The boundary removes matching names introduced while active and then restores the exact saved environment. Local controller composition, Modal sandbox environment-secret payload construction, and both real Docker paths passed; fixture defaults reached the child as `unavailable`.

E-059 passed with 476 tests and both separately required Docker tests. Ruff check/format, ty, `uv lock --check`, wheel/sdist build, isolated locked-runtime wheel install, Bandit, all-groups pip-audit, actionlint, `git diff --check`, and redacted full-history Gitleaks passed. A worktree directory scan reported only the already-audited E-031 documentation phrase. No live provider call occurred.

## 2026-08-29T08:28:28-07:00: Provider exception-family correction

Provenance: user-provided remaining-leak finding, pinned Botocore and Modal exception inheritance, independent command-level fakes, and full local CI/security parity. No provider API or credential was used.

The 08:15 entry's statement that every remote command was covered is corrected: its fixed renderer classified only `ClientError`, while the same command handlers also caught `BotoCoreError` subclasses and `modal.exception.Error`. Those base-family exceptions could therefore expose provider-controlled credential retrieval, transport, authentication, or chained text through the generic local-error path.

The common renderer now classifies `ClientError`, every `BotoCoreError` subclass, and `modal.exception.Error` as fixed `provider_error` / `provider request failed` with exit 2. Independent human and JSON parameterization injects adversarial ClientError fields, `CredentialRetrievalError` arguments, Modal error arguments, and a chained cause across online doctor, controller deploy, submit, recover, status, cancel, result, artifact pull, and remote runs wherever each exception family is caught. Exact output assertions exclude raw messages, arguments, subtype and chaining fields. Separate regressions retain precise local configuration and integrity errors.

E-060 passed with 505 tests and both separately required Docker tests. Ruff check/format, ty, `uv lock --check`, locked sync, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, Bandit, all-groups pip-audit, actionlint, `git diff --check`, and redacted full-history Gitleaks all passed. No live provider call occurred.

## 2026-08-29T08:46:12-07:00: Tetrabench v1 benchmark task design

Provenance: user-provided task contract on 2026-08-29; primary project and paper pages for SWE-bench, Terminal-Bench 2.0, DDBench, InfraBench, CI-Repair-Bench, SWE-Review, GitGoodBench, BulkPR-Bench, and UnderSpecBench; local inspection of the Harbor 0.22 fixture and tetrabench catalog/context contracts. The user contract is authoritative for tetrabench scope. The cited benchmarks establish adjacent evaluation coverage, not fixture admission evidence.

The accepted v1 design contains five systems-design-through-implementation tasks (`authority-fencing`, `atomic-outbox`, `lifecycle-reconciliation`, `online-migration`, and `tenant-authorization`) and five single-pull-request workflow tasks (`pr-submit`, `ci-repair`, `review-adjudication`, `release-backport`, and `merge-queue-recovery`). Every task has objective executable mandatory gates and a strict binary primary reward. Additional native Harbor reward diagnostics cannot soften that reward. The GitHub lane uses real local Git plus a bounded task-local forge state machine and makes no live GitHub API claim.

The design records CPU-only pinned execution, standard Harbor fixture and verifier boundaries, newly authored compact fixtures, three reused Git snapshots, no verifier network dependency, public-test contamination limits, shared-root exploit risk, anti-shortcut admission, exact task order, and v1 exclusions. No task fixture was added, and both catalogs remain empty.

Audit found one prerequisite: detached submission must derive, seal, and request-bind the complete selected task fixture before the first fixture is implemented or cataloged. Existing explicit context paths can omit fixture files and therefore do not prove that detached execution uses the intended immutable task. D-066 records this blocker and points to the canonical design.

Primary sources:

- SWE-bench: https://github.com/swe-bench/SWE-bench
- Terminal-Bench 2.0: https://www.tbench.ai/benchmarks/terminal-bench-2
- DDBench: https://arxiv.org/abs/2608.14863
- InfraBench: https://arxiv.org/abs/2608.11234
- CI-Repair-Bench: https://arxiv.org/abs/2604.27148
- SWE-Review: https://arxiv.org/abs/2607.06065
- GitGoodBench: https://aclanthology.org/2025.realm-1.19/
- BulkPR-Bench: https://arxiv.org/abs/2608.02685
- UnderSpecBench: https://arxiv.org/abs/2607.02294

## 2026-08-29T08:50:34-07:00: V1 design validation

Provenance: local Python 3.12 validation of the uncommitted design and catalog-description change. No task fixture, provider call, detached run, or catalog task was created.

The strict catalog model accepts and exposes both new section descriptions, while both task lists remain empty. Offline `tetrabench doctor`, TOML parsing and empty-task assertions, and design completeness assertions passed. All 505 tests passed, including the two existing real-Docker Harbor tests. Ruff check and format, ty, `uv lock --check`, wheel and source-distribution build to a temporary output directory, and `git diff --check` passed. The test run retained the existing Harbor checksum deprecation and deliberate replacement-race cleanup warnings; no new failure occurred.

## 2026-08-29T09:06:59-07:00: V1 task-family and admission correction

Provenance: user-provided evaluator, manifest, scoring, network, artifact, admission, citation, and prose corrections on 2026-08-29; pinned Harbor 0.22.0 package inspection; Harbor task, artifact-collection, and network-policy documentation; and the cited benchmark papers. This entry corrects the 08:46 task-design entry without changing its ten task families or empty catalogs.

`benchmarks/README.md` is a task-family and admission contract until fixtures own local manifests. Every v1 task requires Harbor 0.22 separate-verifier mode. Agent output is a declared bounded artifact set beneath `/artifacts`; Harbor collects after the agent phase, stops the main service, and launches a clean no-network verifier whose image is built from `tests/` with hidden code baked in. Systems verifiers rebuild fresh state and rerun fixed hidden schedules. GitHub tasks use real local Git plus a minimal API/CLI-only forge sidecar; clean verification validates collected event schema and Git objects, reconstructs transitions from the immutable initial snapshot, and reruns behavior from clean clones. Mutable agent-controlled logs and results are never authority.

Each future fixture must expose `contract.toml` and keep `tests/cases.toml` in the verifier image. The manifests bind initial state, interfaces, logical time, named cases, schedules, checkpoints, gates, seeds, and expected state/effect hashes. Primary reward is exactly finite integer `0` or `1`, with `1` requiring every mandatory gate. Native task rewards are authoritative and section score is their binary pass rate. Tetrabench validation and summarization remain unimplemented prerequisites.

P7 now blocks fixtures on local and detached selected-fixture sealing, separate-verifier handoff, forge-sidecar collection, both phase network policies, deterministic manifests, reward validation, and bounded admission. Per-task admission uses at most 16 hidden cases and 8 schedules; fixed gold/no-op/mutant/exploit/calibration runs; a `$25` model-spend cap; an 8-hour wall-clock cap; 35-minute agent attempts; a 2-minute normal hidden suite; a 4-minute verifier hard timeout; lower submission/verifier artifact budgets beneath the existing collector hard limits. Overproduction fails closed, but these declared-artifact limits do not claim control of arbitrary undeclared agent output.

Citation wording was checked against the primary papers: SWE-bench applies generated patches and runs fail-to-pass/pass-to-pass tests; InfraBench combines four lifecycle gates, executable preservation checks, and a post-hoc LLM risk review; CI-Repair-Bench re-executes a validation-preserving standardized workflow; SWE-Review starts from AI-generated candidate PRs and measures post-review revision outcomes; GitGoodBench covers merge-conflict resolution, interactive rebase, and iterative committing; UnderSpecBench varies intended action, target certainty, and blast radius rather than missing authority. The related-work conclusion is limited to task designs not represented by the cited set.

Primary sources added or rechecked:

- Harbor task and separate-verifier semantics: https://www.harborframework.com/docs/tasks#verifier-environment-shared-vs-separate
- Harbor artifact lifecycle: https://www.harborframework.com/docs/run-jobs/results-and-artifacts#how-collection-works
- Harbor network policy: https://www.harborframework.com/docs/tasks/network-policy
- SWE-bench paper: https://arxiv.org/abs/2310.06770
- InfraBench: https://arxiv.org/abs/2608.11234
- CI-Repair-Bench: https://arxiv.org/abs/2604.27148
- SWE-Review: https://arxiv.org/abs/2607.06065
- GitGoodBench: https://aclanthology.org/2025.realm-1.19/
- UnderSpecBench: https://arxiv.org/abs/2607.02294

## 2026-08-29T09:13:44-07:00: Corrected v1 contract validation

Provenance: local Python 3.12, Docker, package, security, link, and pinned Vale validation after the task-family and admission correction. No fixture, provider call, detached run, or catalog task was created.

All 505 tests passed, including the two separately required real-Docker Harbor tests. Ruff check and format, ty, `uv lock --check`, locked dependency sync, Bandit, all-groups pip-audit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, offline `tetrabench doctor`, `git diff --check`, and redacted full-history Gitleaks passed. The full suite retained the existing Harbor checksum deprecation and deliberate retained-evidence cleanup warnings.

A bounded link check resolved all 13 links in `benchmarks/README.md`, including local anchors and external sources. The repo-pinned Vale AI-tells rules ran on that document. Concrete findings improved sentence-case headings, negation, semicolon use, lifecycle actors, and sentence linkage. Remaining errors are documented false positives for required domain terms (`implementation`, `implement`, `blast radius`, and `named` IDs), an exact paper title, mandatory gate quantifiers, and genuine three-action contract sets; passive-voice and sentence-uniformity warnings mostly occur in the enumerated gate specification.
