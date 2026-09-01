# Tetrabench v1 task-family and admission contract

This document defines ten v1 task families and the contract that a fixture must
satisfy before it can enter a catalog. It is not yet a fixture-local manifest.
Each fixture owns the deterministic manifests defined below. The catalogs
remain empty until each fixture independently passes the mandatory gates in
[Admission](#admission). Source-only prototypes and an unlisted candidate exist;
none is admitted.

## Scope

V1 has two lanes with five task families each.

| Lane | Task | Required outcome |
| --- | --- | --- |
| Systems design through implementation | `authority-fencing` | Enforce lease or token fencing across expiry, restart, and delayed stale work. |
| Systems design through implementation | `atomic-outbox` | Commit order, inventory, and outbox state in one SQLite transaction, then dispatch at least once across crashes. |
| Systems design through implementation | `lifecycle-reconciliation` | Recover orphans, reconcile idempotently, resolve cancellation/completion races, and delete only the intended generation. |
| Systems design through implementation | `online-migration` | Complete expand, backfill, and contract while old and new versions coexist and interrupted work resumes without loss or duplication. |
| Systems design through implementation | `tenant-authorization` | Derive tenant authority from the principal, enforce role changes and revocation, and prevent lookup leakage on every path. |
| GitHub workflow | `pr-submit` | Prepare and submit a valid pull request from a real local Git repository. |
| GitHub workflow | `ci-repair` | Diagnose and repair a failing pull request without bypassing its checks. |
| GitHub workflow | `review-adjudication` | Fix and resolve two valid review threads and reject one fixture-defined contract violation. |
| GitHub workflow | `release-backport` | Backport one released fix to the correct maintenance branch with provenance and passing checks. |
| GitHub workflow | `merge-queue-recovery` | Recover a queued pull request after its base changes, rerun invalidated checks, and merge safely. |

V1 excludes essays and diagrams, live GitHub, cloud, or Kubernetes mutation,
browser tasks, large repositories, subjective style or commit-message scoring,
LLM judges, generic bug-fix tasks, and stacked pull requests. V2 may add stacked
pull request workflows.

## Related benchmark coverage

[SWE-bench](https://arxiv.org/abs/2310.06770) gives an agent a real GitHub issue
and repository snapshot, then evaluates its generated patch by applying it and
running the task's fail-to-pass and pass-to-pass tests. Other benchmarks cover
adjacent task designs:

- [Terminal-Bench 2.0](https://www.tbench.ai/benchmarks/terminal-bench-2)
  evaluates terminal agents on executable tasks across domains.
- [DDBench](https://arxiv.org/abs/2608.14863) evaluates historical
  distributed-system bug repair, including runs with bounded debugging context.
- [InfraBench](https://arxiv.org/abs/2608.11234) evaluates infrastructure agents
  across hardware, local-system, distributed-system, and application layers.
  Its lifecycle checks cover immediate behavior, live operation,
  restart/durability, and decommissioning. It combines executable preservation
  checks with a post-hoc LLM risk review, so its safety assessment is not wholly
  deterministic.
- [CI-Repair-Bench](https://arxiv.org/abs/2604.27148) standardizes each selected
  failing GitHub Actions workflow by removing unrelated workflows, redundant
  matrix dimensions, and non-validation steps while preserving validation
  commands and dependencies. It applies a candidate patch and re-executes that
  standardized workflow.
- [SWE-Review](https://arxiv.org/abs/2607.06065) starts from AI-generated
  candidate pull requests and evaluates both the review decision and whether a
  subsequent review-guided revision resolves the issue.
- [GitGoodBench: A Novel Benchmark for Evaluating Agentic Performance on
  Git](https://aclanthology.org/2025.realm-1.19/) covers merge-conflict
  resolution, interactive rebase, and iterative committing of changes.
- [BulkPR-Bench](https://arxiv.org/abs/2608.02685) evaluates queue-level
  governance of interacting pull requests.
- [UnderSpecBench](https://arxiv.org/abs/2607.02294) varies how clearly a DevOps
  instruction identifies the intended action, target, and blast radius, then
  checks wrong-target and over-scope effects. It does not model missing
  authority as an underspecification axis.

Tetrabench combines task designs not represented by these cited benchmarks. The
systems lane asks an agent to implement cross-boundary invariants in a compact
system rather than repair a known historical defect. The GitHub lane evaluates
a complete single-pull-request workflow against real local Git and a
deterministic task-local forge rather than an external API or a prose claim.

## Common task contract

### Reward

Each task has one strict primary reward:

```text
reward = integer 1 if and only if every mandatory gate passes; otherwise reward = integer 0
```

The primary value must be finite and exactly the integer `0` or `1`. The clean
verifier uses executable assertions only. It does not score architecture prose,
code style, explanations, or an LLM judgment. A task may retain extra numeric
diagnostics such as invariant, fault-schedule, or workflow-state pass counts in
Harbor's native `reward.json`, but no diagnostic can raise, average into, or
partially satisfy the primary reward.

Tetrabench admission must reject a task whose native primary reward has any
other value. Native per-task rewards remain authoritative. A section score is
the pass rate, equivalently the arithmetic mean of its binary task rewards.
This validation and summarization path is an admission prerequisite and remains
unimplemented.

### Deterministic fixture manifests

Every fixture must contain an agent-visible `contract.toml` that defines:

- schema version and task ID;
- the immutable initial snapshot digest;
- allowed interfaces and logical-clock semantics;
- named fault and checkpoint IDs;
- mandatory gate IDs and public case IDs.

The clean verifier owns hidden `tests/cases.toml`. It maps a fixed set of hidden
case IDs, seeds, and schedules to mandatory gates and expected state or effect
hashes. A prose term such as *current*, *harmful*, or *correct* has no scoring
authority unless the fixture maps it to a named case or gate. Manifest parsing,
referential integrity, fixed-case execution, and expected-hash comparison must
fail closed.

### Fixture and execution model

Systems tasks use compact dependency-free Python programs backed by SQLite
where persistence is required. Each task exposes enough incomplete behavior for
the agent to implement the required invariant, with no framework or service
dependency.

GitHub workflow tasks use real local `git` commands and three small newly
authored dependency-free repositories reused as immutable Git snapshots. A
minimal task-local forge sidecar owns pull request, check, review, release, and
merge-queue state. The agent can reach it only through its API or CLI. The forge
neither calls GitHub nor claims to reproduce GitHub's complete API.

Every fixture uses Harbor 0.22's `task.toml`, `tests/`, and optional `solution/`
layout with `verifier.environment_mode = "separate"`. Shared mode is forbidden.
The task declares all submitted agent output as artifacts beneath `/artifacts`. Harbor
collects declared artifacts after the agent phase, stops the main agent service,
then starts the separate verifier and re-materializes only collected artifacts
at their declared absolute paths. Harbor builds the verifier image from the
fixture's `tests/` directory, which bakes `/tests/test.sh` and hidden test code
into the image instead of uploading `tests/` at runtime. These are Harbor's native
[separate-verifier and artifact-transfer
semantics](https://www.harborframework.com/docs/tasks#verifier-environment-shared-vs-separate),
including its
[stop-main-before-sidecar-collection
order](https://www.harborframework.com/docs/run-jobs/results-and-artifacts#how-collection-works).

The agent phase uses public network access because the agent may need model APIs.
The separate verifier environment uses Harbor's `no-network` baseline; Harbor
rejects a trial when the selected environment cannot enforce the requested
[network policy](https://www.harborframework.com/docs/tasks/network-policy#capabilities).
Harbor's Docker egress control may still resolve DNS through its control path.
The verifier records that outcome and returned addresses without requiring DNS
failure. Direct-IP and hostname TCP connection attempts must fail; either
connection succeeding fails reward closed.
Evaluator correctness has no remote dependency. Admission must prove both phase
policies locally and in detached Modal.

Every task is CPU-only and uses one base image pinned by digest. The task config
caps each agent attempt at 35 minutes and the verifier at 4 minutes.
A normal hidden-suite run must finish in under 2 minutes.

Fixtures contain no remotes or future commits. Task authors create repository
snapshots and scenario code for this suite to reduce direct lookup shortcuts.
Public repository source, including candidate verifier source, remains useful
development evidence but is not contamination-resistant. Runtime hiding claims
only that Harbor bakes hidden tests into the separate verifier image and does
not mount them into the agent phase. Every task requires an exploit audit for hidden-test
discovery, verifier replacement, reward forgery, undeclared-artifact smuggling,
process persistence, and access outside the declared workspace or forge API.

### Submission and collection budgets

The existing controller collector admits at most 10,000 files, 64 MiB per file,
and 1 GiB total. Task admission imposes a lower budget on the declared workspace
submission and forge snapshot together: at most 2,000 files, 32 MiB per file,
and 256 MiB total. Verifier output is at most 32 MiB. Admission collects and
checks the full native job so that the agent artifacts, forge snapshot, verifier
output, and Harbor metadata remain within the existing hard collector limits.

Missing artifacts, collector warnings or failures, and any task-budget overrun
fail admission and reward closed. These limits bound admitted declared
artifacts. Harbor collection is best-effort, so they do not establish an
enforceable worst-case bound against arbitrary undeclared output produced inside
an agent environment.

### Pre-admission prerequisites

No fixture may enter a catalog or admission run until two native boundaries
pass version-matched prototypes both locally and in detached Modal. Local
candidate authoring may begin before those proofs:

1. Detached submission derives the complete selected fixture set from catalog
   entries, seals every file, and binds destination, mode, size, and digest into
   the request. It rejects symlinks and mutation with the existing context rules
   and proves local and detached materialization use the same bytes.
2. Separate-verifier handoff proves the lifecycle, network, task budget, clean
   verifier image, and declared `/artifacts` boundary above. The same prototype
   includes a forge sidecar and proves stop-main-first collection of its snapshot
   and event log before clean verification.

They gate catalog and admission authority, not local candidate work.

E-061 locally proves the first boundary. Detached request preparation
automatically seals the complete selected fixture set, and the
controller resolves each plan task only from the materialized context. The
tests cover deterministic ordering, every file identity input, multi-task
composition, explicit-context collisions, path and type rejection, limits, and
file and directory mutation races before provider construction. A materialized
fixture matches the stable checkout tree by relative path, bytes, and normalized
mode.

E-064 locally proves the second boundary with a distinct source-only prototype.
Pinned Harbor 0.22 runs a real Git worktree beside an API/CLI-only forge
sidecar. The final agent-side API call atomically appends the terminal event,
revokes its capability, and seals the database. Harbor then collects the main
worktree, stops `main`, and publishes the already-sealed forge export through
`[[verifier.collect]]` before running a no-network verifier
built from hidden `tests/` source. The verifier checks the native artifact
manifest, sidecar export manifest and hash chain, every event OID, Git fsck,
focused ancestry, a clean clone, product behavior, and forbidden agent files. It
alone writes exact binary reward bytes and diagnostics. Adversarial tests reject
agent-owned event files, post-seal API writes, recomputed snapshot tampering,
missing artifacts, and shared-environment verifier assumptions.

The Python 3.12 suite and every required real-Docker test pass.
Wheels contain neither catalog nor source-only fixture files; source
distributions retain the two prerequisite fixtures. The unadmitted
`authority-fencing` candidate, its hidden verifier, mutants, gold solution,
candidate-only admission tool, and candidate-only project test are absent from
both wheel and source distribution. Detached Modal runs of E-064 and automatic
catalog sealing remain unproven because the retained storage credentials are
expired. The catalogs stay empty until detached proof and the remaining
admission work pass.

## Systems design through implementation

Each systems task declares one bounded workspace artifact containing source and,
where the contract permits it, allowed durable state. The clean verifier never
trusts agent-produced results, logs, test reports, or expected hashes. It starts
fresh state from the immutable initial snapshot, imports only the declared
submission, and reruns the fixed hidden cases and fault schedules from
`tests/cases.toml`. The oracle computes database state, emitted effects, and
public command or API results itself rather than inspecting implementation
structure.

### `authority-fencing`

The system grants a worker a time-bounded lease and monotonic fencing token for
a resource. Work may finish after the lease expires, so every authoritative
write must compare the worker's token with the current token at commit time.

Every hidden fault schedule is a closed table of ID, public checkpoint, and
publicly declared expected exit code. Fault cases resolve that table by ID. One
verifier-owned function derives seeded identities, arguments, and order for the
independent semantic model and subprocess runner. Effect hashes bind the ordered
operation and argument trace.

Local admission separates stable canonical `subject` identity from
`run_attestation`. The subject binds task and verifier contexts, matrix contract,
source revision when available, base image, and tool versions. Each fresh
no-cache run attestation records its nonce, image ID, tag, repository digests,
and normalized build command. Fresh builds intentionally differ; neither whole
attestation bytes nor image IDs are claimed reproducible.

Admissible proof mode first requires a clean worktree and index, captures `HEAD`
once, and extracts one private `git archive`. That snapshot owns every task,
verifier, candidate, gold, temporary-project, source-manifest, lockfile, wheel,
and CLI input. The wheel is hashed and installed with its locked dependencies in
a private environment; production runs invoke only its absolute `tetrabench`
path. The task `tests/` manifest must equal the verifier build-context manifest.

The matrix may be followed by one to three ordered, unretried production runs.
Only exactly three requested runs can be admissible. One or two successful runs
are diagnostics, report `admissible=false` and `ok=false`, exit nonzero, and
cannot use `--output`.
Linux subreaper cleanup proves that no descendant survives before one bounded
no-follow snapshot captures the complete private native output for Harbor and
reward validation. Retained proof output uses a descriptor-anchored exclusive
write through protected root- or current-euid-owned ancestors. Ancestors must
not be group/world writable, except a root-owned sticky world-writable ancestor
such as `/tmp`. The final parent must be current-euid-owned and not group/world
writable, and its descriptor remains open through byte, metadata, visible-name,
parent-fsync, and final repeated checks. A missing or replaced name fails
without output success.
An equally authorized same-UID process can still mutate the file after the tool
returns; post-return immutability is outside this boundary. Dirty runs are
explicit debug evidence: they are non-admissible, exit nonzero, and cannot write
proof output. The clean local N=3 proof passed at source revision
`8fc7c78bc9c59da9b188ed9a9800425f08203d7a`: all 17 matrix entries passed,
followed by exactly three ordered, distinct, unretried production CLI runs with
raw integer reward `1`, pass rate `1/1`, separate verification, native
provenance, and no surviving child or current-run Docker residue.

The source-only calibration runner fixes the ordered profiles `target`
(`openai/openai/gpt-5.6-sol`) and `alternate`
(`openai/anthropic/claude-sonnet-5`) at two unretried attempts each. Immutable
profile records separately bind Harbor, child, broker, and upstream model
identities. OpenRouter is the default personal backend; LiteLLM is an explicit
optional work backend. Calibration runs the topology probe first, reads the
selected backend's authenticated pricing second, and starts attempts last.
OpenRouter `/models` pricing takes conservative maxima across threshold overrides
and every cache-write tier. LiteLLM retains `/model/info`. Both require finite
positive input, output, cache-read, and cache-write rates plus positive model
limits for exactly the selected models. Input and cache rates above `$10` per
million tokens or output rates above `$50` per million make calibration fail.
Nonzero request charges or unsupported paid modalities also fail. Evidence
retains a redacted normalized pricing snapshot, backend, source, and digest.

Each attempt receives one ephemeral OpenAI credential. Chat requests contain or
receive `n = 1`; every multiplicity alias and Responses background mode is
rejected. Message and input content admits only text, tool calls, and tool
results, never image/audio/file/remote-reference blocks. Ordinary URLs inside a
text string remain text. Its first valid request
locks that attempt to either `/v1/responses` or `/v1/chat/completions`; later
requests must use the same endpoint. The broker applies the endpoint's exact
output-limit fields and caps them at 8,192 tokens. It treats the forwarded UTF-8
body's byte length as the input-token upper bound because the supported
tokenizers fall back to at most one token per byte. Before forwarding, the
shared ledger atomically reserves that bound at the hard pricing ceilings plus
a fixed safety margin. LiteLLM preserves its response-cost settlement behavior.
OpenRouter successful nonstream and Responses-stream calls require terminal usage
cost plus a response ID, followed by a bounded authenticated historical
`/generation?id=...` cross-check for exact ID, model, stream shape, cost, and
normalized token counts before bytes reach the child. Generation 404 retries only
inside that settlement window and the attempt deadline. Streaming chat remains
unsupported. Any ambiguity retains the full reservation. Direct compatible
endpoints require explicit pricing and settlement adapters.

Each attempt uses one fresh labeled external Docker bridge network and one
credential-broker sidecar with a unique alias at port `62017`. No host port is
published. A disposable container on that network must complete a one-shot
ephemeral-token probe before model work; the OpenCode attempt receives a
different token. Failure stops before model forwarding and produces no
admissible evidence. The probe is valid only while `now < deadline`; equality is
expired. Expiry is checked under the token lock, rejects without consuming the
token, and permanently invalidates the broker. One attempt per profile is debug
mode only, while retained proof requires all four attempts from a clean committed
snapshot. Native ATIF aggregate prompt and completion tokens must both be
positive, cached tokens may
be zero, and all three must agree with Harbor's native trial and job metrics.
After the production CLI returns, command classification precedes the positive
broker-request gate. Every attempt retains only return code, stream sizes and
digests, safe canonical schema/outcome/reward fields, and bounded native
structural status/counts/digests/exception class names. A nonzero or malformed
CLI result cleans up and reads zero-request broker evidence without retaining
raw output, paths, exception messages, prompts, model content, logs, or tool
output.

The broker runs the source snapshot in a pinned Python image mounted read-only.
Its parent key arrives through anonymous stdin and remains process memory.
A broker-only private evidence mount receives a bounded redacted ledger; `main`
receives neither that mount nor the Docker socket. A temporary task overlay
changes only `environment/docker-compose.yaml`, binds `main` to the exact
external network, adds unique labels and an activation healthcheck, and remains
part of native task identity. The attempt token starts inactive. Harbor's native
`compose up --wait` cannot finish until the runner discovers the exact `main`,
rejects privileged or host-integrated immutable config, records its config
digest, rejects every exposed or published Docker port surface, and activates
through the private broker pipe. A post-activation half-second heartbeat leases
authority. Authorization checks that lease under the token lock, treats equality
as expired, revokes parent-key authority immediately, and signals listener
shutdown without waiting for watchdog polling. The broker is absent within five
seconds. Final cleanup reconciles exact names and labels and proves
authority and owned resources absent. Docker daemon/root is trusted. Network
peers and unrelated host containers are not containment evidence. Docker's
reported IPAM gateway and `Internal` value are recorded as topology only;
authenticated pricing and later forwarding prove actual egress. This transport
requires no host reachability or firewall change. No real calibration has
completed, so difficulty calibration remains unproven. The next clean proof
explicitly carries the retained `$0.96086` prior unknown exposure against the
shared `$25` cap without claiming it as actual spend or a completed attempt. A
separate direct OpenRouter contract probe cost `$0.00007`; it is not benchmark
calibration. `--debug-deny-upstream` is
a non-admissible one-attempt-per-profile diagnostic with no proof output. It
performs normal authenticated pricing, then accepts and reserves one valid broker
request, locks its endpoint, releases the reservation, and returns a fixed local
503 before opening a completion upstream connection. It proves OpenCode
installation, configuration, and endpoint routing at zero completion cost.
Attempt diagnostics retain ordered boolean status for every sidecar, topology,
CLI, main-admission, broker, heartbeat, ledger, native-validation, and cleanup
phase. A failed phase retains only its name and cause class. A CLI result or
exception available before main activation is captured before request-count
validation, without raw output, messages, paths, prompts, logs, or model content.

Mandatory gates:

1. A current unexpired holder can commit, and a non-holder cannot.
2. A new claim after expiry receives a token greater than every prior token.
3. The system rejects a delayed write from the expired holder after the new
   holder claims, even when the delayed worker began first.
4. Restart preserves the token sequence and active authority; a stale token
   captured before restart remains fenced afterward.
5. Concurrent claims produce one active holder and one authoritative token.
6. Renewing a lease does not reduce or transfer its authority, while release
   and subsequent claim fence the released holder.
7. Repeated clean-clone, restart, expiry-boundary, and delayed-write schedules
   leave the resource value attributable only to the latest valid holder.

### `atomic-outbox`

The service accepts an order, reserves inventory, and records one outbound
event. Those three database changes form one SQLite transaction. A separate
dispatcher sends committed events at least once and records acknowledgement.

Mandatory gates:

1. Successful order creation atomically inserts the order, decrements inventory
   once, and inserts one logically identified outbox event.
2. Failure after each statement but before commit leaves all three states
   unchanged.
3. Duplicate order submission cannot create another order, inventory decrement,
   or logical event.
4. The dispatcher never emits an event for rolled-back or uncommitted work.
5. A crash before send leaves the event pending. Restart eventually sends it.
6. A crash after send but before acknowledgement may resend the same logical
   event, but cannot invent a second event identity or lose the pending event.
7. Acknowledgement is durable and prevents further sends during normal restart.
8. Concurrent order attempts cannot drive inventory below zero or partially
   commit a losing order.

### `lifecycle-reconciliation`

The fixture manages resources by desired state and generation. Operations may
stop between external-effect recording and local-state updates, and cancellation
may race completion.

Mandatory gates:

1. Reconciliation discovers and adopts or removes an orphan according to the
   recorded desired state without creating a duplicate resource.
2. Repeating reconciliation from the same observed state is idempotent.
3. A crash at each create/update/delete checkpoint can be resumed from retained
   state to one legal converged result.
4. Concurrent cancellation and completion end in a legal terminal state with
   no active resource, duplicate finalization, or resurrection.
5. Delete carries the intended generation and cannot delete a replacement with
   the same logical name and a newer generation.
6. Stale completion from an older generation cannot overwrite newer desired or
   terminal state.
7. Clean-clone, restart, orphan, race, and delayed-stale schedules converge with
   no unowned external object.

### `online-migration`

The fixture migrates records from an old representation to a new representation
through expand, backfill, and contract while old and new application versions
coexist.

Mandatory gates:

1. Expand is backward compatible: old readers and writers continue to work
   before and during migration.
2. New writers preserve both representations while old readers remain admitted;
   new readers tolerate records not yet backfilled.
3. Backfill is idempotent, bounded in batches, and resumes from every injected
   interruption without skipping or duplicating a logical record.
4. Stale backfill data does not overwrite concurrent writes.
5. Validation proves equivalent logical record sets and values before contract.
6. Contract refuses to run while an old version is admitted or any record is
   unvalidated.
7. After contract, the new representation is authoritative and all records
   remain readable exactly once with no loss or duplicate logical identity.
8. Repeating every phase, including after restart, preserves the final result.

### `tenant-authorization`

The service stores tenant-scoped resources. The authenticated principal, its
role, and current revocation state are the only authority inputs; request bodies
and path parameters identify resources but cannot grant access.

Mandatory gates:

1. Every list, get, create, update, and delete path derives tenant scope from the
   principal and rejects a conflicting caller-supplied tenant.
2. Cross-tenant list and direct-ID access return no data.
3. Missing and unauthorized direct lookups have indistinguishable public status
   and body, including mutation targets, so the response does not reveal
   existence.
4. Every path enforces reader and writer roles; no alternate endpoint or bulk
   operation bypasses the role check.
5. Role changes take effect on the next operation without trusting a cached
   request claim.
6. Revocation blocks every subsequent read and mutation, including access
   through a previously obtained resource ID.
7. Failed authorization leaves database state and externally visible effects
   unchanged.
8. Parameterized tenants, roles, revoked principals, guessed IDs, and concurrent
   revocation schedules satisfy the same policy.

## GitHub workflow

The agent works in a real local Git repository and reaches the task-local forge
sidecar only through its API or CLI. The forge records transitions against Git
object IDs, but its mutable runtime files are never authoritative merely because
the agent produced or influenced them.

After the main service stops, `verifier.collect` snapshots the sidecar state and
append-only event log, while declared artifacts collect the workspace and Git
state. The clean verifier starts from the immutable initial repository and forge
snapshot, validates the event schema and every referenced Git object, and
reconstructs each claimed transition. It then reruns product and check behavior
from clean clones. The verifier ignores commit subjects and prose.

### `pr-submit`

The immutable initial snapshot contains the required base branch and no pull
request. The agent must create the head branch, commit the requested change, run
the required checks, and create the one permitted pull request through the forge.

Mandatory gates:

1. Work is committed on the required head branch from the declared base, with a
   clean worktree and no unrelated path changes.
2. The commit contains the requested executable change and passes repository
   checks.
3. The forge has exactly one open pull request with the correct base, head, and
   head commit.
4. Required checks are registered for that commit and pass; stale check results
   from another commit do not count.
5. The pull request is not merged, queued, or approved through an unauthorized
   transition.
6. No remote, hidden ref, or fixture state outside the allowed workflow was
   added or changed.

### `ci-repair`

The immutable initial snapshot contains an existing open pull request, its
failing head, and retained failing-check history. The agent must preserve that
history, repair the failure on a new head, and rerun the fixture-defined checks.

Mandatory gates:

1. The existing pull request and its original failing check history remain
   present.
2. The repair fixes the actual executable failure and passes all required
   repository checks on the new head commit.
3. The agent does not delete, disable, weaken, skip, or mark a required check as
   passing directly.
4. Forge state binds the new check run and passing result to the new head commit;
   stale success cannot satisfy it.
5. The diff contains no unrelated product or workflow changes.
6. A clean clone of the repaired head reproduces the passing checks.

### `review-adjudication`

The fixture manifest identifies two valid review requests and one invalid
request. The invalid request is a concrete contract violation: applying its
named change makes its mapped public or hidden case violate the expected
state/effect hash. The verifier scores the corresponding decline transition and
the absence of that change, not whether rejection prose sounds persuasive.

Mandatory gates:

1. The two designated valid threads each receive the required code correction,
   executable regression coverage, and a resolved forge state.
2. Each valid resolution points to a head commit containing its correction.
3. The invalid request receives the fixture-defined decline disposition and
   remains resolved or closed according to the fixture protocol.
4. The contract-violating change requested by the invalid thread is absent from
   the Git diff and runtime behavior.
5. No required thread remains pending, and no thread is deleted or rewritten.
6. All required checks pass on the final head and both valid regressions fail
   against the original reviewed head.

### `release-backport`

Mandatory gates:

1. The source fix is identified from the declared release history and remains
   unchanged on its original branch.
2. The maintenance branch starts from the required release line and receives
   the fix with Git ancestry or forge provenance that names the source commit.
3. Conflict resolution, when required, preserves maintenance-line behavior and
   introduces only the intended fix.
4. A backport pull request targets the correct maintenance branch and head.
5. Maintenance checks pass in a clean clone; main-branch-only assumptions or
   unrelated commits are absent.
6. Forge release state records the eligible backport without moving or
   recreating an existing release tag.

### `merge-queue-recovery`

Mandatory gates:

1. The original queued head, stale base, and invalidated check history remain
   visible in forge state.
2. The branch is updated onto the current base with the required conflict
   resolved and no loss of either compatible change.
3. Required checks rerun against the updated head and current base; results for
   the old head or base do not count.
4. The recovered pull request re-enters the queue only after the fresh checks
   pass and all queue prerequisites hold.
5. The forge merges in the prescribed order, and the resulting target branch
   Git tree equals the clean expected integration tree.
6. Failed or superseded queue attempts remain auditable, no competing pull
   request is silently dropped, and no force-written forge success bypasses the
   transition rules.

## Admission

Admission evaluates each task independently, so catalogs remain empty until the
corresponding task has the required retained evidence. Admission
uses the fixed hidden case set in `tests/cases.toml`: at most 16 cases and at
most 8 distinct fault schedules.

1. Contract and handoff: both manifests validate; every case, schedule, gate,
   object, and expected hash reference resolves; the selected fixture is sealed
   and request-bound; separate-verifier handoff, public agent network,
   no-network verification, sidecar collection, and declared artifact placement
   pass locally and in detached Modal.
2. Gold: the gold solution passes three consecutive local runs and two
   consecutive detached Modal runs with primary integer reward `1`, zero flakes,
   and zero retries.
3. No-op: one unchanged local run receives primary integer reward `0` and fails
   a gate specific to the requested behavior.
4. Seeded mutants: one seeded mutant targets each mandatory gate, up to eight
   mutants total. Each runs once locally, receives primary integer reward `0`,
   fails its intended gate, and passes every other mandatory gate. Admission
   records the full gate vector and rejects broad defects as unattributed.
5. State schedules: clean state, restart, and every fixed interruption, race,
   delay, or stale-state schedule in `tests/cases.toml` produce the expected
   state and effect hashes.
6. Exploit audit: one local and one detached run attempt the documented
   shortcuts, including hidden-test discovery, verifier replacement, reward
   forgery, undeclared artifact transfer, process persistence, workspace escape,
   and forge-log or Git-object fabrication. Neither run receives reward `1`.
7. Calibration: two named agent/model profiles are recorded at admission. Each
   receives two attempts of at most 35 minutes. Calibration measures task
   difficulty. It cannot override a deterministic gate or admit a task.
8. Reward and summary. Harbor's native result contains primary integer `0` or
   `1`; tetrabench rejects every other primary value and computes the section
   pass rate from native binary task rewards.
9. Resource bounds: declared workspace and forge artifacts satisfy the 2,000
   file, 32 MiB per-file, and 256 MiB total task budget; verifier output is at
   most 32 MiB; the complete native job remains within the existing 10,000 file,
   64 MiB per-file, and 1 GiB collector limits. A normal hidden suite finishes
   under 2 minutes and the verifier hard-stops at 4 minutes.
10. Budget: total model spend for admission is at most `$25` per task, and total
    admission wall time is at most 8 hours. A task that exceeds either limit
    stays out of the catalog.

## Implementation order

| Candidate | Status |
| --- | --- |
| `systems-design/authority-fencing` | Local candidate implemented under `benchmarks/tasks/`; intentionally unlisted and excluded from wheel/sdist. The clean local proof passed all 17 gold/no-op/mutant/exploit matrix entries and exactly three ordered unretried production CLI runs. Local gold repetition, no-op, mutant, and exploit gates are passed. Two detached repetitions, the detached audit, two-profile calibration, budget completion, and catalog admission remain `unproven`. |
| Remaining v1 candidates | Absent. |

1. Run the locally proven E-064 separate-verifier and forge-sidecar prototype in
   detached Modal, then run automatic selected-fixture sealing through live
   Modal. Renew scoped storage credentials first. Do not create a catalog task
   before these prerequisites pass.
2. Binary native-reward validation and section pass-rate summaries are locally
   complete. Prove them through detached Modal, then freeze the pinned agent and
   verifier images, manifest schemas, fault
   scheduler, exploit-audit checks, budgets, and admission evidence format.
3. Implement and admit systems tasks in this order: `authority-fencing`,
   `atomic-outbox`, `lifecycle-reconciliation`, `online-migration`, then
   `tenant-authorization`.
4. Author and freeze the three dependency-free Git repositories and the local
   forge state machine.
5. Implement and admit workflow tasks in this order: `pr-submit`, `ci-repair`,
   `review-adjudication`, `release-backport`, then `merge-queue-recovery`.
6. Add a task to `benchmarks/catalog.toml` only after its complete admission
   evidence passes locally and in detached Modal. Until then both task lists
   remain empty.
