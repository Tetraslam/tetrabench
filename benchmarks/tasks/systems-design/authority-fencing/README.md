# authority-fencing

Status: local candidate. It is intentionally absent from `benchmarks/catalog.toml`.

The workspace contains a compact Python and SQLite lease queue. The public test
covers the documented CLI and one happy path. The separate verifier owns fixed
hidden concurrency, expiry, restart, stale-action, rollback, and idempotence
cases. It invokes only the submitted CLI under an unprivileged uid and computes
state independently from fresh SQLite databases.

`contract.toml` is the sole logical-clock definition. Ticks are integers from
its inclusive minimum through maximum. A lease is current only while
`now < deadline`; exact equality is expired. TTL is positive, and `now + ttl`
must fit the tick domain. Renewal uses `max(previous_deadline, now + ttl)`.

Every successful command exits `0`, writes one canonical JSON line to stdout,
and leaves stderr empty. Its exact schema is the `cli.output.success_schema` in
`contract.toml`; ordinary actions and `inspect` set `idempotent` to `false`.
Replaying the same terminal operation with the same worker and token succeeds
even after expiry, returns the unchanged terminal row, and sets `idempotent` to
`true`. Every semantic or argument rejection exits `2`, writes exactly the
canonical `cli.output.rejection_schema` to stderr, leaves stdout empty, and
preserves the existing database bytes and semantic state. Named precommit
faults exit `86` with both streams empty and roll back their transaction.

`initial_workspace_sha256` binds the canonical path-and-content manifest for
exactly `README.md`, `authority.py`, and `test_public.py`. The contract does not
claim to hash itself. `hidden_case_input_sha256` binds only the hidden case
inputs (scenario, seed, fault schedule, and gate mapping); the verifier derives
expected state and effect hashes from its independent model. Harbor's sealed
task context and exact verifier image copy bind the complete contract.

The bounded local admission tool runs the gold, no-op, six seeded gate mutants,
and eight exploit fixtures. Admissible proof mode requires a clean worktree and
index. It captures `HEAD` once and makes one private `git archive` snapshot;
that snapshot owns the task, verifier image context, matrix candidates, gold
solution, temporary project, source manifests, lockfile, built wheel, and
installed production CLI. One to three ordered production runs execute only the
absolute CLI from that private locked installation, but only exactly three can
be admissible. Successful one- and two-run executions are diagnostics with
`admissible=false`, `ok=false`, a nonzero exit, and no retained proof file.
Linux subreaper containment and one no-follow snapshot of each private native
output must complete before validation. Retained proof output requires every
ancestor to remain root- or current-euid-owned and not group/world writable,
except a root-owned sticky world-writable ancestor such as `/tmp`. The final
parent must remain current-euid-owned and not group/world writable. Its file
descriptor remains open through byte, metadata, visible-name, parent-fsync, and
final repeated checks. A same-UID process remains authoritative and may mutate
the file after return; post-return immutability is not promised.
Any dirty run remains non-admission with no retained proof file.

One local matrix and one real Harbor Docker oracle have passed. The runner
source exists only in the dirty candidate change, so no clean admissible run is
currently possible. The required three-run proof remains ungenerated and
unproven until after commit. Public repository source is inspectable and is not
contamination-resistant. The current hidden-test claim is only that Harbor
bakes hidden source into the separate verifier image and does not mount it into
the agent runtime. Two detached gold runs, detached no-op/mutant/exploit audits,
calibration, and catalog admission remain unproven.

The concurrent expiry claim is relation/property-based: the scheduler may choose
either seeded contender. Trusted verifier diagnostics and admission evidence
retain the actual winner and loser worker IDs, winning fence token, and durable
owner/token match result. Those nondeterministic identities stay outside the
expected hashes; the canonical winner/loser relation is the deterministic
oracle. Ordered effect traces apply only to deterministic schedules.
