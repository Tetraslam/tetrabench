#!/bin/sh
set -eu

test ! -e /tests
test ! -e /opt/tetrabench-verifier-source
printf 'agent-tests-absent\n'

cd /workspace/repo
test "$(git rev-parse HEAD)" = 40cf7d08fd09619514bab16351e1e926fde8698c
test -z "$(git status --porcelain)"
git switch -c feature
printf 'print("hello, tetrabench")\n' > app.py
git add app.py
git -c commit.gpgsign=false -c user.name=Oracle \
  -c user.email=oracle@example.invalid commit -m 'update greeting'
forge submit-pr --base main --head feature --head-oid "$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
