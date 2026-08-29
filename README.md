# tetrabench

tetrabench is in early implementation. Its verified local surface includes
strict RFC 8785 records, immutable S3 transport, a fixed-key CAS admission
record, atomic submission receipts, a detached Modal client adapter, and local
submission, status, and cancellation services. Tigris conditional create,
stale-ETag rejection, update, and concurrent single-winner behavior have been
proven live on a private copy-on-write fork. It has not deployed a controller or
run Harbor, AWS, or Modal smokes. The checked-in benchmark sections contain no tasks, so
`submit` refuses them before constructing S3 or Modal clients. `cancel` also
refuses a running admission before mutation until the real Harbor child
observer is installed; it can CAS a prepared admission directly to cancelled.

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
uv run tetrabench doctor
uv run tetrabench doctor --json
uv run tetrabench doctor --profile local --online
uv run tetrabench submit systems-design
uv run tetrabench recover systems-design --run-id RUN_ID
uv run tetrabench status RUN_ID
uv run tetrabench status RUN_ID --json
uv run tetrabench cancel RUN_ID
uv run tetrabench runs
uv run tetrabench runs --json
```

Human output uses Rich. `--json` writes one RFC 8785 canonical JSON document,
followed by a newline, to stdout. Errors go to stderr; doctor errors are
canonical JSON on stderr when `--json` is set.

`doctor` validates the project config, catalog, selected profile's task
selection, section READMEs, and selected context files. This default mode is
offline: it does not construct a provider client, read credentials, or call
Modal or storage APIs. `doctor --online` additionally constructs the selected
storage provider client, calls `HeadBucket`, and lists at most one key under
the configured prefix. It never calls a mutation API. A successful check proves
only that those reads worked at that time; writes remain `unproven` and are not
attempted.

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
is an explicit operator action and spawns only while admission is still
prepared. A missing or corrupt receipt is a status warning. S3 conflicts
dominate, and an immutable terminal object dominates admission, receipt, and
Modal output.

Cancellation uses admission CAS and needs no event-write permission. Prepared
runs advance directly to cancelled. Running runs advance to cancelling while
preserving the owner call ID; the service then cancels and polls that call,
sweeps children, and advances to cancelled only after the call is terminal and
two consecutive sweeps are empty. The deployed child observer is absent, so
the installed command refuses running cancellation before changing admission.
Modal API, authentication, and other inspection failures do not prove that the
owner call stopped. Terminal proof can be published only by the exact owner
while admission is running or cancelling, after revalidating the immutable
request and all run/request/plan bindings.
The deployed controller, Volume handling, Harbor execution, real child observer,
and live cleanup remain unimplemented.

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

Docker planning requires both explicit local controller and Docker execution:

```toml
[controller]
kind = "local"

[execution]
kind = "docker"
```

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
