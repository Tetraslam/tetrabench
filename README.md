# tetrabench

tetrabench is in early implementation. Its verified surface is an importable Python 3.12 package with bounded, strict RFC 8785 canonical JSON and SHA-256 helpers. No execution command or configuration schema exists yet.

## Reviewed design scope

The accepted v1 design will orchestrate Harbor v0.22.0 evaluations through:

- a deployed Modal Function for detached cloud execution;
- Docker for explicit attached local development and execution;
- S3-compatible durable publication for AWS and Tigris;
- Harbor-native ATIF, results, and job artifacts.

The design assigns active execution to Modal FunctionCall, in-progress cloud files to a named Modal Volume, durable records to S3, and trace/result meaning to Harbor job files. Local state is a submission receipt/cache rather than a run database.

These statements describe reviewed scope, not working software. Implementation claims will be added only after their acceptance evidence passes.

## Project records

- [AGENTS.md](AGENTS.md) defines the repository workflow and documentation rules.
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) is the canonical scope, decision, progress, blocker, and evidence record.
- [NOTES.md](NOTES.md) is the append-only research and clarification log.

The implementation plan tracks deferred capabilities and unresolved live-smoke requirements. The notes retain their provenance. This README contains only verified user-facing status and behavior.
