# tetrabench

tetrabench is in early implementation. Its verified local surface includes
strict RFC 8785 records, immutable S3 transport, a fixed-key CAS admission
record, atomic submission receipts, a profile-specific Modal App builder, and a
decorator-independent controller runtime. A real Harbor 0.22 runner now compiles
strict plans through Harbor's supported configuration models and executes with
`Job.create` and `job.run`. Its integration fixture passes through attached local
Docker with the oracle agent and a verifier reward of `1.0`, with no model call.
The real Docker fixture also passes through `ControllerRuntime` with in-memory
S3 and Volume implementations, producing a validated immutable terminal after
secure artifact collection. The runtime locally proves admission, attempt
isolation, Volume boundaries, context verification, terminal-last publication,
no-follow bounded artifact collection, bounded failure evidence, and real
child-observer orchestration with fakes. Modal 1.5.4 App construction and the
installed distribution metadata are exercised against the real local SDK.
A Tigris Single-region `iad` bucket is the authoritative coordination baseline.
Retained E-046 evidence records it as private at cutover. Live evidence covers
location admission, immediate GET/HEAD/LIST,
conditional create and concurrent single-winner update, verified probe cleanup,
and a fresh-process detached Harbor oracle smoke with reward `1.0`. The retained
Global bucket is legacy immutable evidence only; tetrabench rejects it for
mutable admission coordination.
A deployed Modal controller has completed detached execution, cancellation, and
forced-interruption recovery smokes against a private Tigris prefix. The recovery
smoke terminated a running owner without publishing cancellation intent, observed
its stale Harbor child, and recovered from a fresh empty-state process. The
successor retained distinct old and new named-Volume attempts, published a
14-artifact terminal with reward `1.0`, and left no tagged child or controller
call running. The Single-region cutover smoke likewise published 14 artifacts
terminal-last, retained its named-Volume attempt, and left no active controller
or nested Harbor child. AWS and true provider preemption remain unproven. The
checked-in benchmark sections still contain no tasks, so `submit` refuses
them. A second source-only fixture proves Harbor 0.22 separate verification
locally: the final forge API call seals its database before the agent exits;
Harbor then collects the real Git worktree, stops `main`, exports the already-
sealed forge sidecar, and runs a no-network verifier with hidden source baked
from `tests/`. The verifier validates native artifact placement, forge hashes
and transitions, Git objects, a clean clone, and product behavior before writing
exact binary reward bytes. Its runtime evidence records whether DNS resolved and
the returned addresses; Harbor's Docker egress control may permit that lookup.
Direct-IP and hostname TCP connection attempts must both fail, and either
connection succeeding forces reward `0`. Detached Modal proof remains unproven
because the retained storage credentials are expired.
The unlisted candidate also has a source-only local calibration runner with two
fixed OpenCode model groups. Every attempt creates a fresh labeled Docker bridge
network and one broker sidecar with a unique alias. The broker image is pinned,
mounts the source snapshot read-only, and receives the parent gateway key through
anonymous stdin after start. The key is absent from Docker env, argv, labels,
mounts, config, and logs. A disposable container completes a one-shot probe. The
attempt token remains inactive until the runner discovers the uniquely labeled
Harbor `main`, validates its immutable Docker config, records one config digest,
and activates the token through that same private pipe. A `main` healthcheck
holds Harbor's native `compose up --wait` boundary until activation, before
OpenCode installation or execution. Authenticated `/model/info` pricing and
limits are retained only as an exact redacted canonical snapshot and digest.
It then starts each attempt with bounded failure evidence before model work.
Each attempt records ordered started/completed/failed booleans for sidecar start,
topology probe, CLI spawn, main discovery/config validation, broker activation,
heartbeat start, CLI wait, ledger read, native validation, and cleanup. A stage
failure retains only its phase and cause class. If the production CLI future
finishes before activation, the runner immediately retains any returned result:
return code, stream byte counts and SHA-256 digests, safe canonical
schema/outcome/reward fields, containment, and no-follow native structural
counts/digests. A future exception retains only its class and bounded native
structure. Positive request-count checks cannot mask this evidence. Raw streams,
paths, exception messages, prompts, model content, logs, and tool output are
excluded.
Each attempt locks to its first valid OpenCode endpoint only after successful
budget reservation. The broker caps output at 8,192 tokens and atomically
reserves each request's worst-case cost under a shared `$25` limit before
forwarding. Every upstream status requires one finite nonnegative authoritative
cost. Missing or malformed settlement makes the ledger fatal, retains the full
reservation as unknown exposure, and prevents later forwarding. Child responses
use only `text/event-stream` for a validated streaming request or
`application/json` otherwise; upstream content type, encoding, and other headers
are not reflected. Fake-upstream tests cover this boundary without a model call.
Chat requests are normalized to exactly one completion and both supported
endpoints reject multiplicity, background generation, non-text modalities,
remote media, and file references. Message/input content admits only text, tool
calls, and tool results. Endpoint-specific output limits remain capped at 8,192,
and every reservation covers that complete permitted output.

