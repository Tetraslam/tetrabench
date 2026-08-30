#!/bin/sh
set -eu
mkdir -p /logs/verifier
chmod 0700 /logs/verifier
python /tests/verify.py \
  --workspace /workspace \
  --contract /tests/contract.toml \
  --cases /tests/cases.toml \
  --diagnostics /logs/verifier/diagnostics.json \
  --reward /logs/verifier/reward.json
chmod 0755 /logs/verifier
chmod 0644 /logs/verifier/diagnostics.json /logs/verifier/reward.json
