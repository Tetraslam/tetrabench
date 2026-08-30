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

## 2026-08-29T09:39:48-07:00: Automatic complete selected-fixture sealing

Provenance: user-provided prerequisite contract; local Python 3.12 implementation; adversarial POSIX filesystem tests; both real attached-Docker tests; wheel/source-distribution inspection; and local CI/security parity. No task fixture, catalog task, provider call, or live resource was added or changed.

Detached `submit` now resolves selected catalog entries once, anchors each `harbor_task` path beneath a no-follow-opened project root, and seals every regular file under every selected task directory. The context manifest preserves project-relative destinations and binds normalized executable mode, size, digest, and immutable bytes. Explicit file context composes only at disjoint portable paths. Complete-tree verification reopens selected roots through the retained project descriptor and compares file and directory identity after all reads. Any missing or non-directory root, escape, symlink, special file, duplicate or prefix collision, case/NFC ambiguity, limit violation, file replacement, add/remove/rename, directory replacement, or mode change fails before S3 or Modal construction. The controller continues to materialize only manifest bytes and Harbor resolves task paths only beneath that materialized root; local `run` remains checkout-attached.

E-061 passed with 536 tests. The fixture-specific suite proves deterministic ordering, byte/path/mode/add/remove identity changes, multi-task union, explicit-context composition and collisions, every named mutation race, provider non-construction, stable local/materialized tree equality, and detached plan compilation after the checkout task tree is deleted. Both separately required Docker tests passed. Ruff check/format, ty, `uv lock --check`, locked sync, Bandit, all-groups pip-audit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and content smoke, `git diff --check`, and redacted Gitleaks over all 19 commits passed. Wheels retain neither catalogs nor source-only fixtures; source distributions retain both. A live Modal catalog submission remains `unproven`, as do the separate-verifier, network-policy, artifact-handoff, and forge-sidecar prerequisites. Both checked-in task lists remain empty.

## 2026-08-29T10:08:14-07:00: Fixture-sealing trust-boundary correction

Provenance: user-provided review findings; Linux `/proc/self/fdinfo` behavior; local adversarial filesystem and direct-record tests; both real attached-Docker tests; and complete local CI, package, dependency, and history-scan parity. No task, catalog entry, provider call, or live resource was added or changed.

D-071 corrects E-061's stable-snapshot wording. Detached preparation now anchors the project root before reading project configuration or catalog bytes, retains that root authority through fixture sealing, enumerates incrementally through descriptor-based `scandir`, and sorts only after bounded collection. Configuration defaults to at most 10,000 discovered entries, 10,000 directories including selected roots, and depth 64; the entry bound cannot be lower than the file bound. Every opened fixture descriptor must match the anchored device and Linux mount ID, every regular file must have one link, and missing mount evidence is unsupported.

The second complete traversal compares names, types, device/inode, mode, mount ID, size, and fresh file digests against the immutable staged bytes. Digest equality owns byte proof; inode reuse is not claimed impossible. This is mutation detection across two observations in a trusted same-UID checkout, not an atomic filesystem snapshot. Malicious same-UID transient change-and-restore behavior remains outside the boundary.

One model-level validator now owns exact duplicates, file/directory prefix conflicts, NFC normalization, Unicode casefold, and portable relative-path rules for both resolved plans and context manifests. The controller reparses the complete request, plan, and manifest before admission and again before materialization. The exact malformed direct-record pair `a` and `a/b` produces no admission transition, attempt event, Volume operation, or output tree.

E-062 passed with 551 tests. The suite includes hardlink, fake cross-device, mount-ID mismatch/unavailable, descriptor cleanup, root replacement after catalog parse, second-enumeration add/remove/rename/replace/mode, coarse-stat digest change, discovery bounds, portable collision, and zero-attempt-mutation regressions. Both required Docker tests, Ruff, format, ty, lock and locked sync, Bandit, all-groups pip-audit, actionlint, wheel/sdist build, isolated locked-runtime wheel smoke, diff checks, and redacted Gitleaks over all 19 commits passed. A live Modal catalog run remains `unproven`.

## 2026-08-29T10:21:31-07:00: Prepared submit authority closure

Provenance: user-provided authority contract, local command-level mutation regression, source-only fixture launcher review, and full Python 3.12 CI/security parity. No provider API or live resource was used.

Detached preparation now retains the exact resolved Modal App, Function, and environment selector in its in-memory `PreparedSubmission`. The submit CLI constructs storage only from immutable resolved-plan storage and constructs Modal only from that prepared selector. The source-only fixture launcher follows the same boundary. Project configuration and catalog data are not reread after preparation, so replacement cannot combine a later endpoint or bucket revision with the prepared request. The launch selector contains resource names only and is absent from plans, requests, receipts, and durable evidence. D-072 records this authority.

E-063 passed with 552 tests. Its command-level regression replaces both project config and catalog after preparation, rejects any second config load, counts exactly one config and catalog load, and observes the original Tigris bucket plus original Modal App, Function, and environment at spawn. Both required Docker tests, lock and locked sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over all 19 commits passed.

## 2026-08-29T10:35:31-07:00: Clean-verifier prototype contract

Provenance: user-provided implementation contract; pinned Harbor 0.22.0 task models and lifecycle source; Harbor documentation; and local Docker image-manifest inspection. Implementation evidence is pending.

E-064 will add one source-only, non-catalog fixture distinct from the existing simple oracle fixture. Its main service owns a real local Git worktree and reaches a task-local forge only through a documented standard-library API/CLI. The forge owns immutable initial state, schema-checked transitions, a hash-chained event log, one atomic SQLite seal transition, and a canonical export. No forge filesystem is shared with main.

Pinned Harbor 0.22 native lifecycle is the authority handoff: declared main artifacts collect first; separate-verifier mode attempts to stop main; sidecar `[[verifier.collect]]` seals before exporting; declared sidecar artifacts collect separately; and only then does Harbor build and run the clean verifier from `tests/`. Harbor treats collect-hook and main-stop failures as warnings, so the verifier must reject missing or invalid exports and the sealed forge must reject any write after collection begins. The verifier runs with `network_mode = "no-network"`, receives only declared artifacts at their source paths, validates Git and forge state independently, and writes the only reward as exact integer `0` or `1` plus diagnostics.

The shared container base is `python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7`. Runtime uses only Python's standard library and Git already installed in the built images; the Git package version will be pinned after the first image build establishes the current Bookworm package candidate. Detached Modal proof remains `unproven` because the retained storage credentials are expired.

## 2026-08-29T11:10:28-07:00: E-064 local clean-verifier evidence

Provenance: pinned Harbor 0.22.0 models and lifecycle, local Docker, adversarial fixture tests, native job inspection, package inspection, and complete local CI/security parity. No catalog task, provider call, commit, or live resource was created or changed.

E-064 passed locally. The source-only `harbor_authority_task` fixture uses Harbor's normal compose task layout with separate `main` and `forge` filesystems. Main contains a deterministic real Git repository and only the documented forge HTTP/CLI surface. The standard-library forge validates one explicit PR-like transition against immutable current state and a 40-character commit OID, appends a SHA-256 event chain in one SQLite transaction, and seals before writing an atomically replaced canonical export. Its HTTP endpoint returns conflict for every write after seal, including the tested leftover-main request.

Harbor collected the main Git artifact, stopped `main`, ran the forge `[[verifier.collect]]` seal/export hook, collected the sidecar artifact, and created a separate verifier from the `tests/` build context. Native task policy recorded public agent network and no-network verifier execution. The Oracle proved `/tests` and the baked verifier marker absent before making one focused commit and submitting the transition. The clean verifier validated its hidden artifact contract, expected paths, sidecar file hashes and event chain, every event OID, immutable base and focused ancestry, Git fsck, exact refs and local config, a no-hardlink clean clone, product output, artifact limits, and forbidden files. It wrote exact `1\n` reward bytes plus canonical diagnostics. Native Harbor job/trial config, lock, result, separate-verifier mode, and artifact manifest all parsed successfully.

Adversarial tests reject an agent-owned event file, invalid OID/current state, an HTTP write after seal, a modified snapshot with a recomputed manifest hash, either missing declared artifact, forbidden external Git object sources, and a same-environment verifier without the baked marker. Agent logs and claimed result files never determine reward.

All 561 tests passed on Python 3.12. The three Docker-marked tests passed separately. Ruff check/format, ty, `uv lock --check`, locked all-group sync, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel install and metadata/content smoke, all-groups pip-audit, `git diff --check`, redacted Gitleaks over all 20 commits, and redacted changed/untracked scans passed. Wheels exclude both source-only fixtures; source distributions retain the complete new fixture. Detached Modal E-064 and automatic catalog sealing remain `unproven` because the retained storage credentials are expired.

## 2026-08-29T11:14:00-07:00: E-064 build-source pin correction

Provenance: live Debian snapshot package-index check and a repeated real Docker E-064 run. This corrects the pending Git-package statement in the 10:35 contract entry.

The main and clean-verifier builds now replace mutable Debian mirrors with `snapshot.debian.org/archive/debian/20260829T000000Z`, disable stale `Valid-Until` rejection for that immutable snapshot, and install exact `git=1:2.39.5-0+deb12u3`. The forge image needs no package installation. The repeated real Docker run passed with reward `1` after both Dockerfiles used the snapshot.

## 2026-08-29T12:00:00-07:00: E-064 authority and verifier correction

Provenance: user-provided review findings, pinned Harbor 0.22 models and artifact semantics, local adversarial tests, and repeated local Docker execution. This entry corrects the 10:35 and 11:10 E-064 entries without altering them. Final parity evidence is pending.