The clean task is copied into a temporary overlay whose only added or replaced
path is `environment/docker-compose.yaml`. It builds the committed main
Dockerfile and seed and attaches `main` to the exact external attempt network.
Candidate and overlay manifests are retained separately; Harbor's native task
digest includes the overlay. Inspection rejects host namespaces, privilege,
added capabilities, devices, weakened security options, runtime sockets, exposed
or published ports, and mounts outside Harbor's explicit log/artifact binds. The
Docker daemon and its root-equivalent operator remain trusted; unrelated host
containers are not security evidence. After activation, a host heartbeat updates
the anonymous pipe at least twice per second. Authorization treats the exact
two-second lease boundary as expired under the token lock, immediately revokes
tokens and parent-key authority, and signals listener shutdown. The `--rm` broker
is absent within five seconds. Forwarding first establishes TCP/TLS without
credentials. Under the authority lock, the broker registers that connected
socket, disables automatic reconnect, rechecks the active child bearer and
lease, and begins the parent-authorized request. Expiry closes registered
sockets. Requests whose credential send already began remain reserved in-flight
work; later handlers cannot open or reopen upstream transport or send
Authorization.
Cleanup reconciles exact random names and labels regardless of create-call
outcomes, reaps the attach client, and proves no active authority or owned
resource remains. A dead parent may leave an inert network; the next startup's
exact-label sweep removes it. No host port is published. The required clean
four-attempt report remains unproven. Debug mode also supports
`--debug-deny-upstream`. It requires one attempt per profile and forbids proof
output. After normal authenticated pricing, the broker accepts one valid request,
records its endpoint and reservation shape, releases the reservation, and
returns a fixed local 503 without opening a completion upstream connection. This
zero-cost route diagnostic is always non-admissible.

Catalog tasks now bind `reward_policy = "numeric" | "binary"` into resolved-plan
identity. Existing catalogs default to numeric, and retained plans without the
field preserve their old canonical identity. After Harbor validates native job
artifacts, tetrabench maps every attempt to one resolved task by its persisted
materialized task path and validates every primary and diagnostic reward as an
exact finite integer or float, never a boolean. Binary tasks require primary
integer `0` or `1` on every attempt; `reward.txt` produces a float and therefore
cannot satisfy that policy. Binary summaries independently require exact string
`"0"` or `"1"` trial values and derive every task and section count and pass rate
from those samples. Numeric summaries use canonical finite decimal strings. The
clean-verifier fixture now writes `reward.json`.

Detached submission now derives every regular file under each selected
catalog task directory and seals that complete fixture into the immutable
context before it constructs an S3 or Modal service. The source-only fixture
helper remains the only exercised live cloud submission path.

