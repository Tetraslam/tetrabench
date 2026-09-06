# CLI reference

Tetrabench uses Rich for human output. Commands except `sections` accept
`--json`, which writes one RFC 8785 canonical JSON document followed by a newline
on success. Errors go to stderr. `plan` errors remain human-readable even with
`--json`. Local configuration and integrity errors keep their specific message.
Caught Botocore and Modal exceptions become `provider_error` with the fixed
message `provider request failed`, so provider-controlled details do not cross
the CLI boundary.

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
existing project, create its README and add a section table with `tasks = []`.

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
Precedence is project, selected user profile, then CLI `--engine`.

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

Tetrabench passes `agent_name` and `model_name` unchanged to Harbor 0.22. The
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

## Running an evaluation

```text
tetrabench run SECTION [--engine docker|modal] [--profile PROFILE]
    [--run-id RUN_ID] [--wait | --detach] [--output DIRECTORY] [--json]
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
`status`, `result`, `cancel`, `recover`, and `artifacts pull` use that reference
to select the original engine, storage, and controller without reading the
current project or profile. They work from another directory, even if the
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
client. Its states and exits are:

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
