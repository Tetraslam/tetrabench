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
The 0.3.0 candidate passed API-key and normal subscription evals for all four
harnesses, plus actual native OAuth refresh and fresh-controller consumption for
Codex, OpenCode, and Pi. That evidence used Claude 2.1.267. The newer onboarding
helpers passed installed offline journeys with transport/terminal doubles;
live onboarding and Claude 2.1.269 checks, full validation, and release gates remain open.
This is not a fully verified or published 0.3.0 release. See
[testing limits](docs/cli-reference.md#testing-and-limitations) for
the source-candidate evidence, separate from published-release proofs.

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
| `auth login/status/logout/reseed` | Manage an explicitly selected eval login | Native login or private auth-state reads/writes |
| `models inspect` | Collect installed native model/reasoning metadata | Offline by default; opt-in metadata reads/auth-state updates |
| `models adopt` | Preview a reasoning choice and its snapshot | Optional auth-state updates; harness file update with `--write` |
| `task new` | Create an unlisted Harbor task | New task directory |
| `task validate` | Seal and validate one fixture | None |
| `task add` | Add a validated task to the project catalog | Atomic catalog update |
| `doctor` | Check local setup; optionally inspect storage/controller or auth metadata | Offline by default; explicit auth check may refresh private state |
| `plan` | Resolve a canonical secret-free execution plan | Local reads; remote auth reads require `--online` |
| `run --engine docker` | Run locally and wait | New private output directory |
| `run --engine modal` | Submit detached; optionally observe with `--wait` | Cloud mutation |
| `controller info` | Show the selected Modal deployment contract | None |
| `controller configure` | Preview explicitly selected credentials for a controller | Secret/environment mutation only with confirmed `--write` |
| `controller deploy` | Deploy the configured Modal controller | Cloud mutation, confirmation required |
| `submit` | Compatibility alias for `run --engine modal --detach` | Cloud mutation |
| `status`, `result` | Inspect a run using its recorded engine and location | Local or provider reads |
| `runs` | List local run references/receipts or remote records | Local or provider reads |
| `cancel` | Interrupt local work or cancel remote work | Mutation, confirmation required |
| `recover` | Clean a stopped Modal owner and prepare a successor | Cloud mutation, confirmation required |
| `artifacts pull` | Download a successful Modal run's artifacts | New private output directory |
| `artifacts verify` | Stream and hash a Modal terminal's inputs and artifacts | Provider reads; no local download |

Commands except `sections` accept `--json` for canonical machine-readable output.
The `--json` forms of `controller deploy`, `cancel`, and `recover`, plus
`controller configure --write --json`, require `--yes`.

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

### API key, local by default

User-specific overrides live at `~/.config/tetrabench/config.toml` on Linux.
They can select models and storage locations without committing personal
settings. Keep secret values in your secret manager or private native credential
store; TOML contains references only. See [model API configuration](docs/cli-reference.md#model-api-configuration)
for legacy profiles. API-key runs need no host agent CLI or OAuth login: Harbor
installs the selected harness in the sandbox. Save this portable file as
`opencode-run.toml`:

```toml
[harness]
name = "opencode"
version = "1.18.30"
model = "openai/gpt-5"
ancillary_models = "primary"

[harness.options]
variant = "high"

[harness.auth]
mode = "api_key"
reference = { kind = "env", name = "MODEL_API_KEY" }

[harness.native_config]
format = "json"
text = '{"compaction":{"auto":true}}'
```

Supply `MODEL_API_KEY` through your secret manager. Before that model run, adapt
the starter's `task.toml`: set `[environment] network_mode = "public"` and give
`[agent] timeout_sec` enough time for installation and solving (for example, 300).
Keep `[verifier.environment] network_mode = "no-network"`. Then run locally with
the project's default Docker engine:

```console
tetrabench doctor --harness ./opencode-run.toml
tetrabench run example --harness ./opencode-run.toml --run-id api-local
tetrabench result api-local
```

No credential value belongs in TOML or command arguments. The optional
`doctor --harness ./opencode-run.toml --check-provider` uses the declared key for
native metadata, requires the matching host CLI, and sends no paid prompt; it is
not an account or entitlement attestation. For cloud API-key execution, use the
[named-environment setup below](#detached-modal-runs).

`--harness` replaces the run's harness without editing task bytes. Project
`[harness]` and user `[profiles.NAME.harness]` tables accept the same schema.
The preferred baseline frozen on 2026-09-11 is pinned per run:

| Harness | Package | Baseline pin |
| --- | --- | --- |
| OpenCode | `opencode-ai` | `1.18.30` |
| Codex | `@openai/codex` | `0.154.0` |
| Claude Code | `@anthropic-ai/claude-code` | `2.1.269` |
| Earendil Pi | `@earendil-works/pi-coding-agent` | `0.85.1` |

Check `agents NAME --json` for options and version restrictions. New native
controls and explicit auth use these pins; Claude 2.1.267 remains accepted for
historical records and explicit runs, without relabeling its live evidence as
2.1.269. OpenCode uses native `--auto`; Codex retains
Harbor's sandbox/approval override, `--dangerously-bypass-approvals-and-sandbox`.
The exact version pin is checked against the installed agent CLI before it solves
the task; it does not freeze all dependencies or hosted models. Tetrabench does
not copy your global interactive login or home configuration. Native context
management stays native: opaque OpenAI compaction, text summarization, and
pruning are different mechanisms. A config snapshot is not evidence that any
compaction occurred. See the
[controlled harness reference](docs/cli-reference.md#controlled-harness-configuration)
for native config, resource bundles, session controls, and ancillary-model limits.

Use separate configurations for API billing and subscription billing. Explicit
`auth.mode` accepts `api_key`, `chatgpt_oauth` (Codex, OpenCode, Pi), or
`claude_setup_token` (Claude Code only). The
[auth setup steps](docs/cli-reference.md#explicit-authentication) use the existing
CLI, which creates private local OAuth configuration and state; no manual binding,
generation, runtime path, or Python setup is needed for a new local login.
Each OAuth harness needs its own eval login and isolated home. Browser approval
belongs to the native client. Claude's long-lived setup token is user-managed,
not a refreshable session owned by an intermediary. No Claude subscription OAuth
is supported in another harness.

### Optional local ChatGPT login

For a local ChatGPT login, install only the pinned Codex host CLI, then
let tetrabench create an independent eval profile:

```console
npm install --global @openai/codex@0.154.0
tetrabench auth login --profile codex-local --agent codex
```

Save `codex-oauth-run.toml`:

```toml
[harness]
name = "codex"
version = "0.154.0"
model = "openai/gpt-6-astra"

[harness.auth]
mode = "chatgpt_oauth"
reference = { kind = "profile", profile = "codex-local" }
```

With the network-enabled task above and `[harbor] concurrency = 1`, run:

```console
tetrabench auth status --profile codex-local
tetrabench run example --harness ./codex-oauth-run.toml --run-id oauth-local
tetrabench result oauth-local
```

Auth commands' `--profile` selects a login; `run --profile` selects engine/storage
settings. Future runs resolve the login's current generation; old sealed runs do
not change. Replacing a ready login requires `auth login --profile codex-local
--replace`. See [local login](docs/cli-reference.md#local-eval-login) for
OpenCode/Pi and logout/relogin, [Claude setup tokens](docs/cli-reference.md#claude-subscription-token),
or the [remote OAuth journey](docs/cli-reference.md#remote-private-auth-state).

For reasoning choices, install the matching native CLI on the inspection host,
then run `tetrabench models inspect --harness ./opencode-run.toml`. Inspection is
offline by default and automatically collects native metadata, without a manual
capture file. Use `models adopt` to preview a supported choice before writing its
bound snapshot. Unknown model/route support remains unknown; see
[model inspection](docs/cli-reference.md#model-inspection-and-adoption).

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
API-key and setup-token runs also need their declared credential variables in the
Secret; OAuth uses the selected auth profile and backend credentials described
below. Oracle needs neither.
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

For the API-key harness above, preview the named variables, then confirm their
transfer into the profile's Secret. `--create-environment` permits creation of
that exact versioned environment; it does not provision a bucket or account:

```console
tetrabench controller configure --profile cloud --harness ./opencode-run.toml \
  --env AWS_ACCESS_KEY_ID --env AWS_SECRET_ACCESS_KEY --env MODEL_API_KEY \
  --create-environment
tetrabench controller configure --profile cloud --harness ./opencode-run.toml \
  --env AWS_ACCESS_KEY_ID --env AWS_SECRET_ACCESS_KEY --env MODEL_API_KEY \
  --create-environment --write
```

Only listed names are copied, with values supplied by your secret manager in the
configure process. For Oracle, omit `--harness` and `--env MODEL_API_KEY`. For an
existing Secret, `--update --write` merges selected keys; it neither removes old
credential keys nor refreshes a running controller. Use separate run profiles for
different billing modes. From the submitter's storage credential environment:

```console
tetrabench controller deploy --profile cloud
tetrabench doctor --profile cloud --harness ./opencode-run.toml --online

tetrabench run example --profile cloud --harness ./opencode-run.toml --wait --run-id first-run
tetrabench status first-run
tetrabench result first-run
# Explicitly audit remote input and artifact bytes:
tetrabench artifacts verify first-run
# After a successful result:
tetrabench artifacts pull first-run ./first-run-artifacts
```

For Oracle, omit `--harness` from doctor/run too. For ChatGPT OAuth, follow the
[existing-backend journey](docs/cli-reference.md#remote-private-auth-state), which
also transfers the selected auth profile. `doctor --online` checks storage and
controller metadata, not remote credential consumption. Deployment automatically
resolves the exact installed wheel and reports its SHA-256. Keep the original wheel
for unpublished development builds.
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
evidence, with source/coverage labels and known native OpenCode/Pi text-summary
costs without double counting. Unknown cost is not zero; reported amounts and
estimates are not a provider invoice or a universal spending cap.
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
TETRABENCH_EXPECT_DOCKER_TESTS=12 uv run pytest --strict-markers -m docker
uv build
```

CI adds Bandit, pip-audit, actionlint, Gitleaks, distribution metadata checks,
and an installed-wheel smoke. Forced interruption/recovery has live evidence.
Live AWS behavior and provider-initiated Modal preemption remain unproven.

The developer-only `tools/install_native_consumers.py --prefix DIRECTORY`
installs locked official consumers outside the checkout for native tests. It is
not the end-user installation workflow. Native verification used Node 24.21.0;
Pi's engine minimum is 22.19.0, not Node 24 generally. With Node on `PATH`, run it via
`uv run python`, then set `TETRABENCH_NATIVE_NODE_MODULES` to the reported
`DIRECTORY/node_modules` when running `uv run pytest -m native`. These tests
exercise pinned consumers without proving live authentication or remote runtime
integration.