Explicit detached-controller recovery is locally and live verified. It refuses
running or inspection-unknown owners and cancellation admission states.
After Modal proves the old owner stopped, recovery CASes admission to
`recovering`, repeatedly sweeps stale children to quiescence, clears only the
current owner by returning to `prepared`, and may spawn a successor call. Rare
concurrent recoveries can spawn multiple calls after that handoff; only the fresh
`prepared→running` CAS winner may run Harbor. The complete revision history
retains old and new owners. Cleanup failure remains resumable without spawning.
Immutable terminal proof appearing at any boundary stops successor work but
still requires bounded child quiescence; recovery on an already terminal run is
cleanup-only and succeeds only after cleanup completes. The live forced-
interruption run exercised this path on Modal. Terminal owner proof gates
recovery. A bounded 30-second settling window succeeded in that smoke; the
window is empirical, not a safety proof. Repeated child sweeps establish
quiescence, and interrupted cleanup remains recoverable. The successor commits
the mounted Volume before attempt setup, preserving interrupted-owner files
without a reload that Modal rejects while old file descriptors remain open.
Child cleanup polls listed and persisted sandboxes so terminal handles are not
treated as running or terminated twice.

Python 3.12 is required. Package metadata rejects Python 3.13 and newer.

## Continuous integration

GitHub Actions runs on pushes to `master` and pull requests with read-only
repository access. The Python 3.12 job checks the lockfile, installs locked
dependencies, runs Ruff and ty, requires the Docker daemon and every required
marked Docker test exactly once, runs the remaining non-Docker suite, scans
production source and tools with
Bandit, builds both distributions, smoke-tests an isolated wheel installation,
and audits all locked dependency groups. A
separate job scans the full Git history with a digest-pinned Gitleaks image and
redacts findings.

S3 reads and publications use bounded visibility checks. They detect every
request, event-sequence, terminal, and dependency conflict visible during that
window, but a lagged conflicting record can make a later read conflict. Zero
visible terminals remains unknown or nonterminal.

One explicit exception to content-addressed publication is the mutable
coordination record at `runs/<run-id>/admission.json`. It is created with
`If-None-Match: *` and advanced one canonical revision at a time with
`If-Match: <etag>`. Its complete history retains one owner FunctionCall ID.
Immutable terminal objects remain terminal authority; admission never replaces
terminal proof.

## Reviewed design scope

[`benchmarks/README.md`](benchmarks/README.md) is the v1 task-family and admission
contract. It is not a substitute for the deterministic manifests that each
fixture must eventually own. Its two catalog task lists remain empty; fixture
work is blocked on detached separate-verifier and forge-sidecar proof plus native
binary-reward admission. The separate-verifier prototype and automatic
selected-fixture sealing are locally complete; their live Modal catalog path
remains unproven.

The accepted v1 design targets Harbor v0.22.0 evaluations through:

- a deployed Modal Function for detached cloud execution;
- Docker for explicit attached local development and execution;
- durable publication through standard AWS S3 endpoints or Tigris's fixed
  endpoint;
- Harbor-native ATIF, results, and job artifacts.

The design assigns active execution to Modal FunctionCall, in-progress cloud
files to a named Modal Volume, durable records to S3, and trace and result
meaning to Harbor job files. Local state is a submission receipt and cache, not
a run database.

These statements describe reviewed scope, not working software. The
implementation plan tracks deferred capabilities and the live-smoke evidence
still required before execution or provider behavior can be claimed.

## Install and inspect