The sidecar collect hook no longer creates forge terminal authority. The agent's final submitted API transition validates complete state, appends the final hash-chained event under a unique replay key, revokes the run capability, and records terminal sealed state in one `BEGIN IMMEDIATE` transaction before agent exit. Collection only publishes an already-terminal export and fails if finalization never happened. Canonical file publication follows with fail-closed temp writes, file and directory `fsync`, rename, and parent `fsync`; it is ordered after the atomic database transition but is not jointly atomic with it.

The clean verifier now parses submitted Git metadata and local config before any Git command, rejects repository-controlled hooks, fsmonitor, aliases, filters, object indirection, replacement refs, shallow/graft state, submodules, includes, unknown config, links, and special files, and invokes only absolute `/usr/bin/git` under a cleared environment and explicit safe overrides. Product behavior runs from a runner-owned copy under a distinct no-new-privileges UID/GID with resource and timeout bounds; tests, forge state, artifacts, reward output, and runtime sockets remain inaccessible to that runner. The separate verifier records UID/GID, effective cgroup CPU/memory, mount/socket exposure, and DNS/direct-IP/hostname egress probes. Harbor 0.22 has no verifier PID-limit primitive, so no PID guarantee is claimed.

Production native-artifact validation now parses every trial manifest with Harbor 0.22 models, derives the effective convention, task, and trial entries using Harbor service/source/destination semantics, requires every declared entry to be `ok`, validates actual file/directory types and links, and rejects missing, extra, failed, skipped, empty, mutated, or colliding declarations before controller success.

## 2026-08-29T11:53:18-07:00: Notes timestamp correction

Provenance: local clock check immediately after the preceding entry was appended.

The preceding heading's `12:00:00-07:00` timestamp was recorded incorrectly. Its actual creation time was approximately `11:52:00-07:00`. File order preserves append order.

## 2026-08-29T11:59:52-07:00: E-065 local hardening evidence

Provenance: complete Python 3.12 suite, three separately required real-Docker tests, pinned Harbor 0.22 native artifacts, verifier runtime diagnostics, package inspection, and local CI/security parity. No catalog task, provider call, detached run, commit, or live resource was created or changed.

E-065 passed locally with 591 tests. All three Docker tests passed separately and again in the full suite. The clean verifier recorded root orchestration, product execution under UID/GID 65532 with no-new-privileges and rlimits, effective one-CPU and 384 MiB cgroup bounds, no Docker/containerd socket or undeclared task mount, and failed DNS, direct-IP TCP, and hostname TCP probes. Harbor 0.22 has no verifier PID-limit primitive, so no PID guarantee is claimed. Detached Modal remains `unproven`.

Adversarial coverage includes concurrent terminal transitions, capability revocation, unique replay keys, pre-finalization collection failure, post-terminal writes, exact HTTP method/path/content type/body/framing limits, duplicate/nonfinite/boolean/whitespace/schema/recomputed-hash JSON, direct Git structure/config inspection, non-executing hook/fsmonitor/alias/filter/alternate-object/replace-ref/shallow exploits, distinct-UID product behavior, native manifest source/destination/service/type/status/path/symlink/entry-set/collision mutations, and controller terminal-publication refusal after native provenance failure.

`uv lock --check`, locked all-group sync, Ruff check and format, ty, clean Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over all 20 commits passed. A full worktree Gitleaks scan initially reported the already documented E-031 prose false positive; replacing its slash-joined service names with normal prose removed that trigger without suppressing the rule.

## 2026-08-29T12:27:48-07:00: E-066 native-manifest provenance correction

Provenance: user-provided remaining P2 findings, pinned Harbor 0.22 `Task`, `ArtifactHandler`, artifact helpers, and manifest models, focused adversarial tests, full Python 3.12 validation, and repeated local Docker execution. No provider call, detached run, commit, or live resource was created or changed.

Native artifact validation now rejects a persisted trial whose task path is absent, a file, a symlink, or an invalid Harbor task directory. It loads the pinned task config and composes task-level artifacts before trial-level artifacts with Harbor's OS-specific convention entry. Fake native-manifest tests use a copied real task directory rather than a `/task` placeholder.

The native manifest parser now rejects invalid UTF-8, trailing data, duplicate object keys, nonfinite constants, invalid root and field types, and schema changes. Accepted bytes must exactly equal Harbor 0.22's `json.dumps(manifest.to_json_data(), indent=2)` output. This deliberately rejects whitespace, member-order, and trailing-byte changes without applying tetrabench's RFC 8785 record serializer to a Harbor-owned file.

All 607 tests passed, including all three Docker tests separately and again in the full suite. `uv lock --check`, locked all-group sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over all 20 commits passed.

## 2026-08-29T13:05:46-07:00: E-067 no-network DNS correction

Provenance: user-provided Harbor 0.22 Docker egress-control correction, the retained hosted GitHub Actions failure, focused network-probe regressions, repeated real local Docker execution, and complete local CI/security/package parity. This entry corrects the DNS requirement recorded in the 11:59 E-065 evidence without altering it.

Harbor's Docker `no-network` control may permit `getaddrinfo` through its control path while denying outbound traffic. The clean verifier now records DNS success or failure and the unique returned addresses as runtime evidence without requiring DNS failure. Direct-IP and hostname TCP probes still must fail; successful response-bearing connections fail reward closed. Tests cover DNS-resolves/TCP-denied, DNS-fails/TCP-denied, and successful direct-IP or hostname TCP probes.

All 607 tests passed, including all three Docker tests separately and again in the full suite. `uv lock --check`, locked all-group sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, and a redacted worktree Gitleaks scan passed. Hosted GitHub Actions validation remains pending.

## 2026-08-29T13:11:29-07:00: E-067 hosted validation

