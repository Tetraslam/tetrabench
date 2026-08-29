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
A new private Tigris Single-region `iad` bucket is the authoritative coordination
baseline. Live evidence covers location admission, immediate GET/HEAD/LIST,
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
checked-in benchmark sections still contain no tasks, so `submit` refuses them; the
source-only fixture helper is the only exercised cloud submission path.

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

Python 3.12 or newer is required.

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
uv run tetrabench runs
uv run tetrabench runs --json
```

Human output uses Rich. `--json` writes one RFC 8785 canonical JSON document,
followed by a newline, to stdout. Errors go to stderr; doctor errors are
canonical JSON on stderr when `--json` is set.

`run` accepts only a profile that resolves to an explicit local controller and
Docker execution. It resolves the selected checked-in catalog tasks and runs
them attached through Harbor. Harbor 0.22's public `Task` model validates every
selected fixture before `--output` is created. `--output` must name a path that
does not exist; tetrabench creates it with mode `0700` and leaves the native
`harbor-job` directory there. Once that private reservation succeeds,
tetrabench never removes it on failure or interruption. Empty, partial, or
concurrently changed output remains owned evidence.
The final report contains Harbor's outcome, standard mean `reward` as a decimal
string, and native job path. Failed or cancelled outcomes exit nonzero. Ctrl-C
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

For a runnable Modal plan, submission seals and uploads selected context,
publishes the immutable request, creates or observes the prepared admission,
then calls the deployed `Function.from_name(...).spawn()` and persists its
FunctionCall ID as local evidence. The CLI never claims admission ownership.
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
Modal API, authentication, and other inspection failures do not prove that the
owner call stopped. Terminal proof can be published only by the exact owner
while admission is running or cancelling, after revalidating the immutable
request and all run/request/plan bindings.

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
Persisted files own outcomes, rewards, exceptions, evidence, and artifact
bindings; the value returned by `job.run` is checked only for agreement. ATIF
is discovered from the securely collected inventory at normal and multi-step
agent paths, following continuation references. The oracle fixture does not
emit ATIF, so the terminal records that absence instead of creating a trace.

The controller constructs its S3 client before removing AWS and Tigris
credential, profile, and credential-file environment variables for the entire
Harbor run. Harbor child configuration receives only an invocation-scoped
registry key; the trusted controller process publishes lifecycle events through
the already-created store. The Docker fixture asks Harbor to interpolate the
standard AWS variables and verifies that the child receives only its explicit
unavailable defaults while terminal publication still succeeds.

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

### Storage IAM requirements

Both normal v1 identities need `s3:GetBucketLocation` on the bucket so every
admission path can fail closed before mutation. The submitter also needs the
existing bucket-list/object-read permissions used by status and conflict checks,
plus `s3:PutObject` for context, request, and admission keys. The controller
needs the existing bucket-list/object-read permissions plus `s3:PutObject` for
admission, event, artifact, and terminal keys. Neither normal identity receives
`s3:DeleteObject`.

The opt-in consistency probe is separate operational evidence. Use a temporary,
probe-prefix-scoped credential with `s3:GetBucketLocation`, `s3:ListBucket`,
`s3:PutObject`, `s3:GetObject`, and `s3:DeleteObject`, then remove the credential
and policy. Tigris currently accepts but does not enforce an `s3:prefix`
condition on `ListBucket`, so listing is bucket-wide; object get/put/delete
permissions remain prefix-scoped. No live IAM resource is changed by
tetrabench's tests or configuration.

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
Controller artifact collection defaults to at most 10,000 regular files, 64
MiB per file, and 1 GiB total. It preflights these limits before publishing any
artifact from an attempt.

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