```console
uv sync
uv run tetrabench --version
uv run tetrabench sections
uv run tetrabench plan systems-design
uv run tetrabench plan systems-design --json
uv run tetrabench run systems-design --profile local --output ./runs/systems-design
uv run tetrabench doctor
uv run tetrabench doctor --json
uv run tetrabench doctor --profile local --online
uv run tetrabench controller info
uv run tetrabench controller info --profile PROFILE --json
uv run tetrabench controller deploy --profile PROFILE
uv run tetrabench controller deploy --profile PROFILE --yes --json
uv run tetrabench submit systems-design
uv run tetrabench recover RUN_ID
uv run tetrabench recover RUN_ID --yes --json
uv run tetrabench status RUN_ID
uv run tetrabench status RUN_ID --json
uv run tetrabench cancel RUN_ID
uv run tetrabench cancel RUN_ID --yes --json
uv run tetrabench result RUN_ID --profile PROFILE
uv run tetrabench result RUN_ID --profile PROFILE --json
uv run tetrabench runs
uv run tetrabench runs --json
uv run tetrabench runs --remote --profile PROFILE
uv run tetrabench artifacts pull RUN_ID OUTPUT_DIR --profile PROFILE
```

Human output uses Rich. `--json` writes one RFC 8785 canonical JSON document,
followed by a newline, to stdout. Errors go to stderr; doctor errors are
canonical JSON on stderr when `--json` is set. Cloud commands reduce caught
Botocore and Modal provider exceptions to `provider_error` and `provider request
failed`; locally generated configuration and integrity errors retain their
specific messages.

`run` accepts only a profile that resolves to an explicit local controller and
Docker execution. It resolves the selected checked-in catalog tasks and runs
them attached through Harbor. Harbor 0.22's public `Task` model validates every
selected fixture before `--output` is created. `--output` must name a path that
does not exist; tetrabench creates it with mode `0700` and leaves the native
`harbor-job` directory there. Once that private reservation succeeds,
tetrabench never removes it on failure or interruption. Empty, partial, or
concurrently changed output remains owned evidence.
The final report contains Harbor's outcome, a canonical task/trial section
summary, and native job path. Numeric sections label their aggregate `Reward`;
binary sections label it `Pass rate` and show passed/sample counts. JSON includes
the complete ordered summary with exact decimal strings. Failed or cancelled
outcomes exit nonzero. Ctrl-C
exits 130 and retains the output directory as partial native evidence. The
checked-in catalogs are empty, so they continue to refuse execution until tasks
are deliberately added.

`doctor` validates the project config, catalog, selected profile's task
selection, section READMEs, and selected context files. This default mode is
offline: it does not construct a provider client, read credentials, or call
Modal or storage APIs. `doctor --online` additionally constructs the selected
storage provider client, calls `HeadBucket` and `GetBucketLocation`, and lists
at most one key under the configured prefix. It reports the bucket location and
whether mutable admission coordination is safe. It never calls a mutation API.

## Local detached control

Receipts live under the platformdirs `tetrabench` state directory. Each
canonical receipt appends physical spawn attempts and returned Modal call IDs
using atomic replacement plus receipt-root-parent, file, and receipt-root
`fsync`. Receipts are recovery caches, not a run database or owner record.

