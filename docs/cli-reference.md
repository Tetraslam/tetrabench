# CLI reference

Tetrabench uses Rich for human output. Commands except `sections` accept
`--json`, which writes one RFC 8785 canonical JSON document followed by a newline
on success. Errors go to stderr. `plan` errors remain human-readable even with
`--json`. Local configuration and integrity errors keep their specific message.
Caught Botocore and Modal exceptions become `provider_error` with fixed advice
selected from known exception types or service codes. Their messages, URLs, and
chained errors are not copied into public diagnostics. See
[diagnostics](#diagnostics) for codes and runtime requirements.

## Local project and tasks

`tetrabench init DIRECTORY [--section NAME]` creates a new local Docker project
with one binary starter task. The default category is `example`. The destination
must not exist and its parent must exist.

`tetrabench sections` lists the categories in the configured catalog and their
task counts. Categories use `[sections.NAME]` tables in
`benchmarks/catalog.toml`; they are project data, not a fixed domain list. Names
are 1 to 64 lowercase ASCII letters, digits, dots, underscores, or hyphens,
starting with a letter or digit. Each section requires a `readme` path relative
to the catalog directory; `description` is optional. To add a category to an
existing project, create its README, then run:

```console
mkdir -p benchmarks/data-quality
printf '# Data quality\n' > benchmarks/data-quality/README.md
tetrabench category-create data-quality --readme data-quality/README.md
tetrabench task new data-quality check-output
tetrabench task validate benchmarks/tasks/data-quality/check-output
tetrabench task add data-quality check-output benchmarks/tasks/data-quality/check-output
tetrabench plan data-quality --json
```

`category-create NAME --readme PATH [--project DIRECTORY]` requires an existing
README relative to the catalog directory. It atomically appends a section with
`tasks = []`, preserving existing catalog content and leaving task fixtures alone.

`tetrabench task new SECTION TASK_ID` creates an unlisted task under
`benchmarks/tasks/`. The section must already exist. `--project DIRECTORY`
selects another project for `task new`; other authoring commands use the current
project directory.

`task validate FIXTURE` seals the complete fixture tree, validates a private copy
through Harbor 0.22, then checks that the source still matches. It does not call
Docker or a provider. Default sealing limits are 256 files, 16 MiB per file,
128 MiB total, 10,000 discovered entries, 10,000 directories, and depth 64.
Validation rejects symlinks and special files.

`task add SECTION TASK_ID FIXTURE` validates twice, rejects duplicate IDs and
fixture paths, and atomically appends a binary task entry while preserving
existing catalog content. The catalog and its lock must be outside the fixture
tree. A sibling advisory lock serializes tetrabench writers; arbitrary programs
editing the catalog concurrently are outside that cooperative lock.

`tetrabench plan SECTION [--profile PROFILE] [--engine docker|modal]
[--harness FILE] [--online]` prints a secret-free plan and its digest. JSON mode
emits the plan alone. An empty selection is valid but not runnable. A local OAuth
profile resolves read-only; an S3-backed profile requires `--online`. Run preparation
resolves the selected authority before sealing the immutable request.

`tetrabench doctor` checks project configuration, catalog selection, section
READMEs, explicit context, and auth setup. `--harness FILE` and `--engine` select
what to diagnose; `--auth-profile NAME` asserts the selected login, not a billing
override. The default performs no provider calls, login, refresh, or inference.

`--online` checks storage via `HeadBucket`, `GetBucketLocation`, and a one-key list.
With an explicit Modal harness it also checks named environment/Secret/Function
metadata, not Secret values or a running child's credentials. Storage writes and
remote runtime readiness remain unproven. `--online` alone does not authorize
model-provider metadata access.

Optional `doctor --harness FILE --check-provider` uses declared credentials for
native metadata, with native refresh/private write-back if needed. It requires
the matching host CLI but performs no login or inference and sends no paid prompt.
Read `metadata_status` separately: bundled/cached metadata is not an attested
provider GET, verified account, or universal entitlement. For Modal this checks
from the submitter, not the remote controller; ordinary remote API-key checks do
not require the model key on the submitter.
Operational metadata failures exit 2; unsupported or unproven metadata alone may
exit 0 and does not imply successful provider verification. Doctor configuration
errors are canonical JSON on stderr with exit 2 when `--json` is selected.

## Engine configuration

The project file is `tetrabench.toml` in the current directory. User profiles are
in `~/.config/tetrabench/config.toml` on Linux (or under `XDG_CONFIG_HOME`).
Precedence is project, selected user profile, then CLI `--engine` / `--harness`.

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

Docker does not accept engine settings. Modal accepts `app_name`, `function_name`,
and `secret_name` under `[engine.settings]`; the equivalent profile tables are
`[profiles.cloud.engine]` and `[profiles.cloud.engine.settings]`. See the
[complete cloud profile](../README.md#detached-modal-runs).

Settings merge when the engine kind stays the same. Switching engines clears
the previous engine's settings. Unknown engines, settings, and configuration
fields fail before provider work. Legacy `[controller]` and `[execution]`
configuration remains readable, but cannot be combined with `[engine]` in the
same configuration layer. Resolved plans still carry the controller/execution
fields for persisted-record compatibility.

### Model API configuration

Legacy `[harbor]` profiles remain supported and unpinned. They do not provide the
controlled settings or version evidence described below. Tetrabench passes
`agent_name` and `model_name` unchanged to Harbor 0.22. The
`opencode` adapter requires a `provider/model` name. Add this profile
to your user configuration (with top-level `schema_version = 1`):

```toml
[profiles.openai.harbor]
agent_name = "opencode"
model_name = "openai/gpt-4.1"
```

Supply `OPENAI_API_KEY` in the environment running the Docker engine. For an
OpenAI-compatible endpoint, also set `OPENAI_BASE_URL` to its API root, such as
`https://gateway.example.com/v1`, and use `openai/<served-model-id>`. Harbor
forwards the key and puts the explicit base URL in OpenCode's provider options.
The endpoint must support the API used by the selected OpenCode model.

Other provider profiles use the same shape:

```toml
[profiles.anthropic.harbor]
agent_name = "opencode"
model_name = "anthropic/claude-sonnet-4-5"

[profiles.openrouter.harbor]
agent_name = "opencode"
model_name = "openrouter/openai/gpt-4.1"
```

Use `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_BASE_URL`) for Anthropic, or
`OPENROUTER_API_KEY` for OpenRouter. To use the OpenAI profile, run
`tetrabench run example --engine docker --profile openai`.

The starter is designed for Oracle: its agent environment has no network and a
30-second agent timeout. Before using an API-backed agent, edit the task's
network policy and timeout to allow installation and access to your endpoint.
Do not relax the separate verifier's no-network policy merely to connect the
agent. The examples describe configuration and Harbor forwarding, not live
availability of a model or endpoint.

For Modal, add the model profile fields to your cloud profile and supply the
model API variables through its named controller Secret. The submitter does not
upload local environment variables. Harbor forwards the selected provider's
model credentials into the agent; it does not inherit your interactive OpenCode
authentication or local OpenCode configuration. Tetrabench exposes neither
arbitrary Harbor agent kwargs nor an `api_key` field in TOML. Keep secret values
out of project files, plans, and receipts. Native Harbor logs and artifacts may
still contain workload-emitted secrets and must be treated as private.

### Controlled harness configuration

Earlier 0.3.0 candidates have live API-key and normal subscription evidence for
all four harnesses, plus native OAuth refresh/fresh-controller consumption for
Codex, OpenCode, and Pi. Claude live evidence is at 2.1.267, not the preferred
2.1.269. New onboarding journeys and 2.1.269 live acceptance remain pending. See
[testing limits](#testing-and-limitations); this is not a fully verified or
published 0.3.0 release.

`tetrabench agents [NAME] [--json]` lists the registered harnesses. JSON includes
package identity, supported native formats, option types/choices/defaults,
credential variables, version restrictions, and limitations. Only the four
listed adapters are registered; arbitrary agent import paths or shell arguments
are not accepted.

| Name | Native package | Preferred pin (frozen 2026-09-11) | Native configuration |
| --- | --- | --- | --- |
| `opencode` | `opencode-ai` | `1.18.30` | JSON or JSONC |
| `codex` | `@openai/codex` | `0.154.0` | TOML or JSON |
| `claude-code` | `@anthropic-ai/claude-code` | `2.1.269` | JSON |
| `pi` | `@earendil-works/pi-coding-agent` | `0.85.1` | JSON with `settings` and/or `models` objects |

Use `agents NAME --json` for the complete option types and conflicts. OpenCode
exposes `variant`, `title`, `agent`, and `pure`; Codex exposes `reasoning_effort`,
`reasoning_summary`, and `web_search`. Claude Code adds native output/thinking,
autocompaction, tool, prompt, MCP, settings-source, and persistence controls.
Pi adds native tool, extension/skill, prompt, offline/discovery, and session
controls. An accepted option name alone does not prove a model supports it.

Use `[harness]` in the project, `[profiles.NAME.harness]` in user configuration,
or a separate TOML file passed to `run --harness FILE`. That separate file must
contain only the `[harness]` table and its subtables, without `schema_version`,
engine settings, or task selection. A higher-precedence harness replaces the
whole lower-precedence harness, rather than merging individual options.
`[harbor]` still controls `attempts` and `concurrency`. Do not combine a harness
with legacy agent/model selection in the same configuration layer; a later
legacy agent/model override clears the controlled harness.

Required fields are `name`, exact `version = "x.y.z"`, and `model = "provider/model"`.
Controlled OpenCode accepts `1.18.29` and `1.18.30`. Its run
command uses `--auto`, not Harbor 0.22's unsupported
`--dangerously-skip-permissions`. Other OpenCode pins, including `1.2.15`, fail
validation before installation. `agents NAME --json` reports version restrictions;
Claude accepts exactly 2.1.267 and 2.1.269 for these native controls. New
controls, resource/session configuration, discovery policy, and explicit auth
use the pins above; Claude's exact 2.1.267 contract remains accepted for explicit
runs and historical records. `latest` is not accepted in a run configuration.
OpenCode and Pi preserve nested model IDs; Codex and Claude Code reject them
because their Harbor adapters truncate them. Pi requires version 0.74.0 or later
and uses Harbor's native Earendil installer. During setup, tetrabench probes the
installed executable and refuses a version mismatch before task solving. The pin
does not make all transitive dependencies, plugins, or hosted models immutable.

Optional fields:

- `options`: only the adapter's listed options. Limits such as `max_turns` use
  integers; `max_budget_usd` uses a decimal string, such as `"1.50"`.
- `args`: supported flags and values normalized to those same options, not a raw
  shell escape. For example, `args = ["--variant", "high"]` replaces
  `[harness.options] variant = "high"`; specifying both is an error.
- `env`: agent variable names mapped to host references such as
  `OPENAI_API_KEY = "${MODEL_API_KEY}"`. Literal credentials and reserved storage,
  controller, or process-control variables are rejected. Custom credential names
  must be referenced by native configuration. Docker resolves references from
  the local process; Modal resolves them inside the controller's named Secret.
  The submitter does not upload local credential values.
- `native_config`: `format` plus exactly one of `path` or `text`. The UTF-8
  content is bounded to 128 KiB and sealed into the plan with its SHA-256; a path
  is resolved relative to the TOML file declaring it. Host paths are not sent to
  the controller. Native auth/env overlays and conflicting model settings fail
  validation. Native fields outside tetrabench's checks retain native semantics.
- `ancillary_models`: `"primary"` (default) or `"native"`. Primary routes known
  ancillary settings to the requested model: OpenCode's small model and Claude
  Code's default tiers/subagent model. OpenCode gets a fixed title to avoid title
  generation; conflicting explicit ancillary models are rejected. Codex role
  `config_file` entries, including roles inside native `profiles`, require
  `"native"` because role files can override model selection. At the baseline pin,
  referenced files must be sealed as portable harness resources, not assumed
  present on the remote host. Native allows the harness's ancillary choices.
  Neither policy intercepts every possible model call or guarantees a universal
  billing cap.
- `auth`: an explicit billing mode and credential reference; see
  [authentication](#explicit-authentication). Do not combine it with model-auth
  selectors in `env` or native provider credential/endpoint overrides.
- `resources`, `discovery`, `session`: run-level inputs and native lifecycle
  controls, described [below](#resource-bundles-and-session-controls).
- `capability_snapshot`: model/route/config-bound evidence written by
  `models adopt`; do not hand-edit it.

The [README OpenCode example](../README.md#project-configuration) is a complete
portable file. Equivalent examples for the other adapters follow. Save each in
its named file, supply the referenced environment variable, then use
`tetrabench run example --engine docker --harness FILE` with your network-enabled
task. These are offline-validated configurations, not claims of current model or
endpoint availability.

`codex-run.toml`:

```toml
[harness]
name = "codex"
version = "0.154.0"
model = "openai/gpt-5"

[harness.options]
reasoning_effort = "medium"
web_search = "disabled"

[harness.auth]
mode = "api_key"
reference = { kind = "env", name = "OPENAI_EVAL_KEY" }

[harness.native_config]
format = "toml"
text = 'model_context_window = 100000'
```

`claude-run.toml`:

```toml
[harness]
name = "claude-code"
version = "2.1.269"
model = "anthropic/claude-sonnet-4-6"

[harness.options]
max_turns = 3

[harness.auth]
mode = "api_key"
reference = { kind = "env", name = "ANTHROPIC_EVAL_KEY" }

[harness.native_config]
format = "json"
text = '{"permissions":{"allow":["Read"]}}'
```

`pi-run.toml`:

```toml
[harness]
name = "pi"
version = "0.85.1"
model = "openai/gpt-5"

[harness.options]
thinking = "high"

[harness.auth]
mode = "api_key"
reference = { kind = "env", name = "PI_EVAL_KEY" }

[harness.native_config]
format = "json"
text = '{"settings":{"compaction":{"enabled":true}}}'
```

For a native file, replace `text` with `path = "./settings.json"` (or a TOML path
for Codex). Pi's wrapper writes its `settings` and `models` objects as native
`settings.json` and `models.json`. A Pi base-URL environment reference requires
an explicit `model_api` option; this endpoint route cannot also supply a native
`models` object. Use `agents pi --json` for accepted API names.

At Pi 0.85.1, native `models` configuration uses `"${TOKEN}"` for `apiKey`.
Older supported Pi configurations used a bare `"TOKEN"`; do not carry that
syntax into the current pin. The outer `harness.env` maps `TOKEN` to
`"${HOST_TOKEN}"`. For example, save this
portable custom-endpoint configuration as `pi-custom-run.toml`:

```toml
[harness]
name = "pi"
version = "0.85.1"
model = "custom/model"

[harness.env]
TOKEN = "${HOST_TOKEN}"

[harness.native_config]
format = "json"
text = '''
{"models":{"providers":{"custom":{
  "baseUrl":"https://example.test/v1",
  "api":"openai-responses",
  "apiKey":"${TOKEN}",
  "models":[{"id":"model"}]
}}}}
'''
```

Replace the example endpoint and model with your own before running. No price
is supplied here; Pi's default zero does not prove a free model call.

Codex native `env_key`, `bearer_token_env_var`, and the values of
`env_http_headers` likewise name agent variables declared in `harness.env`.
For example, `env_key = "TOKEN"` selects `TOKEN = "${HOST_TOKEN}"` from the outer
table. Direct `bearer_token` and `experimental_bearer_token` fields are rejected,
even if their values look like environment references. Use the native selectors
instead of literal secrets or shell interpolation.

Controlled runs use declared credential references, not ambient interactive
logins or home configuration. Anonymous endpoints require explicit native
endpoint configuration. Native adapters still own execution. The agent artifact
`tetrabench-harness.json` records requested/observed CLI versions, options,
credential references, and effective native configs with resolved credential
values replaced by references. Treat the rest of the native artifacts as private.

### Native context management

Unspecified context controls retain the pinned native defaults. OpenCode's
`compaction` configuration, Codex's native compaction/context settings, Claude
Code's `autocompact`/`disable_auto_compact`, and Pi's `settings.compaction` and
`settings.branchSummary` retain their own semantics. Claude's `autocompact`
accepts `"auto"` or a native token window from 100k to 1M; it conflicts with
`disable_auto_compact = true`.

Server-side OpenAI compaction returns opaque state; client-managed text
summarization and pruning are different mechanisms. Tetrabench does not replace
them with a generic summarizer or infer opaque compaction from a setting named
`compaction`. Config provenance records requested controls with
`evidence = "configuration_only"`, an unobserved mechanism, and
unknown compaction counts; that snapshot alone does not establish execution.
The bounded live continuation evidence is listed [below](#testing-and-limitations).

Codex retains Harbor's `--dangerously-bypass-approvals-and-sandbox` override.
Changing native context settings does not restore Codex's own sandbox or approval
prompts; the task's Docker/Modal boundary still matters.

### Explicit authentication

In `[harness.auth]`, `schema_version` defaults to `1`, `mode` selects billing, and
`reference` selects authority. Authoring files support:

| Mode | Harnesses | Reference | Operator owns |
| --- | --- | --- | --- |
| `api_key` | All four | `{ kind = "env", name = "SOURCE_VARIABLE" }` | Key provisioning and rotation |
| `chatgpt_oauth` | Codex, OpenCode, Pi | `{ kind = "profile", profile = "NAME" }` | Separate native login per harness and private state backend |
| `claude_setup_token` | Claude Code only | `{ kind = "env", name = "SOURCE_VARIABLE" }` | Long-lived subscription setup token and renewal |

Profile references resolve once to an exact `native_session` before immutable
sealing. Existing `{ kind = "native_session", profile = "NAME", generation = 1,
binding = "BINDING" }` inputs remain supported and fixed; canonical run records
never contain a floating profile reference.

The four API-key examples are the [README OpenCode file](../README.md#project-configuration)
and `codex-run.toml`, `claude-run.toml`, `pi-run.toml` above. Supply the named
source variable through your secret manager in the local Docker process or
controller Secret. Codex selects direct OpenAI billing; Claude Code selects
direct Anthropic billing. OpenCode/Pi use the selected provider's native key
variable (for example, `openrouter/...` selects OpenRouter). Unsupported provider
contracts fail validation rather than guessing a variable.

Use distinct run files or user profiles for API keys and subscriptions. Explicit
auth rejects competing ambient credentials/config selectors and never silently
falls back to another account. Unset competing selectors before login or a run.
API keys and setup tokens need no OAuth lineage file or private `auth.toml`.
Codex uses noninteractive native `login --with-api-key`, passing the referenced
key over stdin into private ephemeral native state. This does not open a browser
or persist a refreshable OAuth lineage. It also applies when authenticated model
inspection needs a Codex API-key session.

#### Local eval login

API-key and existing Claude setup-token evals need no host agent CLI: Harbor
installs it in the sandbox. Native login and model metadata inspection need the
matching host installation. Install only the harness you use:

| Harness | Host installation |
| --- | --- |
| Codex | `npm install --global @openai/codex@0.154.0` |
| OpenCode | `npm install --global opencode-ai@1.18.30` |
| Pi | `npm install --global @earendil-works/pi-coding-agent@0.85.1` |
| Claude Code | `npm install --global @anthropic-ai/claude-code@2.1.269` |

Pi requires Node **22.19.0 or later** and the installed package; Node 24.21.0 was
used for verification, not imposed as the general minimum. Normal discovery
uses `pi` on `PATH`; `--executable` selects Node, not the Pi script, and optional
`--pi-module` selects that package's `dist/index.js`. For other harnesses,
`--executable` selects the actual `codex`, `opencode`, or `claude` file. Explicit
historical Claude 2.1.267 runs/inspection require that exact host version.

From a project with a network-enabled agent task and `[harbor] concurrency = 1`:

```console
tetrabench auth login --profile codex-local --agent codex
tetrabench auth status --profile codex-local
```

The helper checks the installed pin, creates private configuration/state, generates
the binding, and asks the native client to obtain approval in an isolated eval
home. It does not copy an interactive login. No manual `auth.toml`, generation, or
runtime path is needed. Save `codex-oauth-run.toml`:

```toml
[harness]
name = "codex"
version = "0.154.0"
model = "openai/gpt-6-astra"

[harness.auth]
mode = "chatgpt_oauth"
reference = { kind = "profile", profile = "codex-local" }
```

```console
tetrabench doctor --harness ./codex-oauth-run.toml
tetrabench run example --harness ./codex-oauth-run.toml --run-id oauth-local
tetrabench result oauth-local
```

`auth login/status/logout/reseed --profile` selects a **login profile** in private
`auth.toml`; `run/plan/doctor/controller --profile` selects a **run profile** in
`config.toml`. New logins require `--agent`; existing logins infer it. Local runs
load the default `auth.toml`; for another path, set
`TETRABENCH_AUTH_CONFIG_FILE` in the runtime environment. Auth commands also
accept `--auth-config FILE`. Do not set both file and content sources.

Ready logins require explicit `auth login --profile codex-local --replace`.
`auth logout --profile codex-local`, then `auth login --profile codex-local`, uses
the next generation. Future profile-based runs resolve it automatically; old
sealed runs and capability snapshots stay unchanged. Replacement cannot rebind
the harness/backend or take over a claimed session. Managed profiles omit a fixed
generation; existing fixed-generation profiles remain fixed.

Native device approval is the default; `--browser-auth` selects browser approval.
`status` never refreshes or proves server acceptance. A failed first login may leave
an uninitialized profile; inspect status after an ambiguous result rather than
automatically repeating login. The lower-level `--harness FILE` auth form remains
available but cannot be combined with `--profile`.

For OpenCode or Pi, use `auth login --profile NAME --agent opencode` or
`auth login --profile NAME --agent pi` with a distinct name, then a separate run
file with that harness/pin and profile reference.
OpenCode's ChatGPT route requires `openai/...`; Pi's requires `openai-codex/...`.
Never clone one harness's tokens into another. Parallel trials sharing a lineage
are rejected. Keep private auth state outside task, resource, output, and artifact
trees; it is not portable project content.

#### Claude subscription token

Save `claude-subscription-run.toml` separately from the API-key configuration:

```toml
[harness]
name = "claude-code"
version = "2.1.269"
model = "anthropic/claude-opus-5"

[harness.auth]
mode = "claude_setup_token"
reference = { kind = "env", name = "CLAUDE_EVAL_TOKEN" }
```

For a network-enabled agent task, inject an existing setup token as
`CLAUDE_EVAL_TOKEN` through your secret manager:

```console
tetrabench run example --harness ./claude-subscription-run.toml --run-id claude-local
tetrabench result claude-local
```

To obtain a token, first install the matching host CLI above, then run
`tetrabench auth login --harness ./claude-subscription-run.toml`. It invokes native
`claude setup-token` in an isolated home; complete approval and store the result
in your secret manager. Tetrabench does not save the setup token. You own
renewal on expiry; an intermediary cannot refresh it as a Codex OAuth lineage.
Claude subscription credentials are not supported in OpenCode, Codex, or Pi.

#### Remote private auth state

Use an existing private artifact bucket and a **different private auth bucket**.
The CLI provisions neither buckets, keys, IAM policies, nor accounts. A local
auth directory or a token snapshot in a Modal Secret cannot replace the durable
remote auth backend. This journey uses a new `cloud-oauth` run profile, separate
from any API-key controller.

In `~/.config/tetrabench/config.toml`, add the following (keep only one top-level
`schema_version` if the file already exists):

```toml
schema_version = 1

[profiles.cloud-oauth.engine]
kind = "modal"

[profiles.cloud-oauth.engine.settings]
app_name = "tetrabench-oauth"
function_name = "controller"
secret_name = "tetrabench-oauth-controller"

[profiles.cloud-oauth.storage]
provider = "tigris"
bucket = "your-private-artifact-bucket"
region = "auto"
prefix = "tetrabench"
```

Save `~/.config/tetrabench/cloud-auth-backend.toml` outside the project, mode `0600`,
with this root-level backend definition, not an `[auth]` or `[profiles]` wrapper:

```toml
kind = "s3"
approved_private_backend = true
trust_organization_admins = false
access_key = { kind = "env", name = "TETRABENCH_AUTH_ACCESS_KEY_ID" }
secret_key = { kind = "env", name = "TETRABENCH_AUTH_SECRET_ACCESS_KEY" }

[storage]
provider = "tigris"
bucket = "your-private-auth-bucket"
region = "auto"
endpoint_url = "https://t3.storage.dev"
prefix = "eval-auth"
```

Supply the dedicated auth variables through your secret manager. Install the
host Codex pin, configure Modal access, and obtain a fresh remote-backed login:

```console
npm install --global @openai/codex@0.154.0
uvx --from modal==1.5.4 modal setup
tetrabench auth login --profile codex-cloud --agent codex \
  --backend ~/.config/tetrabench/cloud-auth-backend.toml
tetrabench auth status --profile codex-cloud --online
```

Save `codex-cloud-run.toml` in your project:

```toml
[harness]
name = "codex"
version = "0.154.0"
model = "openai/gpt-6-astra"

[harness.auth]
mode = "chatgpt_oauth"
reference = { kind = "profile", profile = "codex-cloud" }
```

Inject the controller's separate artifact credentials as `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, plus the auth-backend variables, into the configure process.
Preview first; `--write` prompts before creating the named Secret/environment:

```console
tetrabench controller configure --profile cloud-oauth --auth-profile codex-cloud \
  --harness ./codex-cloud-run.toml --create-environment \
  --env AWS_ACCESS_KEY_ID --env AWS_SECRET_ACCESS_KEY \
  --env TETRABENCH_AUTH_ACCESS_KEY_ID --env TETRABENCH_AUTH_SECRET_ACCESS_KEY
tetrabench controller configure --profile cloud-oauth --auth-profile codex-cloud \
  --harness ./codex-cloud-run.toml --create-environment --write \
  --env AWS_ACCESS_KEY_ID --env AWS_SECRET_ACCESS_KEY \
  --env TETRABENCH_AUTH_ACCESS_KEY_ID --env TETRABENCH_AUTH_SECRET_ACCESS_KEY
```

From the submitter's artifact credential environment, with the auth-backend variables
still supplied, deploy and run your network-enabled task (`[harbor] concurrency = 1`):

```console
tetrabench controller deploy --profile cloud-oauth
tetrabench doctor --profile cloud-oauth --harness ./codex-cloud-run.toml --online
tetrabench run example --profile cloud-oauth --harness ./codex-cloud-run.toml --wait --run-id oauth-cloud
tetrabench result oauth-cloud
```

The helper transports only selected auth profiles and explicit env names. It
omits the host runtime path; the actual controller selects its private runtime
directory. Manual runtime paths or JSON transport are unnecessary. Explicit custom
runtime paths remain supported but must be absolute, without `~` or env expansion.
`approved_private_backend` records approval, not proof of privacy. Configure checks
privacy before transfer, not login readiness. Auth storage never falls back to the
artifact credential chain. Optional session credentials require their own explicit
env references and `--env` selections. See [controller configuration](#controller-deployment)
for updates and partial failures.

The backend needs privacy-read permissions as well as scoped object access:

- AWS: `GetBucketPublicAccessBlock`, `GetBucketPolicyStatus`, `GetBucketAcl`,
  `GetBucketLocation`, scoped `GetObject`/`PutObject`, and KMS permissions when
  selecting `kms_key_id`. All public-access blocks and owner-only ACLs are required.
- Tigris: `GetBucketLocation`, `GetBucketPolicyStatus`, `GetBucketAcl`,
  `GetObjectAcl`, and scoped `GetObject`/`PutObject`/`PutObjectAcl`. Unknown or
  unsupported privacy metadata blocks transfer. Managed encryption alone does
  not prove private access. The admission topology restrictions still apply.

For a shared Tigris organization, `trust_organization_admins` defaults to `false`.
Set it to `true` under the S3 backend only after explicitly approving that
organization's administrators: it permits the native
`https://groups.tigris.dev/org/admins` full-control ACL grant alongside the owner.
That is not a public group; public and other unapproved grants remain rejected.
This opt-in neither provisions a backend nor replaces its privacy checks.

Native clients own token refresh. The session framework serializes each refresh
lineage, requires private native write-back and proof that the previous consumer
stopped, and leaves ambiguous claims blocked. There is no timeout takeover,
parallel copying, or automatic reuse after uncertain refresh. Use a separate
login for concurrent work. `auth reseed --profile NAME` requires a fresh native
login and retained run/physical-stop evidence for a claimed predecessor; it does
not stop compute for you. Managed profiles select the next generation, while fixed
profiles must explicitly target it. Future authoring profile references resolve
the new generation; old sealed runs never change. `auth logout --profile NAME` asks
before removing that eval login (`--yes` for JSON); provider revocation is not
implied.

Normal deployed OAuth delivery passed for Codex, OpenCode, and Pi using fresh
independent logins and the approved backend. Separate pinned-native refresh proofs
changed both access and refresh credentials, persisted them, and verified their
use by fresh actual Modal controllers, each with reward `1` and clean shutdown.
This does not prove every refresh-failure recovery scenario. See the scoped backend
and artifact evidence [below](#testing-and-limitations). Keep native logs private:
excluding known credential files is not universal secret scrubbing.

### Model inspection and adoption

```console
tetrabench models inspect --harness ./codex-run.toml --json
# Explicitly permit use of the declared key for native metadata:
tetrabench models inspect --harness ./codex-run.toml --allow-authenticated-read --json
# Use a control/choice actually reported as supported:
tetrabench models adopt --harness ./codex-run.toml --control effort --select high --allow-authenticated-read
# Repeat with --write only after reviewing the preview.
```

Inspection automatically collects installed native metadata; users do not need
to capture JSON or write a Python collector. The callable
`collect_installed(config, base=...)` provides the same acquisition for API
consumers. Metadata comes from OpenCode's provider interface, Codex's app-server
`model/list`, Claude's supported-model interface, or Pi's model registry/runtime.
Claude metadata uses exact CLI/SDK pairs: preferred 2.1.269 / 0.3.269 and historical
2.1.267 / 0.3.267, distinct from the tetrabench package version. Old snapshots are
not rewritten or reused for a different CLI pin.

The matching CLI must be installed on the inspection host's `PATH` with required
Node tooling. `--native-modules DIRECTORY` selects an existing `node_modules`
tree; `--node PATH` selects Node. Inspection never installs an absent package.
The source-only consumer installer in [Development](../README.md#development)
is for tests, not an end-user prerequisite.

Default inspection uses isolated home/cwd, sealed native config/resources, and
Linux PID/network namespaces. It makes no inference request and reads no ambient
auth store. It may copy native **model metadata caches**; `--no-native-cache`
disables that reuse. Missing binaries, pin mismatches, or unavailable namespace
support produce unavailable evidence rather than an unsafe fallback.

`--refresh` permits public unauthenticated metadata reads for OpenCode/Pi; it does
not authorize account access. For Codex/Claude, that CLI flag alone does not
perform authenticated refresh. Add `--allow-authenticated-read` to `models inspect`
or `models adopt` to acquire the file's explicit `harness.auth` reference and
verify its native auth mode before collection. API keys and Claude setup tokens
use the declared environment variable without an auth profile file. OAuth uses
the matching private profile (`--auth-config FILE` selects it), a serialized
claim, native refresh write-back, and private CLI operation evidence for recovery.
No browser login starts implicitly, and authenticated collection does not copy
global model caches. A missing key, profile, or matching native status is an error.

This permission makes credentials available; it does not force an HTTP request.
Native control metadata may still be bundled, cached, or heuristic, not a remote
entitlement check. In inspection JSON, `capability.identity.auth_mode` is the configured
mode; `authentication.observed.mode` records native status only when observed.
`authentication.provided` does not prove server acceptance. Account verification,
account-capability checks, and provider metadata fetches retain their explicit
unverified/not-observed labels. Known credential literals are refused in output.
Config that can execute plugins/hooks/helpers requires `--allow-config-execution`
for offline inspection and is refused for authenticated collection. Session-bearing
configs are also refused; use a separate metadata-only configuration. The callable
API requires both `allow_authenticated_read=True` and an acquired `NativeRuntime`.

Offline inspection accepts authoring profile references without reading authority.
For a profile reference, `models adopt` requires `--allow-authenticated-read`, even
for preview; adoption writes a concrete generation-bound `native_session` and
capability snapshot. Login/replacement never rewrites that snapshot. Ordinary
API-key or concrete-reference offline adoption is unchanged.

`adopt` collects afresh, previews the change, and writes only with `--write`, after
rechecking the source configuration and referenced resource bytes.
Choose `--select NAME` or a supported numeric `--budget N`; native normalization
requires `--accept-normalization`. It writes the actual option/native-config
binding and a `capability_snapshot` tied to harness version, model, route, auth
reference, and resulting config digest. Later config drift invalidates that
snapshot. It does not silently clamp a choice or impose one effort enum across
providers.

Read each control's status and provenance. `unknown`, `unsupported`, and
`unavailable` differ; a known native choice can still have an unknown endpoint or
provider route. Adoption requires sufficient evidence. `inference_validated` is
false for metadata inspection, and no all-model/all-provider execution guarantee
is implied. A successful API-key eval does not validate every metadata choice or
subscription entitlement.

### Resource bundles and session controls

At the current pins, `[[harness.resources]]` selects a UTF-8 file or directory
relative to the declaring TOML. Tetrabench seals it separately from task bytes,
with a destination, digest, and normalized mode. Limits are 128 files, 128 KiB per
file, and 512 KiB total; symlinks, special files, unsupported binary types, and
credential filenames are rejected. Supported referenced local native paths are
also bundled; remote execution never depends on your host home paths.

For example, create `rules.md` alongside this `opencode-resources.toml`:

```toml
[harness]
name = "opencode"
version = "1.18.30"
model = "openai/gpt-5"
discovery = "isolated"

[harness.auth]
mode = "api_key"
reference = { kind = "env", name = "MODEL_API_KEY" }

[[harness.resources]]
source = "rules.md"
destination = "prompts/rules.md"

[harness.native_config]
format = "json"
text = '{"instructions":["resource:prompts/rules.md"]}'
```

Use `directory = true` for a selected tree, such as a `skills/example` bundle
containing `SKILL.md`. `resource:DESTINATION` aliases are rewritten to sandbox
resource paths. Native configs, instructions, roles, MCP config, and supported
session seeds remain user-owned inputs; do not bundle an entire home. Never
include auth stores, `.env`, private keys, or credential-bearing transcripts.
Filename/content checks do not make arbitrary user code or transcripts safe to
publish.

`discovery = "native"` preserves native discovery; `"isolated"` suppresses the
supported automatic discovery surfaces for OpenCode, Claude Code, and Pi. Codex
rejects `"isolated"`; use its native config. Explicit resources still use each
client's native config/user-data paths, independently of the private credential
store. Native user-data ownership stays with OpenCode's XDG directories,
Codex's `CODEX_HOME`, Claude's `CLAUDE_CONFIG_DIR`, and Pi's
`PI_CODING_AGENT_DIR`; do not override these with global credential directories.
This policy is distinct from `models inspect`'s network isolation.

Under `[harness.session]`, `resume_trajectory = true` uses Harbor's trajectory lifecycle.
`load_trajectory = "resource:sessions/FILE.jsonl"` must name a sealed seed;
loading is supported for Codex, Claude Code, and Pi, not OpenCode. Pi requires
native JSONL rather than ATIF. Use the selected harness's native session format
and naming, not an invented common transcript. Resume conflicts with disabled
session persistence. Pi's `session_id` also conflicts with Harbor continuation.
These controls do not resume another run's auth session or prove fact retention
across multiple compactions.

### Testing and limitations

The onboarding candidate passed 114 offline native cases, including preferred
Claude 2.1.269, and 37 installed CLI invocations with transport/terminal doubles.
The latter used a dirty wheel, synthetic Docker execution, and graph-only deploy;
live onboarding and 2.1.269 checks remain pending. Integration/resource fixes are
implemented and full validation is underway, as tracked in the plan.
The older evidence below does not close those gates.

The source candidate passed API-key eval flows through the public CLI for
OpenCode, Codex, Claude Code, and Pi. Subscription evidence combines the normal
smokes at `55dc637` with installed Claude and renewal/successor proofs at `2d1ca9a`
on 2026-09-11. Neither candidate is a published 0.3.0 artifact:

| Harness | Normal subscription smoke | Refresh or renewal acceptance |
| --- | --- | --- |
| Codex 0.154.0 | Astra, reward `1` | Access and refresh changed, persisted; fresh Modal controller verified claimed/staged bytes and earned reward `1` |
| OpenCode 1.18.30 | Astra, reward `1` | Access and refresh changed, persisted; fresh Modal controller verified claimed/staged bytes and earned reward `1` |
| Pi 0.85.1 | Astra, reward `1` | Access and refresh changed, persisted; fresh Modal controller verified claimed/staged bytes and earned reward `1` |
| Claude Code 2.1.267 | Setup token; exact `[1m]` applied, response `claude-opus-5`, reward `1` | Natural setup-token expiry/renewal is user-owned and was not observed |

Owners stopped and two empty child sweeps passed; OAuth profiles returned
ready/unowned after private write-back. Initial normal-smoke scans covered 91
objects; Claude's later scan covered 35 objects/17 inventory entries. The final
successor-phase scan covered 65 objects, including three binary objects, with zero
known credential/private-auth-resource matches in literal, base64, and URL-encoded
forms. Scans are phase-local: the last parent held current Codex/OpenCode values
and initial/renewed Pi values, not a historical credential bank across parents.
Unknown transformations and workload-emitted secrets are not covered. These
short tasks establish neither subscription long-context retention nor actual
subscription charges/quota; a native zero cost is not proof of free usage.

The dedicated Tigris backend passed scoped S3SessionStore CAS/private ACL checks
under the approved single-person organization-admin opt-in. A later auth-store-key
HEAD against a known retained artifact returned 403 without reading its payload.
`GetBucketPolicyStatus` still revealed an unrelated bucket's public/private bit
despite explicit deny, an accepted metadata limitation. These probes do not prove
general IAM isolation, effective encryption, or live AWS behavior; the public
trust default remains false.

Bounded continuation tests separately established:

- Two Codex V2 compaction boundaries and standalone OpenAI opaque-state
  checkpoints succeeded.
- OpenCode crossed three text-summary boundaries and Pi crossed two, with fact
  retention in those tests.
- Claude crossed two native text boundaries and continued to grade `1`, but read
  back its transcript in violation of the verification protocol. This does not
  establish clean, within-protocol fact retention.

Those continuation tests used reduced verification thresholds, not stock-window
performance settings. Claude's `[1m]` is a native
[extended-context model suffix](https://code.claude.com/docs/en/model-config#extended-context).
Applied selection and catalog metadata prove neither entitlement nor context
capacity; catalog omission does not prove server rejection. The live setup-token
smoke preserved the requested `anthropic/claude-opus-5[1m]` selector and reported
native usage `contextWindow = 1000000`. It did not exercise a million-token input
or establish million-token retention or universal entitlement. Local error-handling
tests do not establish natural setup-token expiry/renewal; no token-aging experiment
is required. Earlier hosted CI passed at `5382b78`, before the onboarding changes.
Fresh journey/live validation, independent review, current CI, exact-release-artifact
validation, and publication remain open. The user accepted the observed native
mechanism/continuation/transcript
recovery for 0.3.0. Claude summary-only/clean within-protocol retention remains
unproven; acceptance does not establish that the summaries retained the facts.
Current blockers and retained provenance belong in the [project record](../IMPLEMENTATION_PLAN.md#native-fidelity-and-authentication-working-record).
None of these tests guarantees all models, routes, or future native versions.

## Running an evaluation

```text
tetrabench run SECTION [--engine docker|modal] [--profile PROFILE]
    [--harness FILE] [--run-id RUN_ID] [--wait | --detach] [--output DIRECTORY] [--json]
```

Both engines execute sealed task fixtures and explicitly configured context.
Engine selection comes from configuration unless overridden. Generated projects
select Docker; `--run-id` is optional, otherwise tetrabench generates one.

| Engine | Default | `--wait` | `--detach` | Output |
| --- | --- | --- | --- | --- |
| `docker` | Attached, waits | Same attached execution | Rejected | New local directory |
| `modal` | Detached submission | Observe remote result | Return after submission | Configured S3/Tigris storage |

`--wait` and `--detach` are mutually exclusive. Modal rejects `--output`; use
`artifacts pull` after success. Unsupported modes fail before provider mutation.
Only the positive flags are supported: `--no-wait` and `--no-detach` are rejected.

### Local execution

For Docker, `--output` defaults to `./RUN_ID`. Harbor validates each selected
task before tetrabench creates the output directory.

The output path must not exist. Tetrabench creates it with mode `0700` and keeps
it after every success, failure, or interruption. Failed setup after reservation
leaves inspectable partial evidence rather than deleting it. Ctrl-C exits `130`.
Other local setup errors exit `2`. A completed Harbor outcome exits
`0` for success and `1` for failed or cancelled work.

The report contains Harbor's outcome, native job path, and canonical section
summary. Binary sections report exact pass count, sample count, and pass rate.
Numeric sections report their aggregate reward. A successful Harbor outcome
means execution completed without trial errors, not that every task passed its
verifier. Check the reward or pass rate separately.

### Cost reports

Run/result output includes costs when evidence is available. JSON `costs` has
separate `model`, `auxiliary`, and `infrastructure` totals plus per-scope
`evidence`. Each amount is a decimal USD string or `null`; coverage is `complete`,
`partial`, or `unknown`. Sources distinguish `harness_reported`, `estimate`,
`provider_reported`, and `unknown`; source artifact paths and requested/observed
models preserve provenance. Harness and estimated amounts are not a reconciled
provider invoice. Missing evidence stays unknown, never silently zero.

Pi may report zero for an unpriced custom model. When pricing is absent or
unverified, tetrabench excludes that zero from `amount_usd` and known
subtotals, retains the raw reported subtotal in `reported_amount_usd`, and adds
an unpriced-usage limitation. A wholly unpriced scope has `amount_usd = null`
and unknown coverage. Explicit complete model pricing can distinguish a
configured zero from Pi's default zero; it still does not establish provider
billing. Pi's `message_end` stream alone excludes compaction and branch-summary
costs; the cost reader supplements it with eligible native session/summary records.

Known native OpenCode text-summary and Pi compaction/branch-summary costs enter
the auxiliary subtotal when their native records are available. Matching stream,
session/database, and Harbor aggregate records are alternatives, not additive
charges. Imported or pre-run Pi entries are excluded. Unpriced zero stays unknown;
coverage remains partial and does not include all opaque OpenAI compaction or
background calls. Native/catalog-priced costs are not provider settlement.

If Claude Code's raw result stream is unavailable, its Harbor `cost_usd`
aggregate remains `harness_reported` with a limitation that the amount may be
native-reported or estimated. Absence of the raw stream does not prove how the
price was obtained. Codex's Harbor aggregate fallback is labeled `estimate`.

An aggregate and its per-call records are alternative evidence for the same
scope, not additive charges. Auxiliary observations included in a model subtotal
are not counted again. The runner does not collect infrastructure billing or
perform live pricing lookups. Older results may have no cost supplement. Cost
evidence does not change verifier rewards, and native budget options do not
provide a tetrabench-wide hard cap.

## Controller deployment

`tetrabench controller info --profile PROFILE` is local and read-only. It shows
the exact Modal App, Function, environment, Volume, Secret, and controller-root
names selected by the profile. JSON output also includes the timeout and pinned
versions. It does not resolve a wheel, fetch from PyPI, or contact Modal.

`controller deploy` shows the same contract and asks before calling Modal.
`--yes` skips confirmation. `controller deploy --json` requires it. Tetrabench
never reads or prints Secret values.

Deploy from a non-editable wheel installation. Deployment automatically resolves
the original local wheel when the installer's PEP 610 metadata retains both its
location and SHA-256. For unpublished wheels, use the
[hash-preserving development install](../README.md#development), which supplies
an absolute `file://` URL with a `#sha256=` fragment computed from the wheel.
A plain wheel path can leave `archive_info` empty in uv 0.11.21. An environment
override cannot substitute for the missing original digest.

If the original artifact or its digest is unavailable, deployment fetches the
exact installed version from PyPI and
verifies its size and SHA-256. It also checks the wheel against the installed
package. No manual wheel environment variable is needed for normal deployment.
For an unpublished development build, retain the wheel at the path used to
install it. A live installed-wheel Modal smoke used this automatic local-artifact
path and completed one Oracle task with reward `1` and no surviving run-owned
compute. That smoke does not establish public PyPI installation.

The controller image uses that wheel and its bundled dependency lock, without
requiring a source checkout. Successful deployment reports the wheel's SHA-256
in human output and as `wheel_sha256` in JSON.
`controller info` determines the exact environment where the named Secret must
exist. That namespace includes the profile and package version. On first deploy,
use `controller configure --profile NAME --env NAME ... --create-environment`
for an offline preview. Add `--write` to confirm creation; JSON writes require
`--yes`. Include `--harness FILE` for its credential names and repeat
`--auth-profile NAME` for each selected remote OAuth login. All required artifact,
auth-backend, and model env names must be explicitly selected; values are read only
for the confirmed write. See the [API-key example](../README.md#detached-modal-runs)
or [remote OAuth journey](#remote-private-auth-state).

Configuration creates a Secret by default; `--update --write` uses Modal's merge
update. Unrelated keys, including stale credentials, remain. Selected auth-profile
JSON replaces that one variable, not every other Secret key. Prefer separate run
profiles/Secrets for different billing modes. Configure does not deploy, remove
stale keys, refresh running containers, or establish credential/runtime readiness.
Run `controller deploy` separately. Environment creation and Secret write are
separate operations; partial/unknown state is printed with `ok = false` and exit 2.
Inspect that state before retrying. There is no application retry or rollback;
the native SDK may retry transport with its idempotency key. Use one writer per
Secret. No bucket, key, IAM, or account provisioning is performed.

The deployed Function has zero retries, a 24-hour timeout, and the selected
Volume. Submission calls it by name with canonical invocation bytes and their
digest. Deployment remains `controller info` / `controller deploy`; there is no
general engine deployment command.

## Detached lifecycle

`tetrabench run SECTION --engine modal --profile PROFILE --detach` seals every
selected task and explicit context file before provider construction. It
publishes immutable content and request records before creating or observing
admission. After admission, it spawns the deployed controller and stores the
returned FunctionCall ID in a local receipt. `submit SECTION --profile PROFILE`
is a compatibility alias for this command.

Use `--wait` instead of `--detach` to poll results until terminal proof, conflict,
or failed/cancelled admission. Ctrl-C exits `130` and detaches the observer; it
does not cancel remote compute. Use `cancel RUN_ID` explicitly to stop it.

### Run references

New Docker and Modal runs record their engine and location in local user state
(`~/.local/state/tetrabench/run-references` on Linux, or under `XDG_STATE_HOME`).
`status`, `result`, `cancel`, `recover`, `artifacts pull`, and `artifacts verify`
use that reference to select the original engine, storage, and controller without
reading the current project or profile. They work from another directory, even if the
original project configuration has changed or vanished. Neither `--profile` nor
`--environment` can rebind a valid recorded run. Keep Docker output at its
recorded path. Remote operations still need current provider credentials.

For old remote runs, or when the reference is missing or unreadable, use
`--profile PROFILE` from a project with the original storage configuration.
`result` reads storage; `status` can also inspect recorded Modal call IDs.
Legacy `cancel` and `recover` additionally require the original Modal namespace:

```console
tetrabench result OLD_RUN_ID --profile cloud
tetrabench status OLD_RUN_ID --profile cloud
tetrabench cancel OLD_RUN_ID --profile cloud --environment ORIGINAL_NAMESPACE
# Or recover a stopped controller:
tetrabench recover OLD_RUN_ID --profile cloud --environment ORIGINAL_NAMESPACE
```

Older records did not persist that namespace, so tetrabench cannot recover or
guess it. Obtain it from the original deployment, not the current version's
`controller info`. Without it, cancellation/recovery refuses mutation. This
fallback does not reconstruct missing Docker references.
`runs --remote --profile PROFILE` also uses project/profile configuration.

The local receipt is a recovery cache, not execution authority. Admission in S3
owns controller claims and cancellation intent. Immutable terminal records own
final results. Conflicting visible records dominate every report.

`tetrabench status RUN_ID` reads local execution evidence for Docker. For Modal,
it combines S3 state, the local receipt, and Modal call inspection. A provider
inspection error does not prove that a controller has stopped.

`tetrabench result RUN_ID` validates native Harbor files for Docker. For Modal,
it reads S3 only and does not require a submission receipt or construct a Modal
client. Routine remote `status`, `result`, and `runs` validate small authority
records and bindings without fetching every sealed input or native artifact.
`result` also fetches and verifies the bounded controller-result summary.
Remote result JSON (including entries in `runs`) reports:

| Field | Meaning |
| --- | --- |
| `verification_level = "none"` | No validated record/summary level established, for example unknown or conflicting work |
| `verification_level = "records"` | Authority records validated; no controller summary verified |
| `verification_level = "summary"` | Authority records and the controller summary validated |
| `payload_integrity = "unchecked"` | This read did not verify all input/artifact bytes, even with a successful outcome |

A missing or oversized controller summary can leave a terminal result with
`summary_status = "unavailable"` and record-level verification. Invalid summary
bytes or bindings produce conflict. Legacy summaries can be validated while
their reward summary remains `legacy_unavailable`. Run `artifacts verify` to
audit complete remote content; ordinary result reads do not remember an earlier
audit as a permanent integrity guarantee. Docker reports use their local native
evidence contract rather than these remote fields. Result states and exits are:

| State | Exit |
| --- | --- |
| successful or nonterminal | `0` |
| failed/cancelled outcome or admission; local failed/interrupted execution | `1` |
| invalid configuration, missing local output, or provider request failure | `2` |
| conflicting authority | `3` |
| unknown | `4` |

Validated terminal outcomes take precedence over stale admission state. A
successful result does not by itself prove that child compute has been cleaned
up.

`status` exits `0` for non-conflicting reports, including failed or unknown work;
use `result` for outcome-sensitive exit codes.

`tetrabench runs` lists local references and submission receipts.
`runs --remote --profile PROFILE` requires a profile and derives
run IDs from validated remote record keys; malformed keys make the command exit
`3`, as do conflicting run records. There is no remote index or tetrabench run
database.

## Cancellation and recovery

`tetrabench cancel RUN_ID` asks before mutation. For a running Docker job, it
verifies the recorded owner and writes a durable cancellation request. It sends
no process signal. The owner's event loop handles this request and Ctrl-C
through one asynchronous cancellation path, without repeatedly cancelling
teardown.

Docker reports cleanup complete only after validating the original output and
owner-lock identities, an orderly-stop record from that owner, and two empty
exact-owned container inventories on the original Docker daemon. Active
run-scoped Compose clients block cleanup. A hard-killed owner may leave an
unlocked file, but that alone never proves orderly shutdown or cleanup.

`cancel` can remove exact-owned remaining containers after the owner and Compose
clients stop. Missing orderly-stop evidence still leaves cleanup unproven even
if those containers are gone. Changed Docker context or failed inspection also
cannot prove cleanup. Select the original Docker context before retrying. This
covers containers, not shared images or user-declared persistent volumes.

`cancel` exits `3` while cleanup is incomplete. `status RUN_ID --json` and
`result RUN_ID --json` report `cleanup_complete` separately from the native
result. Docker recovery is unsupported; inspect the retained output and start a
new run.

For Modal, prepared work advances directly to cancelled. Running work records
durable cancellation intent and keeps the owner call ID. The service cancels
and polls that call, then sweeps run-scoped Harbor children until two consecutive
observations are empty. Run `cancel` again to resume interrupted cleanup. JSON
cancellation requires `--yes`.

`tetrabench recover RUN_ID` also asks before mutation. It refuses active or
inspection-unknown owners. After Modal proves the owner stopped, recovery enters
the durable `recovering` state, sweeps stale children, clears the old owner, and
spawns a successor. On an already terminal run, recovery only cleans remaining
children and does not spawn. Several callers may race to spawn after handoff,
but only the fresh admission claimant can enter Harbor. JSON recovery requires
`--yes`.
Cancellation and recovery exit `3` when cleanup is incomplete. Actual
provider-initiated preemption remains unproven; live evidence covers forced
interruption and recovery.

## Remote artifacts

`tetrabench artifacts verify RUN_ID [--profile PROFILE] [--json]` streams and
hashes all sealed input and artifact objects bound to an authoritative Modal
terminal, including failed or cancelled terminals. It deduplicates identical
content descriptors, checks size and SHA-256, and rechecks terminal authority
after reading. It writes no local files and performs no provider mutation.
Conflicting records, no terminal, or inventories outside the audit limits are
refused. Docker does not support this remote audit.

```console
tetrabench artifacts verify first-run --json
```

The report separates `missing` from `corrupt` objects and reports reference
counts, unique object/byte totals, and verified counts. Human output includes
`Audit: STATE`, verified/total objects and bytes, and `Missing: N; corrupt: M`.
It lists each missing or corrupt object's key, expected size, and SHA-256, plus
any refusal reasons, for both recorded-run and legacy-profile routing.
States are `verified` (exit `0`), `failed` or `refused` (exit `3`).
Configuration/provider failures exit
`2`. Verification describes bytes read during that operation, not future storage
availability. Publication and downloads retain their own content checks.

`tetrabench artifacts pull RUN_ID OUTPUT_DIR` uses a recorded Modal reference;
`--profile PROFILE` is the legacy remote fallback. It accepts only a
successful, validated terminal. Failed and cancelled terminals remain available
through `result`. Tetrabench does not materialize them.

The destination must not exist. Tetrabench creates directories at mode `0700`
and files at `0600`, uses no-follow directory-relative writes, verifies each
object's size and SHA-256 while streaming, and fsyncs the tree. Defaults allow at
most 10,000 files, 64 MiB per file, and 1 GiB total. A failed pull keeps the
private partial directory as evidence. Result, listing, and pull commands never
call provider delete APIs.

Docker does not support `artifacts pull`. Its artifacts remain in the native
job directory reported by `run`, `status`, and `result`; the command does not
copy them elsewhere.

## Diagnostics

Execution and controller deployment require Linux and CPython 3.12, matching the
serialized Modal controller. `doctor` checks this offline along with installed
Harbor 0.22.0 and Modal 1.5.4. `run`/`submit`, controller deployment, and Modal
`cancel`/`recover` reject an unsupported runtime before provider mutation.
Execution preflight also precedes provider clients, image builds, and output
reservation.

If the active CLI uses another Python, reinstall with
`uv tool install --python 3.12 tetrabench --reinstall`, or use the
[hash-preserving local-wheel install](../README.md#development) for this checkout.
The 0.3.0 diagnostics use fixed public messages and identify the installed version
when available; they fall back to the unversioned install command when package
metadata is absent. A diagnostic naming 0.3.0 does not prove that release is on
PyPI. Do not change dependencies or bypass preflight to force an incompatible
serialized controller to run.

Read-only remote `result`, `status`, and `artifacts verify` do not invoke that
execution guard, so it does not block inspecting an existing run under another
Python version. This is not a claim that every dependency supports that Python;
the installed package must still import and run. Configuration/version inspection
is not subject to an import-time platform guard either.

Provider and preflight JSON errors include `code`, `operation`, `exception_type`,
`error_type`, and fixed actionable `error` text; missing-credential errors may
also list `credential_names`, never values. Generic configuration errors retain
their validation message and need not have a diagnostic code.

| Code | Action |
| --- | --- |
| `unsupported_python`, `unsupported_platform` | Use Linux with CPython 3.12. Install a released build or the local wheel described in [Development](../README.md#development). |
| `runtime_dependency_mismatch` | Restore the package's locked Harbor/Modal environment. |
| `missing_credentials` | Supply the declared variables or the provider's standard credential chain. |
| `authentication_required`, `authentication_expired` | Authenticate with Modal or renew the provider credentials. |
| `modal_environment_missing`, `modal_secret_missing`, `modal_resource_missing` | Use `controller info` with the same profile to locate the exact versioned environment and resource names. |
| `provider_permission_denied` | Check account, environment, and permissions for the operation. |
| `provider_request_failed` | Check availability/configuration and inspect run status before retrying a mutation. |

For an unpublished checkout, retain and install its local wheel; do not assume a
version named in a diagnostic is already available on PyPI.
