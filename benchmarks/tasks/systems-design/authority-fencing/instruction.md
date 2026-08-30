# Implement authority fencing

Complete `/workspace/authority.py` so the CLI in `contract.toml` enforces every
gate named there. Use only the Python standard library and SQLite. Keep all
submitted work beneath `/workspace`.

The contract is authoritative for the inclusive logical-tick domain, exact
expiry, renewal, canonical success/rejection JSON, exit codes, terminal replay,
and rejection stability. Rejections must not change an existing database.

Run the public check with:

```console
python /workspace/test_public.py
```

Implement and prove the behavior. An architecture essay, generated reward file,
or claimed test result does not affect scoring.
