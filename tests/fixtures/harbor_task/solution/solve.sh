#!/bin/sh
set -eu
for name in \
  AWS_ACCESS_KEY_ID \
  AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN \
  AWS_PROFILE \
  AWS_SHARED_CREDENTIALS_FILE \
  AWS_CONFIG_FILE; do
  eval "value=\${$name-}"
  test "$value" = unavailable
done
printf 'credentials-unavailable\n' > /workspace/credential-boundary.txt
printf 'tetrabench\n' > /workspace/answer.txt
