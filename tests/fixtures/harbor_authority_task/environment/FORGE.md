# Forge interface

The main service has no forge filesystem or volume. Use only:

```console
forge submit-pr --base main --head feature --head-oid COMMIT_OID
```

The CLI sends canonical JSON requests to `POST http://forge:8080/transitions`.
Each accepted transition has exactly these fields:

```json
{"base":"main","head":"feature","head_oid":"40 lowercase hex characters","request_id":"unique replay key","schema_version":1,"type":"pull_request.opened or pull_request.submitted"}
```

The command opens and submits the pull request. The submitted transition validates
the complete state, appends the terminal event, revokes the run capability, and
seals the database in one transaction before the command returns. Harbor's later
collect hook can only publish that already-sealed state. Database finalization and
fail-closed atomic file publication are separate operations.
