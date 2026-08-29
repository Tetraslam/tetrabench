#!/bin/sh
set -eu
mkdir -p /logs/verifier
python /tests/verify.py \
  --workspace /workspace/repo \
  --forge-export /forge/export \
  --artifact-contract /tests/artifact_contract.json \
  --expected-initial /tests/expected_initial.json \
  --baked-marker /opt/tetrabench-verifier-source \
  --diagnostics /logs/verifier/diagnostics.json \
  --reward /logs/verifier/reward.txt
