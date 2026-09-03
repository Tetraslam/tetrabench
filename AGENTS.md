# Tetrabench Agent Contract

## Session Start

At the start of every session, read these files in order:

1. the applicable global `AGENTS.md`;
2. [README.md](README.md);
3. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md);
4. [NOTES.md](NOTES.md).

Read questionnaire-meme.png.

Inspect the repository state before editing. Apply the global Systems Design gate to pinned interfaces, lifecycle boundaries, canonical state, distributed mutation, destructive actions, and other high-blast-radius work.

## Planning Record

[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) is the canonical scope, decision, progress, and evidence record.

- Update its current state, checklists, evidence, and blockers periodically during work and before every session ends.
- Preserve completed and superseded information. Mark it completed or superseded instead of deleting it.
- Keep stable IDs stable. Add new IDs rather than renumbering old ones.
- Do not mark implementation complete without the stated acceptance evidence.
- Surface correctness-critical unknowns as `unproven`.

## Notes

[NOTES.md](NOTES.md) is append-only.

- Add dated, timestamped, and provenanced entries. Each entry is append-only immediately after creation.
- Correct an entry by appending a correction; never rewrite history.
- Promote durable decisions into the plan while retaining the originating note.
- Separate user-provided contracts, primary-source research, observations, and inference.

## User Documentation

[README.md](README.md) contains only verified user-facing behavior. Do not document planned commands, APIs, or capabilities as working. Keep design detail, progress, and unresolved evidence in the plan.

Edit existing human-facing documentation in place. Preserve its facts, intent, voice, structure, links, and caveats, and inspect the old/new diff for loss. Rewrite only when requested or structurally necessary.

## Change Discipline

- Once the user authorizes an eval or calibration budget, continue paid attempts within the implemented cap without asking again. Stop only on success, when the cap cannot fund the next required atomic shape, or when another recorded safety boundary blocks execution.
- Prefer native Harbor, Modal, S3, and Docker mechanisms over custom control planes.
- Do not add a custom run database.
- Never deliberately serialize credentials into plans, receipts, logs, fixtures, or committed configuration.
- Treat native Harbor logs and artifacts as sensitive private bytes; tetrabench guarantees only that it does not deliberately serialize credentials.
- Keep submitter and controller storage credentials separate. Do not give child agent or verifier sandboxes S3 publication credentials.
- Do not write controller code until E-019 proves child ID/tag observation through a supported Harbor v0.22.0 extension surface.
- Preserve unrelated changes and use the smallest reviewable mutation.
- Before ending work, run the strongest practical checks named by the current plan and record their evidence.
