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

`tetrabench plan SECTION [--profile PROFILE] [--engine docker|modal]` prints a
secret-free plan and its digest. JSON mode emits the plan alone. An empty
selection is valid but not runnable.

`tetrabench doctor` checks project configuration, catalog selection, section
READMEs, and explicit context. It does not construct a provider client unless
`--online` is present. Online mode calls only bucket and prefix read operations:
`HeadBucket`, `GetBucketLocation`, and a list limited to one key. It reports
whether the bucket topology is safe for mutable admission and never tests or
claims write access.

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

`tetrabench agents [NAME] [--json]` lists the registered harnesses. JSON includes
package identity, supported native formats, option types/choices/defaults,
credential variables, version restrictions, and limitations. Only the four
listed adapters are registered; arbitrary agent import paths or shell arguments
are not accepted.

| Name | Native package | Native configuration | Supported options |
| --- | --- | --- | --- |
| `opencode` | `opencode-ai` | JSON | `variant`, `title` |
| `codex` | `@openai/codex` | TOML or JSON | `reasoning_effort`, `reasoning_summary`, `web_search` |
| `claude-code` | `@anthropic-ai/claude-code` | JSON | `max_turns`, `reasoning_effort`, `max_budget_usd`, `fallback_model`, `append_system_prompt`, `allowed_tools`, `disallowed_tools`, `permission_mode`, `max_thinking_tokens` |
| `pi` | `@earendil-works/pi-coding-agent` | JSON with `settings` and/or `models` objects | `thinking`, `model_api` |

Use `[harness]` in the project, `[profiles.NAME.harness]` in user configuration,
or a separate TOML file passed to `run --harness FILE`. That separate file must
contain only the `[harness]` table and its subtables, without `schema_version`,
engine settings, or task selection. A higher-precedence harness replaces the
whole lower-precedence harness, rather than merging individual options.
`[harbor]` still controls `attempts` and `concurrency`. Do not combine a harness
with legacy agent/model selection in the same configuration layer; a later
legacy agent/model override clears the controlled harness.

Required fields are `name`, exact `version = "x.y.z"`, and `model = "provider/model"`.
Controlled OpenCode accepts only `1.18.29`, the verified native CLI pin. Its run
command uses `--auto`, not Harbor 0.22's unsupported
`--dangerously-skip-permissions`. Other OpenCode pins, including `1.2.15`, fail
validation before installation. `agents opencode --json` reports this restriction
in `supported_versions`; the other adapters do not have that allowlist.
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
  `"native"` because role files can override model selection. These referenced
  files are not automatically bundled by `native_config`; provide them in the
  task environment. Native allows the harness's ancillary choices. Neither policy
  intercepts every possible model call or guarantees a universal billing cap.

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
version = "0.114.0"
model = "openai/gpt-5"

[harness.options]
reasoning_effort = "medium"
web_search = "disabled"

[harness.env]
OPENAI_API_KEY = "${MODEL_API_KEY}"

[harness.native_config]
format = "toml"
text = 'model_context_window = 100000'
```

`claude-run.toml`:

```toml
[harness]
name = "claude-code"
version = "2.1.63"
model = "anthropic/claude-sonnet-4-6"

[harness.options]
max_turns = 3

[harness.env]
ANTHROPIC_API_KEY = "${MODEL_API_KEY}"

[harness.native_config]
format = "json"
text = '{"permissions":{"allow":["Read"]}}'
```

`pi-run.toml`:

```toml
[harness]
name = "pi"
version = "0.74.0"
model = "openai/gpt-5"

[harness.options]
thinking = "high"

[harness.env]
OPENAI_API_KEY = "${MODEL_API_KEY}"

[harness.native_config]
format = "json"
text = '{"settings":{"compaction":{"enabled":true}}}'
```

For a native file, replace `text` with `path = "./settings.json"` (or a TOML path
for Codex). Pi's wrapper writes its `settings` and `models` objects as native
`settings.json` and `models.json`. A Pi base-URL environment reference requires
an explicit `model_api` option; this endpoint route cannot also supply a native
`models` object. Use `agents pi --json` for accepted API names.

Pi's native `models` configuration uses a bare agent environment variable name
for `apiKey`, such as `"TOKEN"`, not `"$TOKEN"` or `"${TOKEN}"`. The outer
`harness.env` still maps that name to `"${HOST_TOKEN}"`. For example, save this
portable custom-endpoint configuration as `pi-custom-run.toml`:

```toml
[harness]
name = "pi"
version = "0.74.0"
model = "custom/model"

[harness.env]
TOKEN = "${HOST_TOKEN}"

[harness.native_config]
format = "json"
text = '''
{"models":{"providers":{"custom":{
  "baseUrl":"https://example.test/v1",
  "api":"openai-responses",
  "apiKey":"TOKEN",
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

Controlled runs use declared environment references, not ambient interactive
logins or home configuration. Anonymous endpoints require explicit native
endpoint configuration. Native adapters still own execution. The agent artifact
`tetrabench-harness.json` records requested/observed CLI versions, options,
credential references, and effective native configs with resolved credential
values replaced by references. Treat the rest of the native artifacts as private.

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
billing. Pi's message stream also excludes compaction and branch-summary costs.

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
create the environment and Secret there, not in the default or an older
version's environment. The [first-deploy commands](../README.md#detached-modal-runs)
use Modal's native API to create the Secret from explicitly named process
environment variables, without a secrets file or secret values in argv. Inject
the controller credentials for that command, then use the submitter credentials
for deployment and runs. Model runs also need their model API variables in the
Secret. Tetrabench does not create credentials or automatically copy your local
environment into the Secret.

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