Provenance: GitHub Actions run [33272641566](https://github.com/Tetraslam/tetrabench/actions/runs/33272641566) for commit `e40b0cca4cab4c0aa3a45a97110a87a2e99a6491`.

The hosted Python 3.12 job passed the three required Docker tests, the full 607-test suite, lock and locked sync, Ruff, ty, Bandit, package build and isolated-wheel smoke, and the all-groups dependency audit. The independent full-history Gitleaks job passed. This closes the hosted clean-verifier false failure while retaining both TCP-denial gates.

## 2026-08-29T13:57:10-07:00: E-068 binary reward and summary prerequisite

Provenance: user-provided reward/summary contract, pinned Harbor 0.22 persisted config/result behavior, focused adversarial tests, repeated real local Docker execution, and complete local CI/security/package parity. No catalog task, provider call, detached run, commit, or live resource was created or changed.

Catalog tasks now carry numeric or binary reward policy into immutable resolved trials and plan/request identity. Retained plans without the field parse as numeric while preserving their original canonical bytes and digest. Native artifacts retain the persisted TrialConfig and strict raw reward dictionaries, allowing path-based mapping without trial-name interpretation and validation before controller success. All primary, diagnostic, and step-verifier values must be exact finite int/float values excluding booleans. Numeric attempts reject mixed missing/present primary rewards within one task. Binary attempts require primary integer `0` or `1`; the clean verifier now writes canonical `reward.json`, while reward.txt remains float-producing and invalid for binary policy.

Frozen trial, task, and section summaries use deterministic task/trial ordering, exact decimal strings, exact binary counts, and homogeneous policies. New controller-result schema v2 binds the summary to run, request, plan, attempt, and outcome. Cancellation retains it. Remote readers validate identity and summary arithmetic before success; only old schema-v1 results paired with old plans use the legacy native numeric fallback. Local and remote human output label binary aggregates as pass rate with counts, and JSON carries the complete summary.

All 633 tests passed, including all three Docker tests separately and again in the full suite. `uv lock --check`, locked all-group sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over all 25 commits passed. Detached Modal proof remains `unproven` because the retained storage credentials are expired.

## 2026-08-29T14:17:48-07:00: E-068 reward-summary model correction

Provenance: user-provided remaining P1 contract, direct Pydantic record construction, canonical controller-result byte tampering through the remote reader, and complete local Python 3.12 validation. No provider call, detached run, commit, or live resource was created or changed.

`SectionRewardSummary` now independently rejects every binary trial value except exact string `"0"` or `"1"`, including nested instances constructed without their own validators. Each binary task requires exactly its trial count, counts passes from exact `"1"` strings, and derives its canonical pass rate from those counts. Section sample count, pass count, and pass rate similarly derive from all trials. Self-consistent counts and rates that disagree with the trial samples fail closed. Decimal strings across numeric trial and aggregate fields remain finite and canonically normalized, rejecting negative zero, trailing decimal zero, leading-zero, and exponent forms.

All 682 tests passed, including all three Docker tests separately and again in the full suite. Direct-record and remote tamper matrices cover `2`, negative and decimal zero forms, decimal one, leading-zero forms, exponent forms, and coherent count/rate mismatches. `uv lock --check`, locked all-group sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over all 25 commits passed.

## 2026-08-29T15:24:21-07:00: E-069 authority-fencing local candidate

Provenance: user-provided task contract, newly authored standard-library Python and SQLite fixture, task-local fixed manifests, repeated local verifier execution, one final real Harbor 0.22 Docker oracle, the four-test Docker set, and complete local CI/security/package parity. No catalog entry, commit, push, provider call, or detached run occurred.

The unlisted `systems-design/authority-fencing` candidate now exposes `create`, `claim`, `renew`, `complete`, `fail`, and `inspect` through one documented CLI. SQLite `BEGIN IMMEDIATE` owns claim, renewal, and terminal transitions. Integer ticks define `now >= deadline` as expired. Committed reclaims increment the fence token, including same-worker reclaim; stale and expired actions preserve database bytes; matching terminal replay is byte-stable; conflicting terminal actions reject. Named precommit exits prove SQLite rollback and recovery.

The separate verifier admits only four regular submission files, rejects links, special files, extras, and budget overruns, copies only `authority.py` into fresh per-case scratch, and invokes it through fixed Python and `setpriv` as uid/gid 65532 with no new privileges, cleared capabilities, minimal environment, process groups, timeouts, and resource limits. It never imports submitted code. Eleven hidden cases and two fault schedules recompute SQLite state and effect hashes. Runtime diagnostics record uid/gid, cgroup, mounts, sockets, DNS, and active direct-IP and hostname probes. Exact integer reward `1` requires all six gates.

The final bounded local matrix passed all 16 entries: gold received `1`; the unchanged skeleton received `0`; each of the six seeded mutants received `0` with its intended gate equal to `0`; and hardcoded-public behavior, fake reward, hidden-test discovery, output forgery, symlink, special-file, extra-executable, and background-process fixtures each received `0`. The final real Harbor oracle used public agent networking and a no-network separate verifier, emitted canonical `{"reward":1}`, passed all six gate diagnostics, retained a succeeded native trial and collected workspace artifact, and produced a validated binary pass-rate summary of `1`.

Four Docker tests passed separately. The full Python 3.12 suite passed 686 tests. Lock and locked sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and exclusion checks, all-groups pip-audit, shell syntax, `git diff --check`, full-history Gitleaks, and worktree Gitleaks passed. Three consecutive local gold runs, two detached gold runs, detached no-op/mutant/exploit audits, calibration, complete bounded admission, and catalog admission remain `unproven`.

## 2026-08-29T16:24:07-07:00: E-069 authority-fencing review correction

Provenance: user-provided authority-fencing review findings, an independent verifier-side semantic model, regenerated model expectations, a strict 17-entry local admission matrix, all five real-Docker tests, one final real Harbor 0.22 oracle in the full suite, package inspection, and complete local CI/security parity. No catalog entry, commit, push, provider call, detached run, or calibration occurred.

This entry corrects the earlier E-069 evidence without altering it. The CLI contract now fixes exact canonical success and rejection schemas, exits `0`, `2`, and `86`, stream placement, terminal replay with `idempotent: true`, and byte-stable rejected actions. `contract.toml` is the sole inclusive tick domain. Fifteen hidden cases cover exact expiry, zero and maximum ticks, shorter renewal, zero/negative TTL, overflow, concurrency, restart, rollback, stale actions, and terminal replay. Each seed deterministically derives nontrivial identities, valid times, TTLs, contenders, and action ordering.

The hidden verifier now computes expected semantics through an in-memory state model independent of SQLite and submitted code, then verifies the retained expected hashes against that model before comparing a candidate. The hidden scenario/seed/fault/gate input manifest is independently recomputed by verifier and admission under SHA-256 `07820d2df273f0ee306432acec1a3e3ec0aba903ebda857db044c5dca3fbf442`. The accurately scoped three-file initial-workspace manifest has SHA-256 `cf010f8a2e3fbe5077674303c9a8be4994ebf0486b2a84a7543f28605125a5e6`; it makes no self-hash claim.

The final admission matrix passed all 17 entries. Gold had six gate values of `1`; no-op and all eight exploit fixtures received `0`; each focused mutant had exactly its intended gate `0` and every other gate `1`; a deliberately broad mutant had all six gates `0` and was rejected from attribution. The candidate task tree, hidden tests/model, mutants, gold solution, candidate-only admission tool, and candidate-only project test are absent from both wheel and source distribution. Public source is explicitly non-contamination-resistant; runtime hiding claims only separate-verifier image placement.

All five Docker tests passed separately. The full Python 3.12 suite passed 687 tests, including the real Harbor oracle with exact reward `1`, all six gates, native artifact validation, and binary pass rate `1`. Lock/sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build and exclusion, isolated locked-runtime wheel installation, all-groups pip-audit, shell syntax, `git diff --check`, full-history Gitleaks over 26 commits, and worktree Gitleaks passed. Three local gold repetitions, two detached repetitions, detached audits, calibration, and catalog admission remain `unproven`.

## 2026-08-29T16:54:54-07:00: E-069 authority-boundary correction

Provenance: user-provided P2 findings, local adversarial process and filesystem regressions, repeated fresh-image admission matrices, one focused and two suite-contained real Harbor 0.22 Docker oracles, and complete local CI/security/package validation. No catalog entry, commit, push, provider call, detached run, or calibration occurred.

The verifier no longer calls `communicate()`. A selector drains stdout and stderr under one cumulative 64 KiB retained-byte cap. Every submitted CLI starts in its own process group. Output overflow, timeout, and direct-parent exit kill the group; the verifier then drains for a bounded interval, closes both pipes, and reaps the direct child. Infinite output, aggregate stdout/stderr overflow, and a forked descendant retaining the output pipe all fail in under two seconds in regression tests, and the descendant PID is absent afterward.

Submission validation now rejects an unknown root name before stat or content work and stops after the four expected entries plus one excess-entry observation. A regression creates 5,000 empty unknown directories, instruments `scandir`, consumes exactly one entry, and completes under one second.

Default admission snapshots the tests build context, excludes only Docker-ignored bytecode, computes canonical digest `53d3ef1476eaf21e5bc9197d766f80b05780a0b93df72b4fa51015bbba7853d4`, and builds a fresh no-cache image with a unique context-addressed tag and nonce-bound image identity. Canonical evidence records the build and invocation commands, immutable image ID and any repository digests, context digest, and Docker, Python, Harbor, and tetrabench versions. `--image` and `--skip-build` require `--debug`; debug evidence is always marked non-admission, cannot report proof success, and exits nonzero. A tailored fake-image regression reproduces the expected matrix while still receiving `admissible=false` and `ok=false`.

The focused fresh-image matrix and real Harbor oracle passed. All five Docker tests passed, then the full 693-test Python 3.12 suite passed with the real oracle's exact reward `1`, six passing gates, native artifact validation, and binary pass rate `1`. `uv lock --check`, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build and exclusion, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, shell syntax, `git diff --check`, and redacted full-history Gitleaks over 26 commits passed. Detached admission, repetitions, calibration, and catalog admission remain `unproven`.

## 2026-08-29T17:32:47-07:00: E-070 authority-fencing authority closure

Provenance: user-provided remaining review findings; final fresh no-cache 17-entry matrix; real Harbor 0.22 oracle; all five marked Docker tests in one selection; separate 696-test non-Docker suite; two fresh verifier builds; and complete local CI, package, dependency, and history-scan parity. No catalog entry, commit, push, provider call, detached run, or calibration occurred.

Fault schedules are now strict closed records containing only ID, public checkpoint ID, and public expected exit code. Duplicate IDs, missing or unknown fields, wrong types, unknown checkpoints, exit disagreement, and fault-case reference omissions fail closed. Tests recompute both hidden and public input digests after adversarial mutations; a changed checkpoint or exit still cannot pass. Fault cases resolve their schedule by ID, and both model and runner consume its declared values.

One verifier-owned `derive_schedule` function now expands every seed into identities, TTLs, contenders, action and argument order, invalid inputs, terminal attempts, and an optional resolved fault. The independent in-memory model and subprocess runner consume that schedule rather than duplicating randomization. Seed 211693 now has one frozen invalid-TTL order `(-6, 0)`. Effect hashes bind ordered operations and semantic arguments; reversing the trace changes its digest. Hidden case input identity remains `07820d2df273f0ee306432acec1a3e3ec0aba903ebda857db044c5dca3fbf442`, while regenerated expected effect hashes reflect the stronger trace.

Admission evidence now labels fresh-build details as `run_attestation` and exposes a separate canonical `subject` plus `subject_sha256`. The subject binds task and verifier context digests, the exact ordered matrix projection, source revision when available, pinned base image, hidden inputs, and tool versions. Run attestation contains the nonce, image ID, tag, repository digests, and a build command whose temporary IID and context paths are placeholders. Two fresh no-cache builds of identical source produced the same subject digest `f77fbc0c4eb71b78cc334e2e83302e43ec0b2283485f79ece0ed5d071daa2665` while their nonces and image IDs differed. No reproducibility claim applies to whole attestation bytes or image IDs.

CI sets an exact Docker-marker expectation, runs `pytest -m docker` once, then runs `pytest -m "not docker"`. A collection regression proves a missing required marker fails accounting. The final matrix and Harbor oracle passed against verifier context `6a9a8d22be11b51b003b7b693423d6ab5c1800b06859afa820616fa5245f5b6c` and task context `7a80e56ede15b2cd2c60b714569147bc49a24d3552a7d4b324948cf459df4bed`. All five Docker tests passed together; 696 non-Docker tests passed separately. Lock and locked sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, shell syntax, `git diff --check`, and redacted full-history Gitleaks over 26 commits passed. Detached admission, repetitions, calibration, and catalog admission remain `unproven`.

## 2026-08-29T17:51:14-07:00: E-071 final authority-fencing evidence closure

Provenance: user-provided final evidence findings; local filesystem and temporary-Git regressions; a fresh 17-entry admission matrix; real Harbor 0.22 oracle; five marked Docker tests; 699-test non-Docker suite; and complete local CI, package, dependency, and history-scan parity. No catalog entry, commit, push, provider call, detached run, or calibration occurred.

Build and source identity now use explicit canonical manifests. Every directory and regular file carries a normalized relative path, type, permission mode, and file size/digest where applicable; links and special files fail closed. Empty directories and directory/file mode changes alter the build-context digest. The admission subject binds the complete task tree, admission tool, and candidate evidence test. It reports Git `HEAD` only when all bound entries exist there and every file byte and mode matches; otherwise it emits null revision, dirty state, and relies on the source-manifest digest. The current untracked candidate is therefore recorded as dirty rather than attributed to an unrelated commit.

Concurrent expiry claims now hash one canonical winner/loser relation instead of sorted anonymized effects. The verifier independently requires exactly one successful contender and durable worker/token state naming that actual winner. The task documentation classifies this case as relation/property-based because thread scheduling is nondeterministic; ordered effect traces remain the contract for deterministic schedules.

The fresh matrix and real Harbor oracle passed in the five-test Docker selection. The final dirty source manifest is `ba633e243f4f707fc884da14950a0967256a46d115c23eebc7d3933ef882b32a`; subject `361d022ebe6de4fa8cc7152ff97e872b8ce516548f08e88c3f1a655b62b1f908` binds verifier context `3ee5c4f1f1d74ada191b70835b5df2b74b864abecf3472277b0027fd18f9b151` and task context `b9a5091bb444914aefd51dab44e91357bac7da75b7f131a516c396b10c1c6ca8`. Five Docker tests and 699 non-Docker tests passed. Lock/sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, shell syntax, `git diff --check`, and redacted full-history Gitleaks over 26 commits passed. Detached admission, repetitions, calibration, and catalog admission remain `unproven`.

## 2026-08-29T18:23:21-07:00: E-072 concurrent authority evidence closure

Provenance: user-provided final P2 finding; verifier and admission tamper regressions; a fresh no-cache 17-entry admission matrix; real Harbor 0.22 oracle; all five marked Docker tests; 703-test non-Docker suite; and local CI, package, dependency, and history-scan parity. No catalog entry, commit, push, provider call, detached run, or calibration occurred.

Trusted verifier diagnostics now retain the concurrent expiry claim's actual winner worker ID, loser worker ID, winning fence token, and durable owner/token match result. The verifier requires the observed result identities to equal the two contenders from the seed-derived hidden schedule and requires exactly one exit `0` plus one semantic rejection. Durable state must name that winner at token `2`. The admission report preserves the same record in each candidate entry and requires a complete true record before any reward-1 result can become admission evidence. Unseeded IDs, two successes, durable mismatch, malformed records, false match results, and missing passing evidence fail closed.

These runtime identities remain outside expected state and effect hashes. The existing canonical winner/loser relation remains the deterministic oracle, so scheduling may select either seeded contender without changing frozen expectations. The three-file initial workspace manifest changed only because the task README and seed copy now state this boundary; its digest is `841bfa17a5a9932019b673c80cf26f0511ad4f522534e569396ec392fcafe437`.

The final fresh matrix retained gold admission evidence and passed all 17 entries. The real Harbor oracle retained trusted diagnostics and passed within the five-test Docker selection. The verifier context is `996106f5cfd56c5ccaffdfc78be31921557f9ef5beab4e5748cb3bc218d01978`, task context is `de21e0be7ec2d320f568f5e1aa011264d9a7922c27002db9dbfce729df3f800d`, and dirty source manifest is `e2ce2c86f907ea445ee7e16f4d1b6c3f4d19056b17348c8b74d2c66a757d74db`. All five Docker tests, the separately rerun admission matrix, 703 non-Docker tests, and 24 focused non-Docker task tests passed. Lock/sync, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation, all-groups pip-audit, shell syntax, `git diff --check`, and redacted full-history Gitleaks over 26 commits passed. Detached admission, repetitions, calibration, and catalog admission remain `unproven`.

## 2026-08-29T23:39:55-07:00: E-073 N=1 production admission proof

Provenance: user-provided admission-attestation requirements, audit of the interrupted 705-line diff, focused adversarial regressions, one retained actual production CLI proof, six real-Docker tests, the 713-test non-Docker suite, and complete local CI/security/package parity. No commit, push, provider call, detached run, calibration, catalog mutation, or N=3 proof occurred.

The audit found and corrected four authority defects. The attestation serialized its private output path; it now records a fixed placeholder. The bounded production-command reader could block past its deadline after a parent exited while a descendant retained a pipe; it now uses selector readiness, one cumulative output cap, and process-group cleanup. The runner incorrectly required Harbor's legacy `Task.checksum` and native `TrialLock.task.digest` to be equal even though Harbor 0.22 computes them with different pinned algorithms; it now independently recomputes and validates both. Native proof accepted an incomplete artifact projection and only partially checked binary summary/reward shape; it now requires Harbor's convention entry plus the declared workspace entry, exact raw integer reward, complete canonical summary validation, and job/trial lock/config coherence.

The final N=1 attestation is `~/.local/share/opencode/tetrabench-research/authority-fencing-admission/2026-08-29-n1-final.json`, SHA-256 `84641c49d1492664d108cc9d9064e792b4132377e5b62cc2b6b9e721583e73b4`, size 18,373 bytes, and mode `0600`. It records `ok=true`, all 17 matrix entries passing, one ordered production `tetrabench run`, exact binary reward and pass rate `1`, source state `dirty` with `source_revision=null`, canonical subject `fec8ff53f6c21e3e6ff55321de8f6c6aeed4f374e93fe5509718c2b529d39696`, verifier context `1ceae25b2dab592c0883a915c095894d9eabea71b4ed4cd480bdfa32dee7de71`, task context `6ee63830af0bdaf7dab199513e1bbe53359000384f3fc96b8b172929d733071e`, and source manifest `6ca45b74edde722cd84ff164ce6b27ddabf32a7544d4cebafddeb4298ebb95a4`.

All six marked Docker tests passed once in the final selection, including the N=1 production route. All 713 non-Docker tests passed separately. Lock and locked sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build and candidate exclusion, isolated locked-runtime wheel installation and metadata/content smoke, all-groups pip-audit, shell syntax, `git diff --check`, and redacted worktree plus 27-commit history Gitleaks scans passed. The required clean N=3 proof remains deliberately ungenerated and `unproven` until after commit.

## 2026-08-30T00:26:38-07:00: E-074 admission-attestation authority closure

Provenance: user-provided admission-attestation review findings, focused Linux process/filesystem regressions, one explicit dirty N=1 production route, all six marked Docker tests, the 721-test non-Docker suite, and complete local CI/security/package parity. No commit, push, provider call, catalog mutation, detached run, calibration, retained admissible proof, or N=3 execution occurred.

Admissible proof now captures `HEAD` once, requires the whole worktree and index clean plus every bound source tracked at that revision, and extracts one private `git archive`. That snapshot owns the full task, verifier context, candidates, gold solution, temporary catalog/project, `src/tetrabench`, project metadata and lockfile, admission tool/helpers, wheel build, and locked private CLI installation. The task `tests/` manifest must exactly equal the verifier image context. Evidence binds the archive, source manifests, wheel, locked export, installed metadata/dependency inventory, normalized private Python/CLI paths, versions, and source revision. A dirty tracked/untracked copy is available only under explicit debug mode; it always reports `admissible=false`, `ok=false`, exits nonzero, and cannot write proof output.

Each production CLI spawn first makes the runner a Linux child subreaper and starts one process group. Completion and every failure path use `/proc` PPid relationships to find adopted and escaped descendants, kill groups and individual PIDs, boundedly drain pipes, and reap children; unavailable evidence or a survivor fails proof. Regressions cover setsid with closed pipes, double fork, retained-pipe timeout, aggregate output, and a delayed daemon mutation attempt. The actual N=1 route observed and reaped one post-exit descendant with zero survivors.

Proof output retains every no-follow parent descriptor from POSIX root or cwd through final exclusive `openat`, writes exact `0600` canonical bytes, and fsyncs file and parent. Intermediate replacement fails without redirecting or creating output. After trusted CLI exit and containment, one bounded descriptor-rooted traversal captures the complete exact-`0700` run output, rejects links, hard links, and special files, and reads every Harbor model/digest from that in-memory snapshot only. Task copying compares source-before, source-after, and destination manifests, and the complete relevant source manifest is rechecked after execution.

The non-Docker executor tests make three ordered independent calls, stop without retry on call two failure, retain only the first partial record, and prove that neither `full_runs_ok` nor `admissible` can become true. Six Docker tests passed in one final selection, including the explicit dirty N=1 matrix and isolated production CLI path; 721 non-Docker tests passed separately. Lock/sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build and candidate exclusion, isolated locked-wheel smoke, all-groups pip-audit, shell syntax, diff checks, and redacted worktree plus 27-commit Gitleaks scans passed. The required clean N=3 proof remains deliberately ungenerated and `unproven` until after commit.

## 2026-08-30T00:29:03-07:00: E-074 validation-count correction

Provenance: final path-redaction regression and repeated complete non-Docker selection. The preceding E-074 count is superseded by 722 passing non-Docker tests. The added regression proves that failure records replace private absolute paths with a fixed message. All six Docker tests remain passed from the final single Docker selection; no N=3 execution occurred.

## 2026-08-30T00:31:57-07:00: E-074 output-failure correction

Provenance: final proof-output failure regression and repeated complete non-Docker selection. The preceding E-074 validation count is superseded by 723 passing non-Docker tests. An injected file `fsync` failure now proves the exclusive proof file is unlinked and its retained parent is synced best-effort before the error returns. All six Docker tests remain passed from the final single Docker selection; no N=3 execution occurred.

## 2026-08-30T00:53:29-07:00: E-075 exact-three and retained-output authority closure

Provenance: user-provided final admission P1 findings; focused parser, status, partial-run, owner/mode, and filesystem-race regressions; all six marked Docker tests; 738-test non-Docker suite; and complete local CI/security/package validation. No commit, push, clean N=3 execution, provider call, catalog mutation, detached run, or calibration occurred.

Only exactly three requested complete production runs can now set `full_runs_ok`, `admissible`, or `ok`. Successful N=1 and N=2 executions set only `diagnostic_runs_ok`, remain non-admission, exit nonzero, and cannot accept `--output`. N=0 and N>3 fail at the parser boundary. Ordered execution still stops on the first failure, retains only prior records, and never retries. The existing dirty N=1 Docker route remains explicit non-admission.

Retained output now rejects every anchored parent whose owner differs from the current effective UID, whose mode is group/world writable, or whose sticky bit is set. The exclusive file descriptor remains open through exact byte reads, repeated `fstat`, no-follow visible-name dev/inode/mode/owner comparison, file and parent `fsync`, and a final repeated name/inode/byte check before close; the visible identity is checked once more after close. Missing, renamed, or replaced output fails without reporting output success, and cleanup never unlinks a replacement inode. Regressions inject replacement during parent `fsync`, rename during file close, parent replacement, file `fsync` failure, untrusted modes, and owner mismatch. A trusted same-UID process can still mutate the file after return; no post-return immutability is claimed.

All six Docker tests passed once, including dirty N=1 non-admission, and all 738 non-Docker tests passed separately. Lock and locked sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-wheel installation and metadata/content smoke, all-groups pip-audit, shell syntax, `git diff --check`, and redacted Gitleaks over all 27 commits passed. The required clean N=3 proof remains ungenerated and `unproven` until after commit.

## 2026-08-30T01:00:58-07:00: Proof-output ancestor policy correction

Provenance: user-provided proof-output path contract. Implementation and validation evidence are pending.

Absolute and relative proof paths are supported. Every ancestor may be owned by root or the current effective UID and must not be group/world writable, except that a root-owned sticky world-writable ancestor such as `/tmp` is allowed. The final output parent must be owned by the current effective UID and must not be group/world writable; direct output beneath `/tmp` therefore remains forbidden. Its descriptor stays open through the exclusive write and existing identity checks. Unrelated-owned ancestors and nonsticky writable ancestors fail closed. D-083 records this correction to D-082.

## 2026-08-30T01:12:13-07:00: E-076 proof-output path validation

Provenance: focused Linux owner/mode/path regressions, all six marked Docker tests, the 743-test non-Docker suite, and complete local CI/security/package parity. No commit, push, clean N=3 execution, provider call, catalog mutation, detached run, or calibration occurred.

Descriptor traversal now applies separate ancestor and final-parent policies. Root- and current-euid-owned nonwritable ancestors pass. A root-owned sticky world-writable ancestor passes, allowing an absolute `/tmp/<private-0700>/proof.json`; direct `/tmp/proof.json` fails because `/tmp` would be the final parent. The final parent remains current-euid-owned and not group/world writable, including when it has a nonwritable sticky bit. Tests also pass for an absolute path beneath the actual home directory and a cwd-relative private path, and reject a mocked unrelated-owner ancestor plus a nonsticky `0777` ancestor. Existing parent and output replacement races remain passing. The synchronized task README changed the bound initial-workspace digest to `af7a552f51fa1ebb728c21596e1228630aabe408df67d161dadd8e40de254791`.

All six Docker tests and 743 non-Docker tests passed separately. Lock and locked sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-wheel installation and metadata/content smoke, all-groups pip-audit, shell syntax, `git diff --check`, and redacted Gitleaks over all 27 commits passed. No N=3 run occurred.

## 2026-08-30T01:45:41-07:00: E-077 clean authority-fencing local N=3 proof

Provenance: retained private canonical admission report from the clean committed source snapshot at `8fc7c78bc9c59da9b188ed9a9800425f08203d7a`, report metadata and digest verification, and post-run Docker/process cleanup inspection. No private run UUID is recorded here.

The clean local admission passed all 17 gold, no-op, focused-mutant, broad-mutant, and exploit matrix entries, then ran exactly three ordered and distinct production `tetrabench` CLI executions without retry. Each run succeeded with raw integer reward `1`, pass rate `1/1`, separate-verifier execution, native provenance, zero retries or errors, and zero surviving descendants. Their durations were approximately 33.35, 33.41, and 33.38 seconds. No current-run Docker residue remained.

The canonical subject is `55008e986151650fcd441496f9cd1f651471a746611b9cbbcf7d5a5c4e95bce4`. Its source manifest is `fdf94bf71970b20bd76df6519b5f4ea01df0d1d6c82822e4ed3f45350597bc40`, task context is `7b6bfb34f19910ca832de6c6c05ac12d76ef1ecc832c7dd711aa4a41967ccc1d`, and equal task-tests/verifier context is `460f3fd934ee4c6ba63f0214aae3c5cb5b2213fe20cafa5b9ee2b75d1d63374a`. The locked wheel is bound by SHA-256 `515c88567e8d1ae777a7fa6b99403dbda61cf608ff0b73a6630dcb13794a78c8`, the fresh verifier image by `sha256:a51f6011aa7b272dbb84d9e3836b819e1fbc71c0b17f5f6ed3238f2a9339faef`, and every native trial by task digest `sha256:1b100e6225edaf5ce9fe646d135bb7b86298c76709ce4dc4b0f62eef6b340ddd`.

The retained report is `~/.local/share/opencode/tetrabench-research/authority-fencing-admission/2026-08-30/authority-fencing-local-n3.json`, mode `0600`, 52,944 bytes, SHA-256 `df0e0b1e38325708e187f09ac1ad4a0b851f5f1ab212e7947994f817e308d3c5`. Local gold repetition, no-op, mutant, and exploit gates are passed. Two detached repetitions, the detached audit, two-profile calibration, budget completion, and catalog admission remain `unproven`.

## 2026-08-30T02:30:41-07:00: E-078 local calibration runner boundary

Provenance: user-provided calibration and credential-broker contract; pinned Harbor 0.22 OpenCode adapter inspection; current OpenCode provider documentation; fake-upstream adversarial tests; production `tetrabench plan` compilation for both fixed profiles; and an unauthenticated gateway liveliness request. No model completion, calibration attempt, credential file, proof output, commit, or push occurred.

The source-only runner fixes `target` to Harbor model `openai/openai/gpt-5.6-sol` and `alternate` to `openai/anthropic/claude-sonnet-5`, with two ordered unretried attempts each. The parent gateway key is read only from `TETRABENCH_CALIBRATION_GATEWAY_KEY`. A host broker issues one high-entropy token per attempt and accepts only the exact route, model, method, framing, bounded body, output-token count, request count, concurrency, and deadline. It forwards to the fixed LiteLLM gateway after replacing authorization, strips sensitive response headers, streams boundedly, records no model content, and requires an authoritative finite nonnegative response-cost header for every successful model response under one `$25` ledger and conservative `$5` final-request margin.

The runner reuses the admission tool's clean single-HEAD archive, locked wheel installation, absolute production CLI, subreaper containment, no-follow native snapshot, native Harbor validation, and private fd-anchored evidence writer. Exact four-attempt clean execution is the only admissible mode; one attempt per profile is dirty diagnostics without proof output. Fake-upstream tests cover the broker and production profile compilation without model spend. The gateway liveliness endpoint returned HTTP 200 from this host. The broad virtual key is not revoked because it remains in OpenCode auth; the runner creates no key file. Real calibration remains `unproven` until this code is committed and the clean four attempts are explicitly run.

## 2026-08-30T02:48:59-07:00: E-078 local validation

Provenance: final local Python 3.12 validation, six real-Docker tests, fake-upstream broker adversaries, production profile compilation, package inspection, dependency audit, and redacted secret scans. No model completion, calibration attempt, credential file, proof output, commit, or push occurred.

All six Docker tests and 779 non-Docker tests passed. The broker suite covers wrong, expired, invalidated, and duplicate credentials; forbidden methods, routes, traversal, framing, body, headers, models, output tokens, requests, and concurrency; response streaming, disconnect, malformed or missing costs, shared spend margin, sensitive response headers, inflight shutdown, and child-environment credential exclusion. Both fixed profiles compile through the production `tetrabench plan` command without network or model access.

Lock and locked-environment checks, Ruff check and format, ty, Bandit, actionlint, wheel/sdist build and source-only exclusion, isolated locked-wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted full-history plus worktree Gitleaks passed. The only network request was the unauthenticated gateway liveliness check, which returned HTTP 200. Real calibration remains `unproven` and no final attempt ran.

## 2026-08-30T03:28:54-07:00: E-078 calibration review correction

Provenance: user-provided three-review correction contract; LiteLLM `/model/info` interface inspection; fake-upstream and direct ledger tests; 814-test non-Docker suite; six pre-existing real-Docker tests; package, audit, lint, type, security, and secret-scan checks; and one Docker default-bridge reachability test. No authenticated gateway request, model completion, calibration attempt, proof output, commit, or push occurred.

The runner now performs one authenticated exact-group pricing and model-limit preflight before any broker. It retains only selected rates, limits, and their canonical digest. Finite positive rates above the fixed `$10` input/cache or `$50` output per-million ceilings fail closed. Each request reserves its exact forwarded-body byte upper bound and 8,192 output tokens at those ceilings plus `$0.25`; the shared ledger makes reservation and settlement atomic under `$25`. Endpoint-specific output-token schemas, the first-valid-request endpoint lock, bounded accepted-socket workers, pre-forward success-metadata validation, mandatory coherent ATIF aggregate token metrics, and actual endpoint/reservation/cost evidence are covered by fake-upstream tests.

Proof mode now rejects caller-selected addresses and discovers the rootful Docker default `bridge` gateway and host interface. Debug mode accepts loopback only and exactly one attempt per profile. The bridge reachability test exposed this workstation's UFW boundary: a container on Docker's default bridge timed out connecting to the host gateway listener because host `INPUT` defaults to drop and has no matching TCP allow rule. The six existing Docker tests pass under the CI-parity `uv run` environment, and all 814 non-Docker tests pass. Successful container-to-broker reachability remains `unproven` on this host; the broker made no paid upstream call.

## 2026-08-30T03:59:03-07:00: E-078 fixed-port fail-closed topology completion

Provenance: user-provided topology correction; one real disposable-container probe on Docker's default bridge; a substituted-connector success test; fake-upstream and adversarial HTTP tests; all 818 non-Docker tests; all seven Docker tests; and complete local lint, type, package, dependency, security, workflow, diff, and secret-scan parity. No authenticated gateway request, model completion, calibration attempt, firewall mutation, proof output, commit, or push occurred.

Proof mode now binds only the discovered default-bridge gateway and fixed/configured high port `62017`. Before authenticated `/model/info` or any model request, a broker accepts one distinct ephemeral-token `204` probe from a disposable default-bridge container; this route cannot forward upstream. On this workstation the container timed out at the firewall, the probe returned a fixed failure, pricing was never called, authoritative cost remained zero, and no evidence was admissible. The successful route is covered through a substituted connector that performs the same authenticated HTTP exchange against a loopback test broker.

The runner contains no `sudo`, UFW, or firewall mutation path and never binds a wildcard interface. User documentation permits only an operator-installed temporary rule scoped to the discovered bridge interface, source subnet, gateway, and TCP port `62017`, with deletion of that exact rule after success or failure. There is no standing-rule contract.

The retained pricing snapshot still selects only the two exact model groups and finite positive rates/limits, redacts provider configuration, and enforces `$10` input/cache and `$50` output per-million ceilings. Forwarded UTF-8 bytes bound input tokens; endpoint-specific output fields include `max_completion_tokens`; output is capped at 8,192. Decimal reservations and authoritative settlement are atomic under `$25`, each actual cost must fit its reservation, and the recorded total must equal ledger cost. Accepted-socket workers are bounded before thread creation, each attempt locks its first valid endpoint, invalid successful upstream metadata returns an immediate closing `502`, and native ATIF prompt and completion tokens must both be positive and agree with Harbor evidence.

`uv lock --check`, locked all-group sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build and source-only exclusion, isolated locked-wheel metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over 30 commits plus the worktree passed. Successful bridge reachability and the clean exact-four calibration remain `unproven`.

## 2026-08-30T05:07:18-07:00: E-081 probe-expiry validation and note-order correction

Provenance: focused deadline/race tests, complete local Python 3.12 validation, seven real-Docker tests, package inspection, dependency audit, and a redacted staged-diff secret scan. No authenticated gateway request, model call, firewall command, or proof output occurred.

The 05:00:19 probe-expiry contract entry was accidentally inserted before the existing 04:09:57 through 04:53:22 entries instead of appended at the end. This entry records that ordering error without rewriting any prior note. The contract itself remains unchanged.

Ten focused probe tests, all 843 non-Docker tests, and all seven Docker tests passed. `uv lock --check`, locked all-group sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build and source-only exclusion, isolated locked-wheel metadata/content smoke, all-groups pip-audit, and `git diff --check` passed. Digest-pinned Gitleaks scanned the complete staged diff and found no leaks. Successful bridge reachability and the clean exact-four calibration remain `unproven`.

## 2026-08-30T05:00:19-07:00: Probe-expiry atomicity contract

Provenance: user-provided probe-expiry correction and focused local regressions. No firewall, gateway, provider, or model call is authorized.

`consume_probe` must evaluate `now >= deadline` while holding the same broker state lock that owns active state, token authentication, and one-shot consumption. Exact equality is expired. Expiry rejects before comparing or consuming the token, clears usable broker credentials, and permanently marks the broker inactive. Moving the clock backward cannot reactivate it. A request admitted before the deadline may consume the token; concurrent requests blocked on the lock evaluate expiry only after acquiring it.

Focused tests cover immediately before, exactly at, and immediately after the deadline plus two valid callers held across expiry. Ten focused probe tests pass. Full validation remains pending and will be recorded separately.

## 2026-08-30T04:09:57-07:00: Calibration final-finding contract

Provenance: user-provided six-finding calibration contract. Implementation and validation evidence are pending. No firewall, gateway, provider, or model call is authorized.

Every forwarded upstream response, including 4xx and 5xx, must carry exactly one finite nonnegative authoritative `X-Litellm-Response-Cost`. Missing, malformed, ambiguous, or otherwise unavailable settlement makes the shared ledger fatal, retains the full worst-case reservation as unknown spend exposure, closes the upstream response, and prevents every later request from forwarding. Known cost above its reservation or the total cap remains fatal. Endpoint lock and request count commit only after reservation succeeds, atomically under the broker state lock.

The broker derives child response metadata solely from the validated request stream mode: `text/event-stream` for streaming and `application/json` otherwise. It never reflects upstream content type, encoding, or other header values. Before potentially paid work, the runner appends one bounded started attempt record. Any later harness, native, or validation failure changes that record to failed with ordinal, profile, model, locked endpoint when present, request count, known actual cost, retained unknown-cost reservation, and no model content. Failure reports count started attempts and spend exposure, while failed attempts can never become admissible or reach proof output.

Proof order is the unauthenticated disposable-container broker probe, authenticated pricing, then attempts. A blocked topology report must include exact paired `sudo ufw allow ...` and `sudo ufw delete allow ...` commands derived from the validated discovered interface, subnet, gateway, and fixed port. Those commands contain no credential. Documentation must show their form and require try/finally removal; no standing firewall rule is permitted. D-086 records this boundary.

## 2026-08-30T04:31:44-07:00: E-079 calibration final-finding validation

Provenance: fake-upstream and direct concurrency regressions, all 836 non-Docker tests, all seven real-Docker tests, and complete local CI/security/package parity. No authenticated gateway request, model completion, calibration attempt, firewall command, proof output, commit, or push occurred.

Every forwarded 2xx, 4xx, or 5xx response now requires one finite nonnegative authoritative cost. Missing, malformed, ambiguous, negative, and nonfinite settlement returns a closing `502`, leaves the exact reservation retained, marks the shared ledger fatal, and makes a later valid request stop before upstream. Valid 4xx and 5xx responses settle and forward. Known-cost later response validation failures settle that known cost before invalidating the ledger. The upstream response closes on every path.

Reservation occurs while the broker state lock is held and precedes endpoint/count commitment. Direct failure and concurrent one-winner tests prove that a failed reservation changes neither endpoint nor count. Validated boolean stream mode alone selects `text/event-stream` or `application/json`; upstream content type and encoding never cross the broker, and response length is derived from retained bytes.

The runner appends a started record before each potentially paid attempt. Failure retains ordinal, profile, model, locked endpoint when available, committed request count, known authoritative cost, unknown-cost reservation exposure, and no content. The failure report includes started attempts and summed exposure, while proof publication remains reachable only from a complete admissible run. Proof execution order is disposable-container reachability, authenticated pricing, then attempts. A blocked topology report now includes exact secret-free UFW add/delete commands derived from the validated interface, subnet, gateway, and port, plus an explicit try/finally removal requirement.

All 836 non-Docker tests and all seven Docker tests passed. `uv lock --check`, locked all-group sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build and source-only exclusion, isolated locked-wheel metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over 30 commits passed. Successful bridge reachability and the clean exact-four calibration remain `unproven`.

## 2026-08-30T04:53:22-07:00: E-080 topology-probe token-consumption correction

Provenance: user-provided final calibration finding; invalid-then-valid, missing-then-valid, and synchronized concurrent-valid HTTP regressions; all 839 non-Docker tests; all seven real-Docker tests; and complete local CI/security/package parity. No authenticated gateway request, model completion, calibration attempt, firewall command, proof output, commit, or push occurred.

The topology-probe broker now validates active state and one exact bearer Authorization value while holding the same state lock that owns one-shot token consumption. Missing or invalid authentication leaves the token usable. Concurrent valid requests serialize at that boundary, so exactly one receives `204` and consumes the token while the other receives `401`. Calibration documentation now states the execution order directly: topology probe, authenticated pricing, then attempts.

`uv lock --check`, locked all-group sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build and source-only exclusion, isolated locked-wheel metadata/content smoke, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over 30 commits plus the worktree passed. Successful bridge reachability and the clean exact-four calibration remain `unproven`.

## 2026-08-30T05:08:53-07:00: E-081 note-placement correction

Provenance: local inspection of the append-only notes after recording E-081.

The 05:07:18 validation entry was also inserted after the 03:59:03 entry because its patch anchor matched repeated validation text. The intended append-only order is represented by this final entry: the 05:00:19 contract and 05:07:18 validation were created after E-080, and the E-081 validation evidence in the 05:07:18 entry is authoritative. No earlier entry was rewritten or deleted.

## 2026-08-30T12:00:00-07:00: D-089 calibration sidecar transport contract

Provenance: user-provided credential-broker Docker-sidecar, overlay-identity, inspection, and cleanup contract. Implementation and validation are pending. No gateway, model, paid, firewall, commit, or push action is authorized.

The calibration atomic transport unit is one attempt: one fresh labeled external Docker bridge network, one broker container and alias, one disposable one-shot probe token, one distinct attempt token, one exact task overlay, and one broker-only redacted ledger directory. The host owns network/container creation and proven removal. The broker process alone owns the forwarding parent key after it arrives over an anonymous stdin pipe; Docker configuration and durable evidence may contain no parent key or prompt/content bytes. Harbor owns `main`, which joins the exact external network through an overlay that changes only `environment/docker-compose.yaml`; candidate bytes and overlay bytes retain separate manifests and digests, while native task identity includes the overlay.

Faithful nonpaid evidence must use the real Docker topology with a fake upstream or broker test mode, probe and simulated client containers, exact model/cost settlement, runtime inspect, and final absence checks. Adversarial coverage includes parent-key inspection, mount/socket separation, extra peers, broker crash, stale tokens, cleanup failures, overlay drift, and network/alias injection. Successful four-attempt calibration remains `unproven` and pending a clean post-commit run.

## 2026-08-30T12:58:00-07:00: E-082 sidecar transport validation

Provenance: two real-Docker sidecar tests, fake broker response mode, complete non-Docker suite, Docker residue inspection, and local lint, type, security, workflow, package, dependency, diff, and worktree secret scans. No gateway, model, paid, firewall, proof-output, commit, or push action occurred.

All eight marked Docker tests and 850 non-Docker tests passed. The first sidecar integration created a fresh external bridge network, started the pinned broker from the mounted source, passed credentials after start through stdin, consumed a one-shot probe token, accepted a distinct simulated-client token for exact model `openai/gpt-5.6-sol`, settled exact fake cost `$0.125`, and proved the broker/network absent. Docker inspection found the parent key absent from config, env, argv, mounts, and logs; the source was read-only, the evidence mount was broker-only and writable, no Docker socket existed, and the network retained bridge gateway egress. The second integration rejected an extra peer, killed the broker, rejected the stale attempt token, and again proved cleanup. No labeled calibration container or network survived.

Overlay tests retain separate exact candidate and overlay manifests and digests, reject instruction-byte drift, and bind only `environment/docker-compose.yaml` to the generated external network. Pricing digest/model binding, cleanup failure, and unsafe network/alias names fail closed. Ruff, format, ty, Bandit, actionlint, lock checking, wheel/sdist build, isolated locked-wheel smoke, all-groups pip-audit, `git diff --check`, and redacted worktree Gitleaks passed. The exact clean four-attempt calibration remains post-commit and `unproven`.

## 2026-08-30T06:48:54-07:00: E-083 calibration activation and lease correction

Provenance: user-provided Docker-sidecar calibration review findings; fake-upstream request adversaries; real Docker inactive-token, immutable-main-config, parent-death, and stale-network tests; all eight marked Docker tests; 877-test non-Docker suite; and complete local lint, type, security, package, dependency, diff, and full-history secret-scan parity. No gateway, model, paid, host-port, firewall, proof-output, catalog, commit, or push action occurred.

This entry corrects D-089/E-082 where they treated continuous peer polling, only-broker-and-main membership, immediate attempt-token authority, or `Internal=false` as security evidence. Docker daemon/root and its same-UID operator are trusted. Network inspection now records the actual IPAM gateway and `Internal` value as topology only. Authenticated pricing and later forwarding, not that topology, prove egress. Unrelated host containers are outside the evidence boundary.

Chat admits exactly one completion, injecting `n=1` when absent. Both endpoints reject multiplicity and Responses background aliases, non-text modalities, prediction, remote media, files, and unknown output-limit spellings. Message and input content admits text, tool calls, and tool results only; ordinary URL text remains text. Endpoint-specific 8,192-token caps remain, and the worst-case reservation covers every permitted output.

The broker starts with its attempt token inactive. Harbor `main` carries unique attempt/candidate labels and a readiness healthcheck. The pinned `docker compose up --wait` lifecycle blocks before OpenCode installation and execution while the runner validates one immutable Docker config/image digest, exact Harbor mounts, namespace/privilege/capability/device/security settings, socket absence, and task identity. Only then does the host activate over the anonymous stdin control pipe, which is unavailable to `main`. A 0.5-second heartbeat owns a two-second monotonic lease. Lease loss drops the parent key, revokes tokens, closes the listener, and exits the `--rm` broker within five seconds.

Cleanup no longer trusts create-call flags. It reconciles exact random names and unique labels after accepted-create disconnect, attach timeout, normal completion, and failure; terminates and reaps the attach client; and fails evidence if authority or an owned resource remains. Parent `SIGKILL` left only an inert network, and the next exact-owner-label startup sweep removed it. Logical manifests continue retaining repository-relative paths; only absolute host, temporary, and home paths are excluded from durable evidence. Candidate/overlay and native task fidelity are unchanged.

All eight Docker tests and 877 non-Docker tests passed. Ruff check and format, ty, `uv lock --check`, locked all-group sync, Bandit, actionlint, wheel/sdist build, isolated locked-runtime wheel installation and metadata/content checks, all-groups pip-audit, `git diff --check`, and redacted Gitleaks over 31 commits passed. No labeled calibration container or network survived. The exact clean four-attempt calibration remains post-commit and `unproven`.

## 2026-08-30T07:05:13-07:00: E-083 Docker-count and Harbor-lifecycle correction

Provenance: one additional real Harbor Docker lifecycle regression and repeated exact Docker-marker selection after the preceding E-083 entry.

The preceding E-083 count is superseded by nine passing Docker tests. The added test runs the pinned Harbor `docker compose up --wait` path with the Oracle agent, so it makes no model call. It proves the generated task overlay holds Harbor on the readiness healthcheck, the runner discovers and validates the actual labeled Harbor `main` with its exact `/logs/agent`, `/logs/artifacts`, and `/logs/verifier` mounts, activation completes, and only then can Harbor continue agent setup. The complete 877-test non-Docker selection remains passing from E-083. No labeled calibration container or network survived.

## 2026-08-30T07:07:59-07:00: E-083 request-adversary count correction

Provenance: five additional fake-upstream top-level modality regressions and repeated focused non-Docker validation.

The E-083 non-Docker count is superseded by 882. The added cases explicitly reject Responses `modalities`, `audio`, and `prediction`, plus Chat `modalities` and `audio`, before upstream forwarding. The nine-test Docker selection remains passing from the preceding correction.

## 2026-08-30T10:46:54-07:00: E-083 committed calibration findings

Provenance: user-provided post-commit calibration findings. Implementation and validation are pending. No gateway or model call is authorized.

Authorization must check heartbeat lease expiry atomically with bearer-token acceptance under the broker state lock. Exact or later expiry immediately revokes token, probe, parent-key, and in-flight upstream authority; the request rejects without relying on the watchdog. A heartbeat arriving after expiry cannot renew authority. Concurrent authorize-versus-expiry evidence must permit no post-expiry acceptance.

Harbor `main` immutable-config admission must reject every Docker port surface: any `Config.ExposedPorts`, `HostConfig.PortBindings`, `PublishAllPorts`, or runtime host-published/listening port representation. The fresh attempt bridge remains calibration-internal transport only and publishes no host port. Adversarial inspect fixtures and the full fake, Docker, security, package, and hosted-CI validation remain pending.

## 2026-08-30T11:07:00-07:00: E-084 lease and port-surface validation

Provenance: focused fake-broker tests, full non-Docker and real-Docker suites, direct Docker residue inspection, and complete local CI/security/package parity. No gateway or model call occurred.

`BrokerState.authorize` now reads monotonic time, checks the attempt deadline and heartbeat lease, invalidates authority, and decides bearer acceptance under one lock. Equality is expired. Invalidation clears attempt and probe tokens plus the parent key, closes registered upstreams, and sets the sidecar shutdown event so listener/process teardown starts without watchdog polling. Heartbeat, request admission, and upstream registration repeat the expiry boundary, so delayed renewal and forwarding fail closed. The exact-boundary test proved upstream closure and shutdown signaling; eight synchronized authorizers at expiry produced zero acceptance.

Immutable Harbor `main` inspection now rejects nonempty `Config.ExposedPorts`, `HostConfig.PortBindings`, and runtime `NetworkSettings.Ports`, and requires `HostConfig.PublishAllPorts` to be exact false. Four independent inspect adversaries cover those surfaces before activation. The existing real sidecar and Harbor `compose up --wait` tests passed with internal-only attempt networking and no host-published port.

The focused fake suite passed 145 tests. The full selections passed 888 non-Docker and nine Docker tests, with no owned calibration container or network remaining. Lock/sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-wheel smoke, all-groups pip-audit, `git diff --check`, and redacted full-history Gitleaks over 32 commits passed. Hosted CI remains pending.

## 2026-08-30T11:26:07-07:00: Connected-upstream calibration contract

Provenance: user-provided calibration P1 correction. Implementation and validation are pending. No gateway or model call is authorized.

Before parent-key authority is obtained or used, the broker establishes the upstream TCP/TLS connection without credentials. While holding the `BrokerState` lock it registers that connected `HTTPConnection`, disables automatic reconnect, and atomically rechecks the exact active child bearer plus heartbeat/deadline lease before beginning the credentialed send. Exact expiry clears authority and closes registered sockets under the lock. A handler whose credential send has not begun cannot open or reopen upstream transport or emit Authorization after expiry.

The credential-send boundary defines admitted in-flight reserved work. Expiry may close a request whose send already began; if authoritative settlement is then unavailable, the exact reservation remains retained and the ledger remains fatal. Deterministic evidence must cover expiry after child-request capture but before upstream send, socket closure, reconnect refusal, expiry during an established in-flight request, and zero post-expiry upstream request count and known cost. D-092 records this boundary.

## 2026-08-30T11:46:39-07:00: E-085 connected-upstream validation

Provenance: deterministic fake-upstream barriers; all calibration, non-Docker, and real-Docker tests; complete local CI/security/package parity; and direct Docker residue inspection. No gateway or model call occurred.

The forwarding path now establishes TCP/TLS before parent-key access. Under the broker lock it requires a connected socket, sets `auto_open=0`, registers the connection, rechecks the exact active child bearer and monotonic lease, and begins the parent-authorized request. Expiry clears authority and closes the registered set under that lock. The pre-send expiry barrier produced zero upstream HTTP requests and zero known cost, released its unforwarded reservation, preserved fatal broker behavior, and proved a later request could not reconnect. A request whose credential send began before expiry remained admitted reserved work and settled its authoritative `$0.125` response after expiry without admitting another request.

All 152 calibration tests, 892 non-Docker tests, and nine Docker tests passed. `uv lock --check`, locked all-group sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, redacted Gitleaks over 33 commits, and Docker residue inspection passed. No owned calibration container or network remained. Hosted CI remains pending.

## 2026-08-30T11:59:58-07:00: E-085 hosted validation

Provenance: GitHub Actions push run `33329238829` for commit `70124d51f13659a5156f25031619703903c34c46`.

The Python 3.12 job passed lock checking, locked installation, lint, format, type checking, Bandit, Docker availability, all nine required Docker tests, all 892 non-Docker tests, package build, isolated-wheel smoke, and the complete dependency audit. The independent full-history Gitleaks job passed. E-085 is complete.

## 2026-08-30T12:28:12-07:00: E-086 model-info preflight correction

Provenance: user-provided first real calibration outcome from clean commit `02452fa0f4262363e44484549ebb58160f94d784`; local fake-upstream framing and scale regressions; complete non-Docker and real-Docker suites; and local lint, type, security, package, dependency, diff, secret-scan, and Docker-residue checks. No gateway or model call occurred during this correction or its validation.

The first real calibration reached authenticated model-info pricing preflight, then stopped before pricing completion because the 2,231,684-byte response exceeded the old 1 MiB cap. It produced no report, started no attempts, incurred no spend, and passed cleanup. This is a failed preflight, not completed calibration; the clean exact-four calibration remains `unproven`.

The model-info control-plane read now has a separate named 4 MiB maximum and reads at most that cap plus one byte. It rejects declared or streamed overflow, duplicate or malformed Content-Length, conflicting transfer framing, truncation, malformed JSON, ambiguous required groups, invalid limits, and rates above the fixed hard ceilings. A valid document above 2 MiB with a bounded irrelevant row passes, while the retained canonical pricing snapshot still contains only the exact target and alternate profiles. The independent 64 MiB model-completion response cap is unchanged.

All 160 calibration tests, 900 non-Docker tests, and nine Docker tests passed. `uv lock --check`, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, redacted Gitleaks over 35 commits, and Docker residue inspection passed. No owned calibration container or network remained. Hosted CI remains pending.

## 2026-08-30T12:38:32-07:00: E-086 hosted validation

Provenance: GitHub Actions push run `33331034746` for implementation commit `586ebc26827bacf4553d9a715f8a34d5ea552718`.

The Python 3.12 job passed lock checking, locked installation, lint, format, type checking, Bandit, Docker availability, all nine required Docker tests, all 900 non-Docker tests, package build, isolated-wheel smoke, and the complete dependency audit. The independent full-history Gitleaks job passed. E-086 is complete, while the exact clean four-attempt calibration remains `unproven`.

## 2026-08-30T13:07:22-07:00: E-087 calibration failure-diagnosis contract and second run

Provenance: user-provided second real calibration outcome from clean commit `53a3748`; user-provided command-evidence and deny-upstream diagnostic contract; focused local fake-upstream, command-outcome, native-snapshot, and parser tests. No model completion occurred during this implementation or validation.

The second real calibration completed authenticated pricing, began target attempt 1, made zero broker or model requests, incurred `$0`, and failed with the old masked request-count error. It produced no report and left no calibration resource residue. This is not calibration evidence; calibration remains `unproven`.

A returned production CLI command now owns failure classification before a positive broker-request requirement. Attempt evidence retains only return code, stdout/stderr byte counts and SHA-256 digests, safe canonical schema/outcome/reward fields, bounded containment, and no-follow native structural status/counts/digests/exception class names. It retains no raw stream, path, exception message, prompt, model content, log, or tool output. Nonzero execution and malformed output clean resources, read the ledger with a zero request minimum, and retain precise stage/type.

The explicit `--debug-deny-upstream` mode is limited to debug's one attempt per profile and forbids proof output. After real pricing, its broker authenticates and validates a completion request, locks the endpoint, records the safe request/reservation shape, releases the reservation, and returns a fixed local 503 without opening completion upstream transport or using parent completion authority. It is always non-admissible and has zero completion cost. Full local and hosted validation remain pending.

## 2026-08-30T15:34:18-07:00: E-087 local validation

Provenance: 165 focused non-Docker calibration tests; the complete nine-test real-Docker selection; the complete 908-test non-Docker suite; fake pricing and completion-upstream accounting; package inspection; dependency audit; and local lint, type, security, workflow, diff, residue, and full-history secret scans. No gateway request or model completion occurred.

The fake pricing test made one authenticated model-info request, then the diagnostic broker accepted one valid completion request, locked `/v1/responses`, retained its reservation shape, returned local 503, released the reservation, opened no upstream completion transport, sent no parent completion authorization, and recorded zero cost. Zero-request nonzero execution retained `agent_install_or_execution/nonzero_exit` after cleanup and a min-zero ledger read. Malformed stdout retained `cli_schema/malformed_stdout`; failed native output retained only structural counts, manifest digest, and `ValidationError`, with private bytes and paths absent.

All 165 focused calibration tests, 908 non-Docker tests, and nine real-Docker tests passed. `uv lock --check`, locked all-group sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, redacted Gitleaks over 37 commits, and Docker residue inspection passed. Hosted CI remains pending. Calibration remains `unproven`.

## 2026-08-30T15:43:28-07:00: E-087 hosted validation

Provenance: GitHub Actions push run `33339548116` for implementation commit `267b5e748ed0cf2efbee0c4d4f1d83e5d1df7d63`.

The Python 3.12 job passed lock checking, locked installation, lint, format, type checking, Bandit, Docker availability, all nine required Docker tests, all 908 non-Docker tests, package build, isolated-wheel smoke, and the complete dependency audit. The independent full-history Gitleaks job passed. E-087 is complete. No model completion occurred, and calibration remains `unproven`.

## 2026-08-30T16:11:08-07:00: Third calibration diagnostic and stage-authority contract

Provenance: user-provided third real calibration outcome from the clean `0c8e6c`/`53a3748`/`02452fa` diagnostic path and user-provided preactivation instrumentation contract. No gateway or model completion occurred during this implementation.

The third diagnostic used deny-upstream. Authenticated pricing succeeded, then target attempt 1 failed before command return or main activation with zero broker or model requests and `$0` cost. It produced no report and left no calibration residue. The exact cause remains pending this instrumentation, so this is not calibration evidence and calibration remains `unproven`.

Each attempt now tracks `sidecar_start`, `topology_probe`, `cli_spawn`, `main_discovery`, `main_config_validation`, `broker_activation`, `heartbeat_start`, `cli_wait`, `ledger_read`, `native_validation`, and `cleanup` through ordered started/completed/failed booleans. A typed stage error retains only failed phase and cause class. If the command future completes before activation, returned commands retain only bounded return code, stream sizes and digests, safe canonical fields, containment, and native structural evidence; exceptions retain only their class and bounded native structure. Request-count validation cannot replace that earlier command authority. Full local and hosted validation remain pending.

## 2026-08-30T16:19:27-07:00: E-088 local validation

Provenance: focused phase and early-command regressions; the complete non-Docker and real-Docker suites; package inspection; dependency audit; and local lint, type, security, workflow, diff, worktree secret-scan, and Docker-residue checks. No gateway or model completion occurred.

All seven preactivation phases fail independently with exact started/completed/failed status and content-free phase/cause evidence. Early successful and nonzero command results retain bounded outcome evidence before main discovery failure; early timeout, output, and containment exceptions retain only their class and category. A preactivation failure followed by command completion captures the available `CommandResult` after containment and before the zero-minimum ledger read. Private test messages, stream bytes, and paths are absent from retained evidence.

All 178 focused non-Docker calibration tests, 921 non-Docker tests, and nine real-Docker tests passed. `uv lock --check`, locked all-group sync, Ruff check/format, ty, Bandit, actionlint, wheel/sdist build, isolated locked-wheel installation and metadata/content smoke, all-groups pip-audit, `git diff --check`, redacted worktree Gitleaks, and Docker residue inspection passed. Hosted CI remains pending. Calibration remains `unproven`.

## 2026-08-30T16:38:11-07:00: E-088 hosted validation

Provenance: GitHub Actions push run `33341954636` for implementation commit `6e3f1671efcf371f1e8dcb41a6ec5b42bd14d05e`.

The Python 3.12 job passed lock checking, locked installation, lint, format, type checking, Bandit, Docker availability, all nine required Docker tests, all 921 non-Docker tests, package build, isolated-wheel smoke, and the complete dependency audit. The independent full-history Gitleaks job passed. E-088 is complete. No gateway or model completion occurred, and calibration remains `unproven` pending a diagnostic rerun with the new stage evidence.
