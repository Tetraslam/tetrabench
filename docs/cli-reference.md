# CLI reference

Tetrabench uses Rich for human output. `--json` writes one RFC 8785 canonical
JSON document followed by a newline. Errors go to stderr. Local configuration
and integrity errors keep their specific message. Caught Botocore and Modal
exceptions become `provider_error` with the fixed message `provider request
failed`, so provider-controlled details do not cross the CLI boundary.

## Local project and tasks

`tetrabench init DIRECTORY` creates a new local Docker project with one binary
starter task. The destination must not exist.

`tetrabench sections` lists the two catalog sections and their task counts.

`tetrabench task new SECTION TASK_ID` creates an unlisted task under
`benchmarks/tasks/`. `task validate FIXTURE` validates a sealed private copy and
does not call Docker or a provider. `task add SECTION TASK_ID FIXTURE` validates
the fixture, then appends it to the user-owned catalog. The catalog and its lock
must be outside the fixture tree.

`tetrabench plan SECTION` prints a secret-free plan and its digest. An empty
selection is valid but not runnable.

`tetrabench doctor` checks project configuration, catalog selection, section
READMEs, and explicit context. It does not construct a provider client unless
`--online` is present. Online mode calls only bucket and prefix read operations:
`HeadBucket`, `GetBucketLocation`, and a list limited to one key. It reports
whether the bucket topology is safe for mutable admission and never tests or
claims write access.

## Local execution

`tetrabench run SECTION --output DIRECTORY` requires local controller and Docker
execution after project and optional user-profile merging. Harbor validates each
selected task before tetrabench creates the output directory.

The output path must not exist. Tetrabench creates it with mode `0700` and keeps
it after every success, failure, or interruption. Failed setup after reservation
leaves inspectable partial evidence rather than deleting it. Ctrl-C exits `130`.
Other local setup errors exit `2`. A completed Harbor outcome exits
`0` for success and `1` for failed or cancelled work.

The report contains Harbor's outcome, native job path, and canonical section
summary. Binary sections report exact pass count, sample count, and pass rate.
Numeric sections report their aggregate reward.

## Controller deployment

`tetrabench controller info --profile PROFILE` is local and read-only. It shows
the exact Modal App, Function, environment, Volume, Secret, timeout, and
controller-root names selected by the profile.

`controller deploy` shows the same contract and asks before calling Modal.
`--yes` skips confirmation. `controller deploy --json` requires it. Tetrabench
never reads or prints Secret values.

The deployed Function has zero retries, a 24-hour timeout, and the selected
Volume. Submission calls the deployed Function by name with canonical
invocation bytes and their digest.

## Detached lifecycle

`tetrabench submit SECTION --profile PROFILE` seals every selected task and
explicit context file before provider construction. It publishes immutable
content and request records before creating or observing admission. After
admission, it spawns the deployed controller and stores the returned FunctionCall
ID in a local receipt.

The local receipt is a recovery cache, not execution authority. Admission in S3
owns controller claims and cancellation intent. Immutable terminal records own
final results. Conflicting visible records dominate every report.

`tetrabench status RUN_ID` combines S3 state, the local receipt, and Modal call
inspection. A provider inspection error does not prove that a controller has
stopped.

`tetrabench result RUN_ID --profile PROFILE` reads S3 only and does not require a
receipt or construct a Modal client. Its states and exits are:

| State | Exit |
| --- | --- |
| successful or nonterminal | `0` |
| failed or cancelled terminal | `1` |
| conflicting authority | `3` |
| unknown | `4` |

`tetrabench runs` lists local receipts. `runs --remote --profile PROFILE` derives
run IDs from validated remote record keys; malformed keys make the command exit
`3`. There is no remote index or tetrabench run database.

## Cancellation and recovery

`tetrabench cancel RUN_ID` asks before mutation. Prepared work advances directly
to cancelled. Running work records durable cancellation intent and keeps the
owner call ID. The service cancels and polls that call, then sweeps run-scoped
Harbor children until two consecutive observations are empty. Run `cancel` again
to resume interrupted cleanup. JSON cancellation requires `--yes`.

`tetrabench recover RUN_ID` also asks before mutation. It refuses active or
inspection-unknown owners. After Modal proves the owner stopped, recovery enters
the durable `recovering` state, sweeps stale children, clears the old owner, and
spawns a successor. Several callers may race to spawn after handoff, but only
the fresh admission claimant can enter Harbor. JSON recovery requires `--yes`.

## Remote artifacts

`tetrabench artifacts pull RUN_ID OUTPUT_DIR --profile PROFILE` accepts only a
successful, validated terminal. Failed and cancelled terminals remain available
through `result`. Tetrabench does not materialize them.

The destination must not exist. Tetrabench creates directories at mode `0700`
and files at `0600`, uses no-follow directory-relative writes, verifies each
object's size and SHA-256 while streaming, and fsyncs the tree. Defaults allow at
most 10,000 files, 64 MiB per file, and 1 GiB total. A failed pull keeps the
private partial directory as evidence. Result, listing, and pull commands never
call provider delete APIs.
