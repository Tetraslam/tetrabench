FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260829T000000Z bookworm main' \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install --yes --no-install-recommends git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/*

COPY . /tests
RUN chmod 0700 /tests \
    && chmod 0500 /tests/test.sh /tests/verify.py \
    && printf 'baked-verifier-source\n' > /opt/tetrabench-verifier-source \
    && chmod 0444 /opt/tetrabench-verifier-source

WORKDIR /tests
