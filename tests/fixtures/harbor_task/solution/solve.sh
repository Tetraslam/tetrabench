#!/bin/sh
set -eu
for name in \
  AWS_ACCESS_KEY_ID \
  AWS_ACCOUNT_ID \
  AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN \
  AWS_SECURITY_TOKEN \
  AWS_PROFILE \
  AWS_DEFAULT_PROFILE \
  AWS_SHARED_CREDENTIALS_FILE \
  AWS_CONFIG_FILE \
  AWS_WEB_IDENTITY_TOKEN_FILE \
  AWS_ROLE_ARN \
  AWS_ROLE_SESSION_NAME \
  AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
  AWS_CONTAINER_CREDENTIALS_FULL_URI \
  AWS_CONTAINER_AUTHORIZATION_TOKEN \
  AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE \
  AWS_CREDENTIAL_EXPIRATION \
  AWS_BEARER_TOKEN_BEDROCK \
  AWS_SDK_LOAD_CONFIG \
  BOTO_CONFIG \
  TIGRIS_ACCESS_KEY_ID \
  TIGRIS_SECRET_ACCESS_KEY \
  TIGRIS_STORAGE_ACCESS_KEY_ID \
  TIGRIS_STORAGE_SECRET_ACCESS_KEY \
  aws_alternate_credential \
  AwS_Mixed_Credential \
  tigris_alternate_credential \
  TiGrIs_Mixed_Credential \
  bOtO_cOnFiG \
  bOtOcOrE_TcP_KeEpAlIvE; do
  eval "value=\${$name-}"
  test "$value" = unavailable
done
printf 'credentials-unavailable\n' > /workspace/credential-boundary.txt
printf 'tetrabench\n' > /workspace/answer.txt
