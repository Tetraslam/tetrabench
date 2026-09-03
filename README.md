# tetrabench

Run Harbor evaluations locally, then move the same sealed task set to detached
Modal execution with durable S3 or Tigris results.

[![CI](https://github.com/Tetraslam/tetrabench/actions/workflows/ci.yml/badge.svg)](https://github.com/Tetraslam/tetrabench/actions/workflows/ci.yml)

Tetrabench gives eval authors one CLI for project setup, task validation, local
Docker runs, remote submission, recovery, and artifact retrieval. It keeps
Harbor's native task, trial, verifier, and trajectory outputs intact rather than
inventing a second evaluation format.

## Quick start

You need Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and a running
Docker daemon.

```console
git clone https://github.com/Tetraslam/tetrabench.git
cd tetrabench
uv tool install --python 3.12 .

tetrabench init ../my-evals
cd ../my-evals
tetrabench doctor
tetrabench task validate benchmarks/tasks/systems-design/hello-tetrabench
tetrabench plan systems-design
tetrabench run systems-design --output ./runs/hello
```

The generated starter runs through Harbor with its Oracle solution and a
separate no-network verifier. A successful run ends with:

```text
Outcome: succeeded
Pass rate: 1 (1/1)
```

`init` creates a standalone project:

```text
my-evals/
├── tetrabench.toml
└── benchmarks/
    ├── catalog.toml
    ├── systems-design/README.md
    ├── github-workflow/README.md
    └── tasks/systems-design/hello-tetrabench/
        ├── instruction.md
        ├── task.toml
        ├── environment/Dockerfile
        ├── solution/solve.sh
        ├── tests/Dockerfile
        └── tests/test.sh
```

## Author an eval

Create an unlisted task, edit its instruction, environment, solution, and
verifier, then validate and add it to your project catalog:

```console
tetrabench task new systems-design lease-fencing

$EDITOR benchmarks/tasks/systems-design/lease-fencing/instruction.md
$EDITOR benchmarks/tasks/systems-design/lease-fencing/tests/test.sh

tetrabench task validate benchmarks/tasks/systems-design/lease-fencing
tetrabench task add systems-design lease-fencing \
  benchmarks/tasks/systems-design/lease-fencing
tetrabench run systems-design --output ./runs/lease-fencing
```

`task validate` is read-only. It seals the complete fixture tree under bounded
path, file, and byte limits, validates a private copy through Harbor 0.22, then
checks that the source did not change. It does not call Docker, Modal, or a
storage provider.

`task add` is an explicit mutation for user-owned catalogs. It validates twice,
rejects duplicate IDs and fixture paths, preserves existing catalog bytes, and
atomically appends a binary task entry. Tetrabench writers serialize through a
sibling lock file; arbitrary programs editing the catalog concurrently are
outside that cooperative lock.

The generated task is deliberately small. Replace its exact-answer verifier
with assertions for your domain. A verifier writes Harbor's native
`/logs/verifier/reward.json`; binary tasks must produce exactly integer `0` or
`1`. See [benchmark authoring and admission](benchmarks/README.md) for the
stricter rules used by tetrabench's own benchmark catalog.

## Commands

| Command | Purpose | Side effects |
| --- | --- | --- |
| `init` | Create a runnable local project | New directory |
| `task new` | Create an unlisted Harbor task | New task directory |
| `task validate` | Seal and validate one fixture | None |
| `task add` | Add a validated task to the project catalog | Atomic catalog update |
| `doctor` | Validate config, catalog, context, and optional storage reads | None |
| `plan` | Resolve a canonical secret-free execution plan | None |
| `run` | Run selected tasks through local Docker | New private output directory |
| `controller deploy` | Deploy the configured Modal controller | Cloud mutation, confirmation required |
| `submit` | Publish a request and spawn detached execution | Cloud mutation |
| `status` | Combine durable and provider execution evidence | Provider reads |
| `result` | Read authoritative remote state without a local receipt | Storage reads |
| `cancel` | Record cancellation intent and clean owned children | Cloud mutation, confirmation required |
| `recover` | Clean a stopped owner and prepare a successor | Cloud mutation, confirmation required |
| `artifacts pull` | Materialize one successful terminal inventory | New private output directory |

Add `--json` for canonical machine-readable output. The `--json` forms of
`controller deploy`, `cancel`, and `recover` require `--yes`.

## Project configuration

The starter uses local Docker without a user profile:

```toml
schema_version = 1
catalog_path = "benchmarks/catalog.toml"

[controller]
kind = "local"

[execution]
kind = "docker"

[harbor]
agent_name = "oracle"
attempts = 1
concurrency = 1
```

Set `harbor.agent_name = "opencode"` and a Harbor-compatible `model_name` to run
an agent instead of the checked-in Oracle solution. Tetrabench passes these
identifiers to Harbor unchanged.

User-specific overrides live at `~/.config/tetrabench/config.toml` on Linux.
They can select models and credentials without committing personal settings to
an eval repository. Configuration is strict: unknown fields, malformed paths,
and invalid controller/execution combinations fail before provider work.

## Detached Modal runs

Keep the project local by default and add a user profile for cloud execution:

```toml
schema_version = 1

[profiles.cloud.controller]
kind = "modal"
app_name = "tetrabench"
function_name = "controller"
secret_name = "tetrabench-controller"

[profiles.cloud.execution]
kind = "modal"

[profiles.cloud.storage]
provider = "tigris"
bucket = "your-private-bucket"
region = "auto"
prefix = "tetrabench"
```

The local submitter uses boto3's standard credential chain. The configured Modal
Secret must expose the corresponding `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` to the controller. Harbor children receive neither the
controller's storage credentials nor its publication authority.

Tigris uses `https://t3.storage.dev`. Mutable coordination accepts known
Single-region buckets and Multi-region `usa` or `eur` buckets. Tetrabench keeps
Global and Dual-region buckets readable for legacy results, but rejects them
before new run mutation because their cross-region consistency is insufficient
for admission compare-and-swap.

```console
tetrabench doctor --profile cloud --online
tetrabench controller info --profile cloud
tetrabench controller deploy --profile cloud

tetrabench submit systems-design --profile cloud --run-id first-run
tetrabench status first-run --profile cloud
tetrabench result first-run --profile cloud
tetrabench artifacts pull first-run ./artifacts/first-run --profile cloud
```

Submission seals selected task fixtures and explicit context before publishing
an immutable request. S3 admission records own controller claims and
cancellation intent. Immutable terminal objects own final truth. Local receipts
are recovery hints, not run authority.

## Repository benchmarks

The checked-in production catalog remains empty. `systems-design/authority-fencing`
is a source candidate with completed local, detached, and reward-forgery proofs,
but it stays unlisted until its exact four-run model calibration passes. This
does not affect projects created by `tetrabench init` or tasks added to a user's
own catalog.

Read [the benchmark contract](benchmarks/README.md) for task design and
admission, and [the project record](IMPLEMENTATION_PLAN.md) for authority
boundaries, decisions, live evidence, and remaining unproven claims.

## Development

```console
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest --strict-markers -m "not docker"
TETRABENCH_EXPECT_DOCKER_TESTS=11 uv run pytest --strict-markers -m docker
uv build
```

CI adds checks from Bandit, pip-audit, actionlint, and Gitleaks. Its installed-
wheel smoke exercises the authoring commands. The project is not currently
published to a package registry and does not currently declare an open-source
license.
