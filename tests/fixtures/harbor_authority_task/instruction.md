Update `/workspace/repo/app.py` so it prints `hello, tetrabench`.

Work on branch `feature`, make one focused commit, then open a pull-request-like
transition from `feature` to `main` with:

```console
forge open-pr --base main --head feature --head-oid "$(git rev-parse HEAD)"
```

The forge API is documented at `/workspace/FORGE.md`. It is the only supported
way to reach forge state. Do not create local forge logs, result files, or
snapshots.