For a runnable Modal plan, submission anchors the project root before reading
the project configuration or catalog. It reads both through retained
root-relative no-follow descriptors, then traverses each `harbor_task` directory
from that same root authority. It seals every regular file at its
project-relative path and composes those files with explicit context whose
destinations do not overlap. File content,
normalized execution mode, size, digest, and destination bind the plan and
request. Discovery uses incremental descriptor-based `scandir`, retaining at
most 10,000 discovered entries and 10,000 directories at depth 64 by default;
configuration may lower those bounds but cannot set the entry bound below the
file bound. Every opened fixture descriptor must remain on the anchored Linux
mount and device, and regular files must have one link. A second complete
descriptor-anchored traversal compares names, types, identity, mode, mount,
size, and every file digest against the staged bytes. Missing or replaced
directories, links, special files, mutation, unavailable mount evidence,
portable path ambiguity, collisions, and context limits stop submission before
it constructs an S3 or Modal service. Preparation also resolves the exact Modal
App, Function, and environment into its in-memory result. Provider construction
uses only that result and immutable resolved storage; it does not reread project
configuration or the catalog. This launch selector is absent from plans,
requests, receipts, and other durable records. Submission then uploads the
staged context,
publishes the immutable request, creates or observes the prepared admission,
calls the deployed `Function.from_name(...).spawn()`, and persists its
FunctionCall ID as local evidence. The controller resolves task paths only
beneath its materialized context; it never falls back to the submitter's
checkout. The CLI never claims admission ownership.
Every spawned controller must CAS prepared to running with its actual call ID;
before that CAS it must validate the full run/request/plan invocation against
the immutable request and admission. Only the winner may enter Harbor. `recover`
is an explicit operator action. It asks for confirmation before cloud mutation;
`--yes` skips the prompt, and JSON recovery requires `--yes`. A prepared orphan
spawns directly. An owned running or failed admission first requires terminal
Modal call inspection, advances to `recovering`, and reaches `prepared` only
after two consecutive child sweeps are empty. A cleanup error leaves recovery
resumable without spawning. Concurrent callers may spawn more than one
FunctionCall while admission is prepared, but only the fresh
`prepared→running` CAS claimant may run Harbor; losing calls exit before work.
A terminal observed before claim also makes the call exit before work. A new
owner cannot claim `recovering`. A new physical attempt with the pre-existing
owner ID exits before Harbor; explicit recovery must prove that owner terminal
and clean its children before preparing a successor. Actual Modal preemption
behavior remains unproven.

Recovery receipts append `recovery-intended` before spawn and `spawn-returned`
after Modal returns the call ID. Missing or corrupt receipts do not grant or
remove authority. `status` reports `recovering` as attention and tells the
operator to rerun recovery if cleanup stopped. S3 conflicts dominate, and an
immutable terminal object dominates admission, receipts, and Modal output.

Cancellation uses admission CAS and needs no event-write permission. Prepared
runs advance directly to cancelled. Running runs advance to cancelling while
preserving the owner call ID; the service then cancels and polls that call,
sweeps children, and advances to cancelled only after the call is terminal and
two consecutive sweeps are empty. The profile-scoped observer combines child
IDs from immutable attempt events with run-tagged `Sandbox.list` results under
Harbor's `__harbor__` App, terminates with wait, and repeats bounded sweeps.
The controller preserves a cancelling admission instead of replacing it with
failed when provider shutdown interrupts its exception path. Cancellation polls
the owner for up to ten seconds and can be resumed safely if provider shutdown
takes longer.
`cancel` asks for confirmation before constructing an S3 or Modal client.
`--yes` skips the prompt. `cancel --json` requires `--yes` and otherwise exits
without provider construction or mutation.
Modal API, authentication, and other inspection failures do not prove that the
owner call stopped. Terminal proof can be published only by the exact owner
while admission is running or cancelling, after revalidating the immutable
request and all run/request/plan bindings.

## Remote results and artifacts

`result` reads the selected AWS or Tigris profile directly. It requires no
local receipt and constructs no Modal client. The report distinguishes
`unknown`, `nonterminal`, `terminal`, and `conflict`, validates terminal,
admission, request, plan, storage, and controller-summary bindings, and shows
outcome, the canonical section summary, and the complete terminal
inventory. Unknown exits 4, conflict exits 3, and failed or cancelled authority
exits 1. Successful and still-running states exit 0.

New controller-result schema v2 binds run, request, plan, attempt, outcome, and
summary. Remote reads reject malformed identity or arithmetic before reporting
success. Retained schema-v1 controller results are accepted only with plans that
predate reward policies; those use the native numeric fallback and report the
structured summary as unavailable and legacy. New binary plans never fall back.

`runs` retains local receipt listing by default. `runs --remote --profile
PROFILE` paginates the configured `runs/` prefix, derives unique run IDs only
from valid admission, request, event, and terminal keys, then reads each run
through the same authoritative result path. Malformed keys are reported in
sorted order and make the command exit 3. There is no remote index or database.

