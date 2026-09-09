# tetrabench

Run Harbor evaluations with Docker or detached Modal execution, using your own
task categories and Harbor's native results.

[![CI](https://github.com/Tetraslam/tetrabench/actions/workflows/ci.yml/badge.svg)](https://github.com/Tetraslam/tetrabench/actions/workflows/ci.yml)

Tetrabench is an installed, [MIT-licensed](LICENSE) CLI for authoring tasks,
running sealed task sets, and retrieving results. Modal runs retain artifacts in
S3 or Tigris.

## Quick start

You need Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and a running
Docker daemon. Install a released version with:

```console
uv tool install --python 3.12 tetrabench
```

Create and run a fresh project:

```console
tetrabench init my-evals
cd my-evals
tetrabench doctor
tetrabench task validate benchmarks/tasks/example/hello-tetrabench
tetrabench plan example
tetrabench run example --engine docker --run-id hello --output ./hello
tetrabench result hello
```

The generated starter runs through Harbor with its Oracle solution and a
separate no-network verifier. A successful run ends with:

```text
Outcome: succeeded
Pass rate: 1 (1/1)
```

The installed CLI works outside its source checkout. For a development build,
install a local wheel instead (see [Development](#development)).
These docs describe this checkout; features absent from your installed release
require that local build, not an unpublished version from PyPI.

`init` creates a standalone project with the neutral `example` category:

```text
my-evals/
├── tetrabench.toml
└── benchmarks/
    ├── catalog.toml
    ├── example/README.md
    └── tasks/example/hello-tetrabench/
        ├── instruction.md
        ├── task.toml
        ├── environment/Dockerfile
        ├── solution/solve.sh
        ├── tests/Dockerfile
        └── tests/test.sh
```

Use `tetrabench init my-evals --section data-quality` to choose another
category name.

## Author an eval

Create an unlisted task, edit its instruction, environment, solution, and
verifier, then validate and add it to your project catalog:

```console
tetrabench task new example check-output

$EDITOR benchmarks/tasks/example/check-output/instruction.md
$EDITOR benchmarks/tasks/example/check-output/tests/test.sh

tetrabench task validate benchmarks/tasks/example/check-output
tetrabench task add example check-output benchmarks/tasks/example/check-output
tetrabench run example --engine docker --output ./check-output
```

Categories are data in `benchmarks/catalog.toml`, called *sections* by the CLI.
They are not limited to this repository's benchmark domains. The command above
runs all selected tasks in `example`, including the starter.

To add an empty category, first create its README under `benchmarks/`, then run
`tetrabench category-create data-quality --readme data-quality/README.md`.
The README path is relative to the catalog directory and must already exist.

`task validate` validates a sealed private copy through Harbor 0.22 without
Docker or provider calls. `task add` validates again before atomically adding a
binary entry to your catalog. See the [CLI reference](docs/cli-reference.md) for
validation limits and concurrent-edit caveats.

The generated task is deliberately small. Replace its exact-answer verifier
with assertions for your domain. A verifier writes Harbor's native
`/logs/verifier/reward.json`; binary tasks must produce exactly integer `0` or
`1`. See [benchmark authoring and admission](benchmarks/README.md) for the
stricter rules used by tetrabench's own benchmark catalog.

## Commands

| Command | Purpose | Side effects |
| --- | --- | --- |
| `init` | Create a runnable local project | New directory |
| `sections` | List configured categories and task counts | None |
| `category-create` | Add an empty category using an existing README | Atomic catalog update |
| `agents [NAME]` | Show controlled harnesses, options, and credential variables | None |
| `task new` | Create an unlisted Harbor task | New task directory |
| `task validate` | Seal and validate one fixture | None |
| `task add` | Add a validated task to the project catalog | Atomic catalog update |
| `doctor` | Validate config, catalog, context, and optional storage reads | None |
| `plan` | Resolve a canonical secret-free execution plan | None |
| `run --engine docker` | Run locally and wait | New private output directory |
| `run --engine modal` | Submit detached; optionally observe with `--wait` | Cloud mutation |
| `controller info` | Show the selected Modal deployment contract | None |
| `controller deploy` | Deploy the configured Modal controller | Cloud mutation, confirmation required |
| `submit` | Compatibility alias for `run --engine modal --detach` | Cloud mutation |
| `status`, `result` | Inspect a run using its recorded engine and location | Local or provider reads |
| `runs` | List local run references/receipts or remote records | Local or provider reads |
| `cancel` | Interrupt local work or cancel remote work | Mutation, confirmation required |
| `recover` | Clean a stopped Modal owner and prepare a successor | Cloud mutation, confirmation required |
| `artifacts pull` | Download a successful Modal run's artifacts | New private output directory |
| `artifacts verify` | Stream and hash a Modal terminal's inputs and artifacts | Provider reads; no local download |

Commands except `sections` accept `--json` for canonical machine-readable output.
The `--json` forms of `controller deploy`, `cancel`, and `recover` require
`--yes`.

See the [CLI reference](docs/cli-reference.md) for exit codes, retained failure
evidence, provider-read boundaries, cancellation and recovery behavior, and
artifact materialization limits.

## Project configuration

The starter uses local Docker without a user profile:

```toml
schema_version = 1
catalog_path = "benchmarks/catalog.toml"

[engine]
kind = "docker"

[harbor]
agent_name = "oracle"
attempts = 1
concurrency = 1
```

`run --engine docker|modal` overrides the selected profile and project engine.
Docker waits locally and rejects `--detach`. Modal defaults to detached;
`--wait` observes the remote result, and Ctrl-C stops observation without
cancelling the remote run. `--wait` and `--detach` cannot be combined.
The negative forms `--no-wait` and `--no-detach` are not supported.

User-specific overrides live at `~/.config/tetrabench/config.toml` on Linux.
They can select models and storage locations without committing personal
settings. Keep credentials in environment variables or provider credential
stores, not TOML. See [model API configuration](docs/cli-reference.md#model-api-configuration)
for legacy profiles. For a controlled run, save this portable file as
`opencode-run.toml`:

```toml
[harness]
name = "opencode"
version = "1.18.29"
model = "openai/gpt-5"
ancillary_models = "primary"

[harness.options]
variant = "high"

[harness.env]
OPENAI_API_KEY = "${MODEL_API_KEY}"

[harness.native_config]
format = "json"
text = '{"compaction":{"auto":true}}'
```

```console
tetrabench agents
tetrabench agents opencode --json
tetrabench run example --engine docker --harness ./opencode-run.toml
```

Supply `MODEL_API_KEY` through your secret manager. Before that model run, adapt
your own task's agent network policy and timeout: the generated starter permits
neither network access nor enough time for agent installation. Keep its separate
verifier offline. The example validates configuration, not model availability.

`--harness` replaces the run's harness without editing task bytes. Project
`[harness]` and user `[profiles.NAME.harness]` tables accept the same schema.
Supported harnesses are OpenCode, Codex, Claude Code, and native Earendil Pi
(`@earendil-works/pi-coding-agent`, version 0.74.0 or later). Controlled OpenCode
accepts only the verified 1.18.29 pin and uses its native `--auto` flag. Check
`agents NAME --json` for each adapter's capabilities and version restrictions.
The exact version pin is checked against the installed agent CLI before it solves
the task; it does not freeze all dependencies or hosted models. Tetrabench does
not copy interactive logins or home configuration. See the
[controlled harness reference](docs/cli-reference.md#controlled-harness-configuration)
for native config files, credential references, and ancillary-model limits.

## Detached Modal runs

Keep the project local by default and add this profile to
`~/.config/tetrabench/config.toml`, replacing the bucket name:

```toml
schema_version = 1

[profiles.cloud.engine]
kind = "modal"

[profiles.cloud.engine.settings]
app_name = "tetrabench"
function_name = "controller"
secret_name = "tetrabench-controller"

[profiles.cloud.storage]
provider = "tigris"
bucket = "your-private-bucket"
region = "auto"
prefix = "tetrabench"
```

Provision a private bucket and configure boto3's standard credential chain for
the local submitter. For Secret creation below, inject the controller's separate
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` into that process environment
through your secret manager. Do not put values in files or command arguments.
Model runs also need their model API variables in the Secret; Oracle does not.
Harbor children do not receive the controller's storage credentials.

Tigris uses `https://t3.storage.dev`. Mutable coordination accepts known
Single-region buckets and Multi-region `usa` or `eur` buckets. Tetrabench keeps
Global and Dual-region buckets readable for legacy results, but rejects them
before new run mutation because their cross-region consistency is insufficient
for admission compare-and-swap.

```console
uvx --from modal==1.5.4 modal setup
tetrabench controller info --profile cloud
```

On first deployment, create the exact environment printed above and its named
Secret. The environment includes the profile and package version, so a Secret
in the default or an older environment will not suffice. Replace
`ENVIRONMENT_FROM_INFO` below; skip environment creation if it already exists.
The native Modal API copies only the listed variables from the process:

```console
uvx --from modal==1.5.4 modal environment create ENVIRONMENT_FROM_INFO
uv run --no-project --python 3.12 --with modal==1.5.4 python - <<'PY'
import os
import modal

modal.Secret.objects.create(
    "tetrabench-controller",
    {name: os.environ[name] for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")},
    environment_name="ENVIRONMENT_FROM_INFO",
)
PY
```

From the submitter's credential environment, run:

```console
tetrabench doctor --profile cloud --online
tetrabench controller deploy --profile cloud

tetrabench run example --engine modal --profile cloud --wait --run-id first-run
tetrabench status first-run
tetrabench result first-run
# Explicitly audit remote input and artifact bytes:
tetrabench artifacts verify first-run
# After a successful result:
tetrabench artifacts pull first-run ./first-run-artifacts
```

Deployment automatically resolves the exact installed wheel and reports its
SHA-256. Keep the original wheel for unpublished development builds.
`controller info` only reports configuration. Use `--detach` instead of `--wait`
to return after submission. The installed-wheel Modal smoke completed one Oracle
task with reward `1` and no surviving run-owned compute, resolving the local
wheel automatically without a wheel environment override.

New runs record their engine, storage, and controller, so lifecycle commands work
outside the original project without `--profile`. For old remote runs without a
reference, use the original project/profile configuration. Legacy `cancel` and
`recover` also require `--environment ORIGINAL_NAMESPACE`, which older records
did not store. See the [CLI reference](docs/cli-reference.md#run-references).
Docker artifacts stay at their recorded local location; `artifacts pull` does
not copy them. Native logs and artifacts may contain workload-emitted secrets.

Routine remote reads validate authority records and, for `result`, the small
controller summary. JSON reports distinguish `verification_level` from
`payload_integrity = "unchecked"`; they do not rehash the full payload inventory.
Use `artifacts verify` for that read-only audit; human output shows verified totals
and separate missing/corrupt counts. Publication and artifact downloads still
verify content. Cost reports separate model, auxiliary, and infrastructure
evidence, with source and coverage labels. Unknown cost is not zero; reported
amounts and estimates are not a provider invoice or a universal spending cap.
Pi's unpriced default zero is excluded from known subtotals, and a Claude Code
aggregate without its raw source is not assumed to be an estimate. See
[cost reports](docs/cli-reference.md#cost-reports) for those provenance limits.

## Repository benchmarks

The checked-in production catalog includes `systems-design/authority-fencing`.
Its 1 GiB agent environment passed local, detached, reward-forgery, and exact
four-run model calibration gates as an end-to-end platform proof. It does not
define a queue of future evals or affect user-created projects. The fixture is
included in the source distribution, not the wheel.

Read [the benchmark contract](benchmarks/README.md) for task design and
admission, and [the project record](IMPLEMENTATION_PLAN.md) for authority
boundaries, decisions, live evidence, and remaining unproven claims.

## Development

Build and install a wheel from a fresh checkout:

```console
git clone https://github.com/Tetraslam/tetrabench.git
cd tetrabench
uv build --wheel
```

Install the wheel with its hash so uv retains the artifact identity needed for
deployment:

```console
wheel=$(realpath dist/tetrabench-*.whl)
digest=$(sha256sum "$wheel" | cut -d ' ' -f1)
uv tool install --python 3.12 "tetrabench @ file://$wheel#sha256=$digest"
```

Use a `dist` directory containing only the wheel you intend to install. Keep it
at that path for deployment if the exact build is not on PyPI. A plain wheel
path can omit the original digest from uv's installation metadata, forcing
deployment to look up PyPI instead. Released versions use the simple
`uv tool install --python 3.12 tetrabench` command above.

To validate the checkout:

```console
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest --strict-markers -m "not docker"
TETRABENCH_EXPECT_DOCKER_TESTS=11 uv run pytest --strict-markers -m docker
uv build
```

CI adds Bandit, pip-audit, actionlint, Gitleaks, distribution metadata checks,
and an installed-wheel smoke. Forced interruption/recovery has live evidence.
Live AWS behavior and provider-initiated Modal preemption remain unproven.
