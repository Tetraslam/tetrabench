FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

COPY forge.py /opt/forge/forge.py
COPY initial_state.json /opt/forge/initial_state.json

RUN chmod 0555 /opt/forge/forge.py \
    && chmod 0444 /opt/forge/initial_state.json \
    && mkdir -p /forge

CMD ["python", "/opt/forge/forge.py", "serve", "--host", "0.0.0.0", "--port", "8080"]