`artifacts pull` accepts one successful bound terminal. Failed and cancelled
terminals remain inspectable through `result` but are not materialized. Pull
validates the whole logical inventory and the shared controller/receiver limits
before reserving an absent destination. The defaults admit at most 10,000 files,
64 MiB per file, and 1 GiB total. Each content-addressed object streams directly
from S3 to its exclusive destination descriptor while tetrabench hashes and
counts it; pull has no whole-object buffering path. Digest and size must verify
before the file is fsynced and successfully closed. Every acquired S3 response
body is closed, including when response metadata or streamed bytes are rejected.
The destination and each nested directory are created at exact `0700`; files
are exclusively created and immediately set to exact `0600`. Finalization
reapplies `0600` to every file and `0700` to every directory through held or
no-follow descriptors, verifies exact type, identity, and mode with `fstat`, then
fsyncs and closes. Materialization uses no-follow, exclusive,
directory-FD-relative operations rooted in the opened destination, so pathname
replacement or a symlink cannot redirect writes outside that tree. Traversal,
duplicate or prefix conflicts, corruption, and detected replacement or injected
content abort the pull. Partial evidence receives the same mode restoration and
fsync on a best-effort basis without masking the original failure. The reserved
directory is retained on every failure. A process with the same UID remains
authoritative over these files and can mutate them during or after the pull;
tetrabench does not claim isolation from that actor or post-return immutability.
Remote result, listing, and pull paths never call provider delete.

## Verified local Harbor fixture

The integration-only fixture runs attached through local Docker and returns the
native Harbor job directory and bindings:

```console
uv run python - <<'PY'
from pathlib import Path
from tetrabench.integration import run_local_composition

result = run_local_composition(
    Path("tests/fixtures/harbor_task"),
    Path("/tmp/tetrabench-harbor-fixture"),
)
print(result.terminal.outcome, result.controller.terminal_sha256)
print(result.invocation_root / "jobs/harbor-job")
PY
```

The native directory is retained unchanged. Tetrabench validates job and
per-trial config, lock, and result files with Harbor 0.22's Pydantic models.
Each persisted trial task path must still load as a real Harbor task directory,
and its task-level artifacts compose before trial-level artifacts. Native
artifact manifests require strict JSON and the exact bytes written by Harbor
0.22's indented serializer; tetrabench does not reinterpret them as RFC 8785
records.
Persisted files own outcomes, rewards, exceptions, evidence, and artifact
bindings; the value returned by `job.run` is checked only for agreement. ATIF
is discovered from the securely collected inventory at normal and multi-step
agent paths, following continuation references. The oracle fixture does not
emit ATIF, so the terminal records that absence instead of creating a trace.

`tests/fixtures/harbor_authority_task` is a separate source-only authority
prototype. It is not cataloged or installed in wheels. Its Docker composition
contains `main` and a forge sidecar with no shared forge volume. The Oracle makes
one real commit and submits one transition through the documented `forge` CLI.
The final forge API transition validates complete state, appends its terminal
hash-chained event, revokes the run capability, and seals the database in one
immediate transaction before the agent exits. Harbor's sidecar collect hook only
publishes an already-sealed export. Database finalization and fail-closed atomic
file publication are separate operations. The clean verifier
recomputes authority from the collected repository and sealed export; agent
logs, local result files, and claimed hashes never determine reward.

The controller constructs its S3 client before removing every environment key in
the case-insensitive `AWS_` and `TIGRIS_` namespaces, plus Botocore's
non-prefixed `BOTO_CONFIG` and `BOTOCORE_TCP_KEEPALIVE` selectors, for the
entire Harbor run. This includes `AWS_ACCOUNT_ID`. Attached local execution
applies the same boundary while
Harbor validates and runs. Harbor child configuration receives only an
invocation-scoped registry key; the trusted controller process publishes
lifecycle events through the already-created store. The Docker fixture asks
Harbor to interpolate uppercase, lowercase, mixed-case, and alternate namespace
members and verifies that the child receives only its explicit unavailable
defaults while terminal publication still succeeds. Arbitrary nonstandard
provider variables outside these namespaces and reviewed Botocore names are not
removed.

