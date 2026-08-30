FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG TETRABENCH_ADMISSION_BUILD_NONCE=harbor
LABEL dev.tetrabench.admission-build-nonce=$TETRABENCH_ADMISSION_BUILD_NONCE

COPY . /tests
RUN chmod 0700 /tests \
    && chmod 0500 /tests/test.sh /tests/verify.py /tests/model.py /tests/schedule.py \
    && chmod 0400 /tests/contract.toml /tests/cases.toml

WORKDIR /tests
