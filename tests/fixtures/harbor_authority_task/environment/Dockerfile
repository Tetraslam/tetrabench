FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260829T000000Z bookworm main' \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install --yes --no-install-recommends git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/*

COPY repo/app.py /opt/seed/app.py
COPY forge_cli.py /usr/local/bin/forge
COPY FORGE.md /workspace/FORGE.md

RUN chmod 0755 /usr/local/bin/forge \
    && mkdir -p /workspace/repo \
    && cp /opt/seed/app.py /workspace/repo/app.py \
    && cd /workspace/repo \
    && git init --initial-branch=main \
    && git config user.name Fixture \
    && git config user.email fixture@example.invalid \
    && git add app.py \
    && GIT_AUTHOR_DATE='2024-01-01T00:00:00+00:00' \
       GIT_COMMITTER_DATE='2024-01-01T00:00:00+00:00' \
       git commit --message initial \
    && rm -rf .git/hooks \
    && test "$(git rev-parse HEAD)" = 40cf7d08fd09619514bab16351e1e926fde8698c

WORKDIR /workspace/repo