## Controller deployment

`controller info` is local and read-only. It prints the exact App, Function,
Modal environment, Volume, Secret, timeout, and fixed controller-root names for
the selected profile. `controller deploy` prints the same contract and asks for
confirmation before calling Modal; `--yes` skips the prompt, and JSON deployment
requires `--yes`. Secret values are never read or printed.

The image copies the local project into a build layer and installs it with pip
as the `tetrabench` distribution, including its metadata and exact Harbor and
Modal pins. Deployment creates or resolves the profile-specific Modal
Environment before deploying. The serialized Function has zero retries, a
24-hour timeout, the named Volume at `/tetrabench/controller`, and the named S3
credential Secret. Submission invokes it through
`Function.from_name(...).spawn()` with canonical invocation bytes and their
digest. A paid smoke has verified package/API behavior, named-Volume retention,
nested Harbor Modal execution, terminal publication, and post-run child cleanup.
Artifact publication accepts only regular files reached through no-follow
directory descriptors beneath the attempt root; links, special files, mutation,
and escaped bindings fail closed.

## Project configuration

The checked-in [`tetrabench.toml`](tetrabench.toml) is a working example. It
selects the default Modal controller and Modal execution, points to the local
catalog, and contains only a storage bucket placeholder and a Modal Secret name
reference. Secret values are not valid configuration fields.

AWS storage requires an explicit region and always uses standard AWS endpoints;
`endpoint_url` is rejected:

```toml
[storage]
provider = "aws"
bucket = "private-benchmark-artifacts"
region = "us-west-2"
```

Tigris fixes its endpoint to `https://t3.storage.dev` and its region to `auto`:

```toml
[storage]
provider = "tigris"
bucket = "private-benchmark-artifacts"
```

Before admission create/update and before a higher-level `submit`, `recover`,
`cancel`, or controller mutation sequence can publish a new run object,
tetrabench reads the bucket location. AWS accepts the documented null
`us-east-1` response, legacy `EU`, and regions known to the pinned SDK; an empty
string is invalid. Tigris Single-region (`iad`, for example) and Multi-region
(`usa` or `eur`) buckets are accepted. Tigris Global and Dual-region buckets are
rejected for mutable coordination because their cross-region consistency is
eventual. Missing or unknown locations also fail closed.

The gate does not prohibit generic immutable content publication. A retained
Global Tigris bucket may continue storing and serving content-addressed objects
and legacy run evidence under bounded eventual-read semantics. Status and
result reads remain available there. Moving mutable coordination to a safe
topology requires provisioning a new Single-region bucket, copying retained
objects as needed, and cutting clients over. It is not an in-place bucket
location migration.

The retained Global baseline was inspected live and is correctly classified as
unsafe for admission mutation. A separate private Single-region `iad` bucket is
now authoritative for coordination; no legacy object was copied or changed.
The source-only probe passed there with immediate GET, HEAD, and LIST after
conditional create, a synchronized two-client one-winner update race, and
verified HEAD/LIST absence after deletion. It performs those mutations only when
explicitly enabled:

```console
uv run python tools/provider_consistency_probe.py \
  --provider tigris --bucket BUCKET --allow-mutation
```

### Storage roles and actions

The local contract and E-046 retained policy evidence define this bounded
matrix. Current live enforcement remains `unproven` until the future probes
below run with renewed credentials.

| Role | Bucket actions | Object reads | Object writes | Delete |
| --- | --- | --- | --- | --- |
| Submitter | `s3:GetBucketLocation`, `s3:ListBucket` | Configured prefix, for conflict and status reads | Context objects, requests, and admission beneath the configured prefix | None |
| Controller | `s3:GetBucketLocation`, `s3:ListBucket` | Configured prefix, for request, reconciliation, and cleanup reads | Admission, events, artifacts, and terminals beneath the configured prefix | None |
| Harbor child (agent and verifier) | None | None | None | None |

