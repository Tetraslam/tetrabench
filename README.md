# tetrabench

tetrabench is in early implementation. Its verified surface is an importable
Python package with bounded, strict RFC 8785 canonical JSON and SHA-256 helpers,
typed local configuration, catalog and context resolution, and a read-only
planning CLI. The package also has a verified immutable S3 transport for
content, request, event, and terminal records; it has not yet passed live AWS
or Tigris smokes. The checked-in benchmark sections currently contain no tasks,
so their plans contain zero trials and are reported as not runnable. CLI
submission and execution are not implemented. `doctor --online` can check
read-only access to configured AWS or Tigris storage, but live provider
behavior has not been accepted as proven.

Python 3.12 or newer is required.

S3 reads and publications use bounded visibility checks. They detect every
request, event-sequence, terminal, and dependency conflict visible during that
window, but a lagged conflicting record can make a later read conflict. Zero
visible terminals remains unknown or nonterminal.

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