All durable key constructors validate the configured prefix, run and attempt
IDs, digests, and logical paths before a provider call. Normal runtime clients
have no delete method in their used provider interface; only the opt-in
consistency probe calls `DeleteObject`.

Tigris currently accepts but does not enforce an `s3:prefix` condition on
`ListBucket`. Submitter and controller listing is therefore bucket-wide even
when object read and write actions are prefix-scoped. Use a dedicated bucket
when exposing bucket key names to either role is unacceptable.

The opt-in consistency probe is separate operational evidence. Use a temporary,
probe-prefix-scoped credential with `s3:GetBucketLocation`, `s3:ListBucket`,
`s3:PutObject`, `s3:GetObject`, and `s3:DeleteObject`, then remove the credential
and policy. No live IAM resource is changed by tetrabench's tests or
configuration.

The next live IAM probe must use fresh submitter, controller, and deliberately
unprivileged child credentials. For each identity it must exercise every allowed
action above, a cross-prefix `GetObject` and `PutObject`, and `DeleteObject`; the
expected denials and allowed calls must be retained with credential material
redacted. The privacy and encryption probe must record `GetBucketPolicy`,
`GetBucketAcl`, `GetPublicAccessBlock`, and `GetBucketEncryption` where the
provider supports them, then write and `HeadObject` one probe object to inspect
its effective encryption metadata before verified cleanup. Unsupported provider
inspection APIs remain `unproven`; a default or documentation claim is not
evidence of the live bucket state.

Docker planning requires both explicit local controller and Docker execution:

```toml
[controller]
kind = "local"

[execution]
kind = "docker"

[harbor]
agent_name = "oracle"
attempts = 1
concurrency = 1
```

Plans admit at most 256 tasks, 32 attempts per task, and concurrency 64.
Context fixture discovery defaults to 10,000 total entries, 10,000 directories
(including selected roots), and depth 64. These are fail-closed Linux limits;
filesystems without `/proc` fd mount evidence are unsupported.
Controller artifact collection defaults to at most 10,000 regular files, 64
MiB per file, and 1 GiB total. It preflights these limits before publishing any
artifact from an attempt.

Fixture sealing detects mutation across two complete reads under a trusted
same-UID checkout; it does not claim an atomic single-instant filesystem
snapshot. A malicious same-UID process that changes and restores state between
observations is outside this boundary. Inode identity detects replacement, while
the second digest comparison owns file-byte equality; inode reuse is not treated
as cryptographic proof.

`harbor.agent_name` and optional `harbor.model_name` pass through to Harbor as
opaque strings. Tetrabench does not interpret or validate them and does not
guarantee that Harbor, an agent, or a model provider will accept them.

## User profiles

An optional `config.toml` under the platformdirs `tetrabench` user-config
directory may define profiles. This safe example contains no credentials:

```toml
schema_version = 1

[profiles.local.controller]
kind = "local"

[profiles.local.execution]
kind = "docker"

[profiles.local.storage]
provider = "aws"
bucket = "private-benchmark-artifacts"
region = "us-west-2"
```

Select it with `tetrabench plan systems-design --profile local` or
`tetrabench doctor --profile local`. The selected profile is applied over the
checked-in project config. Supported fields merge explicitly by precedence;
changing a `kind` or `provider` starts that variant from its own defaults.
Unknown keys and arbitrary recursive dictionary merges are rejected.

## Project records

- [AGENTS.md](AGENTS.md) defines the repository workflow and documentation
  rules.
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) is the canonical scope,
  decision, progress, blocker, and evidence record.
- [NOTES.md](NOTES.md) is the append-only research and clarification log.

The notes retain the provenance behind durable plan decisions. This README
contains only verified user-facing status and behavior.
