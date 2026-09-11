"""Exercise an installed wheel's public console commands with synthetic boundaries.

Run with its Python 3.12: python -I tools/onboarding_journeys.py --work NEW_DIR.
The caller supplies an isolated HOME and a network namespace. No real credentials,
login, provider, inference or Docker execution is permitted. Native issuer state
and provider doubles are explicitly synthetic; this is not live acceptance.
"""

from __future__ import annotations

import argparse
import base64
import builtins
import hashlib
import io
import json
import os
import runpy
import subprocess  # nosec B404 -- observes argv-only synthetic issuer processes
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class JourneyFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise JourneyFailure(message)


class Journeys:
    def __init__(self, work: Path):
        self.work = work
        self.project = work / "project"
        self.cli = Path(sys.prefix) / "bin/tetrabench"
        self.events: list[dict] = []
        self.results: list[dict] = []
        self.secret_values: set[str] = set()
        self.secret_values.add("synthetic-provider-error")
        self.graphs = []
        self.s3_objects = {}
        self.s3_metadata = {}
        self.clients = []

    def scan(self, value: str) -> None:
        require(
            not any(secret in value for secret in self.secret_values),
            "credential in public command output",
        )

    def write(self, relative: str, value: str) -> Path:
        path = self.work / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o600)
        return path

    def observe_synthetic_credentials(self):
        """Test oracle only: retain issuer literals to check public output."""
        bodies = list(self.s3_objects.values())
        authority = self.work / "state/tetrabench/auth/profiles"
        if authority.exists():
            bodies.extend(p.read_bytes() for p in authority.rglob("*.json"))
        for body in bodies:
            try:
                document = json.loads(body)
            except (ValueError, UnicodeError):
                continue
            state = document.get("state", document)
            if not isinstance(state, dict) or not state.get("native"):
                continue
            native = json.loads(base64.b64decode(state["native"]))
            self.secret_values.update(
                native.get("tokens", {}).get(name, "")
                for name in ("access_token", "refresh_token", "id_token")
            )
            self.secret_values.discard("")

    def command(self, *args, expected=0):
        before = len(self.events)
        stdout, stderr = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        os.chdir(self.project if self.project.exists() else self.work)
        try:
            with (
                patch.object(sys, "argv", [str(self.cli), *args, "--json"]),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                try:
                    runpy.run_path(str(self.cli), run_name="__main__")
                    code = 0
                except SystemExit as error:
                    code = error.code or 0
        finally:
            os.chdir(previous)
        self.observe_synthetic_credentials()
        self.scan(stdout.getvalue())
        self.scan(stderr.getvalue())
        try:
            document = json.loads(stdout.getvalue())
        except ValueError:
            document = None
        record = {
            "argv": ["tetrabench", *args, "--json"],
            "exit_code": code,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "events": self.events[before:],
        }
        self.results.append(record)
        self.write(
            f"commands/{len(self.results):03d}.json",
            json.dumps(record, indent=2) + "\n",
        )
        require(
            code == expected,
            f"public command {args[:2]} exit {code}, expected {expected}",
        )
        return document

    def install_boundaries(self, stack: ExitStack):
        import boto3
        import modal

        # Only this external transport module is imported from beside the tool.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.onboarding_journey_fakes import ModalRpc, SyntheticS3

        self.rpc = ModalRpc(self.events)
        client = SimpleNamespace(stub=self.rpc, _snapshotted=False)

        class ClientFactory:
            def __call__(self):
                return client

            async def aio(self):
                return client

        async def native_client(cls, *args, **kwargs):
            return client

        stack.enter_context(patch.object(modal.Client, "from_env", ClientFactory()))
        stack.enter_context(
            patch("modal.client._Client.from_env", classmethod(native_client))
        )

        def s3(service, **kwargs):
            require(service == "s3", "unexpected provider client")
            obj = SyntheticS3(self.events, self.s3_objects, self.s3_metadata, **kwargs)
            self.clients.append(obj)
            self.events.append(
                {
                    "transport": "s3",
                    "operation": "client",
                    "explicit_auth_credentials": "aws_access_key_id" in kwargs,
                }
            )
            return obj

        stack.enter_context(patch.object(boto3, "client", s3))
        original_open = builtins.open

        def terminal(path, *args, **kwargs):
            # Native approval is simulated by the issuer. Keep the real child
            # process and private writeback; substitute only the controlling tty.
            if path == "/dev/tty":
                self.events.append(
                    {"transport": "native", "operation": "synthetic-terminal"}
                )
                return original_open(os.devnull, "r+b", buffering=0)
            return original_open(path, *args, **kwargs)

        stack.enter_context(patch.object(builtins, "open", terminal))
        original_popen = subprocess.Popen

        def process(argv, *args, **kwargs):
            rendered = [str(x) for x in argv]
            self.scan(" ".join(rendered))
            self.events.append(
                {
                    "transport": "process",
                    "operation": Path(rendered[0]).name,
                    "native_issuer": any(
                        x == str(self.work / "bin/codex") for x in rendered
                    ),
                    "version_probe": "--version" in rendered,
                }
            )
            return original_popen(argv, *args, **kwargs)

        stack.enter_context(patch.object(subprocess, "Popen", process))

        def deploy(app, **kwargs):
            self.graphs.append(app)
            self.rpc.functions.add(
                (kwargs.get("name", app.name), kwargs.get("environment_name"))
            )
            self.events.append(
                {
                    "transport": "modal",
                    "operation": "synthetic-deploy-graph",
                    "functions": list(app.registered_functions),
                    "environment": kwargs.get("environment_name"),
                }
            )
            return app

        stack.enter_context(patch.object(modal.App, "deploy", deploy))

        def local_boundary(prepared, output):
            self.events.append(
                {
                    "transport": "docker",
                    "operation": "synthetic-execution-boundary",
                    "engine": prepared.engine_kind,
                    "auth_mode": prepared.plan.harness.auth.mode,
                }
            )
            raise RuntimeError(
                "synthetic Docker execution boundary; no daemon/model invoked"
            )

        stack.enter_context(
            patch("tetrabench.engines.docker.run_prepared_local", local_boundary)
        )

    def inputs(self):
        import secrets

        for key in (
            "MODEL_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "TETRABENCH_AUTH_ACCESS_KEY_ID",
            "TETRABENCH_AUTH_SECRET_ACCESS_KEY",
            "UNSELECTED_VALUE",
        ):
            value = "synthetic-" + secrets.token_urlsafe(24)
            os.environ[key] = value
            self.secret_values.add(value)
        self.api = self.write(
            "project/api.toml",
            '[harness]\nname="codex"\nversion="0.154.0"\nmodel="openai/gpt-6-astra"\n'
            '[harness.auth]\nmode="api_key"\nreference={kind="env",name="MODEL_API_KEY"}\n',
        )
        self.alias = self.write(
            "project/oauth.toml",
            '[harness]\nname="codex"\nversion="0.154.0"\nmodel="openai/gpt-6-astra"\n'
            '[harness.auth]\nmode="chatgpt_oauth"\nreference={kind="profile",profile="journey-local"}\n',
        )
        self.write(
            "config/tetrabench/config.toml",
            'schema_version=1\n[profiles.cloud.engine]\nkind="modal"\n'
            '[profiles.cloud.engine.settings]\napp_name="journey"\nfunction_name="controller"\nsecret_name="journey-controller"\n'
            '[profiles.cloud.storage]\nprovider="tigris"\nbucket="journey-artifacts"\nregion="auto"\nprefix="journey"\n'
            '[profiles.cloud-api.engine]\nkind="modal"\n[profiles.cloud-api.engine.settings]\napp_name="journey-api"\n'
            'function_name="controller"\nsecret_name="journey-api-controller"\n'
            '[profiles.cloud-api.storage]\nprovider="tigris"\nbucket="journey-artifacts"\nregion="auto"\nprefix="api"\n',
        )

    def native_issuer(self):
        from tools.onboarding_journey_fakes import ISSUER

        path = self.write(
            "bin/codex",
            ISSUER.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1),
        )
        path.chmod(0o700)
        self.write("bin/node", "#!" + sys.executable + '\nprint("v24.21.0")\n').chmod(
            0o700
        )
        os.environ["PATH"] = str(path.parent) + os.pathsep + os.defpath
        return path

    def api_journey(self):
        report = self.command("doctor", "--harness", str(self.api))
        require(
            report["authentication"]["native_ready"]["status"] == "not_required",
            "API key required host native",
        )
        require(
            not self.results[-1]["events"], "offline API doctor used external boundary"
        )
        local = self.command("plan", "example", "--harness", str(self.api))
        require(local["execution"]["kind"] == "docker", "local default changed")
        self.command(
            "run",
            "example",
            "--harness",
            str(self.api),
            "--run-id",
            "api-local",
            expected=2,
        )
        require(
            any(e["transport"] == "docker" for e in self.results[-1]["events"]),
            "API key run did not reach execution boundary",
        )
        require(
            not any(e["transport"] == "native" for e in self.results[-1]["events"]),
            "API eval bootstrapped native auth",
        )
        key = os.environ.pop("MODEL_API_KEY")
        try:
            remote = self.command(
                "doctor", "--profile", "cloud", "--harness", str(self.api)
            )
            require(
                remote["authentication"]["configured"]["status"] == "ok",
                "remote API required submitter key",
            )
            require(
                not self.results[-1]["events"],
                "offline remote doctor contacted provider",
            )
            plan = self.command(
                "plan", "example", "--profile", "cloud", "--harness", str(self.api)
            )
            require(plan["execution"]["kind"] == "modal", "remote profile ignored")
        finally:
            os.environ["MODEL_API_KEY"] = key
        self.command(
            "controller",
            "configure",
            "--profile",
            "cloud-api",
            "--harness",
            str(self.api),
            "--env",
            "AWS_ACCESS_KEY_ID",
            "--env",
            "AWS_SECRET_ACCESS_KEY",
            "--env",
            "MODEL_API_KEY",
            "--create-environment",
        )
        self.command(
            "controller",
            "configure",
            "--profile",
            "cloud-api",
            "--harness",
            str(self.api),
            "--env",
            "AWS_ACCESS_KEY_ID",
            "--env",
            "AWS_SECRET_ACCESS_KEY",
            "--env",
            "MODEL_API_KEY",
            "--create-environment",
            "--write",
            "--yes",
        )
        self.command("controller", "deploy", "--profile", "cloud-api", "--yes")
        self.command(
            "doctor", "--profile", "cloud-api", "--harness", str(self.api), "--online"
        )
        self.command(
            "run",
            "example",
            "--profile",
            "cloud-api",
            "--harness",
            str(self.api),
            "--run-id",
            "api-remote",
            "--detach",
        )
        require(
            any(
                e.get("operation") == "FunctionMap" for e in self.results[-1]["events"]
            ),
            "remote public run did not reach real SDK spawn request",
        )
        require(
            not any(e["transport"] == "native" for e in self.events),
            "API-key journey used host native auth",
        )

    def local_oauth_journey(self):
        issuer = self.native_issuer()
        login = self.command(
            "auth",
            "login",
            "--profile",
            "journey-local",
            "--agent",
            "codex",
            "--executable",
            str(issuer),
        )
        require(
            login["generation"] == 1 and login["state"] == "ready",
            "bootstrap generation wrong",
        )
        self.command("auth", "status", "--profile", "journey-local")
        planned = self.command("plan", "example", "--harness", str(self.alias))
        require(
            planned["harness"]["auth"]["reference"]["generation"] == 1,
            "alias generation unresolved",
        )
        import tomlkit

        # Copy only the public plan's immutable auth reference into an input
        # document, as an operator can. Do not invent a reasoning capability or
        # resolve a generation through a callable onboarding helper.
        selected = planned["harness"]
        frozen = self.write(
            "project/frozen.toml",
            tomlkit.dumps(
                {
                    "harness": {
                        key: selected[key]
                        for key in ("name", "version", "model", "auth")
                    }
                }
            ),
        )
        self.command(
            "models",
            "inspect",
            "--harness",
            str(self.alias),
            "--allow-authenticated-read",
            "--no-native-cache",
        )
        old = frozen.read_bytes()
        self.command(
            "auth",
            "logout",
            "--profile",
            "journey-local",
            "--executable",
            str(issuer),
            "--yes",
        )
        status = self.command("auth", "status", "--profile", "journey-local")
        require(status["state"] == "logged_out", "logout failed")
        newer = self.command(
            "auth", "login", "--profile", "journey-local", "--executable", str(issuer)
        )
        require(newer["generation"] == 2, "relogin generation did not advance")
        plan = self.command("plan", "example", "--harness", str(self.alias))
        require(
            plan["harness"]["auth"]["reference"]["generation"] == 2,
            "alias stayed stale",
        )
        self.command("doctor", "--harness", str(frozen), expected=2)
        require(frozen.read_bytes() == old, "old snapshot was rewritten")
        self.write("bin/fail-provider", "fail\n")
        failed = self.command(
            "doctor", "--harness", str(self.api), "--check-provider", expected=2
        )
        require(
            failed["authentication"]["provider_checked"]["status"] == "failed",
            "provider failure misreported",
        )
        (self.work / "bin/fail-provider").unlink()

    def remote_journey(self):
        malformed = self.write(
            "config/tetrabench/bad-backend.toml", '[backend]\nkind="s3"\n'
        )
        self.command(
            "auth",
            "login",
            "--profile",
            "bad-backend",
            "--agent",
            "codex",
            "--backend",
            str(malformed),
            "--executable",
            str(self.work / "bin/codex"),
            expected=2,
        )
        backend = self.write(
            "config/tetrabench/backend.toml",
            'kind="s3"\napproved_private_backend=true\ntrust_organization_admins=false\n'
            'access_key={kind="env",name="TETRABENCH_AUTH_ACCESS_KEY_ID"}\nsecret_key={kind="env",name="TETRABENCH_AUTH_SECRET_ACCESS_KEY"}\n'
            '[storage]\nprovider="tigris"\nbucket="journey-auth"\nregion="auto"\nprefix=""\nendpoint_url="https://t3.storage.dev"\n',
        )
        self.command(
            "auth",
            "login",
            "--profile",
            "journey-remote",
            "--agent",
            "codex",
            "--backend",
            str(backend),
            "--executable",
            str(self.work / "bin/codex"),
        )
        self.command("auth", "status", "--profile", "journey-remote", expected=2)
        self.command("auth", "status", "--profile", "journey-remote", "--online")
        remote = self.write(
            "project/remote.toml",
            self.alias.read_text().replace("journey-local", "journey-remote"),
        )
        self.command(
            "plan",
            "example",
            "--profile",
            "cloud",
            "--harness",
            str(remote),
            expected=2,
        )
        self.remote_plan = self.command(
            "plan",
            "example",
            "--profile",
            "cloud",
            "--harness",
            str(remote),
            "--online",
        )
        selected = [
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "TETRABENCH_AUTH_ACCESS_KEY_ID",
            "TETRABENCH_AUTH_SECRET_ACCESS_KEY",
        ]
        args = [
            "controller",
            "configure",
            "--profile",
            "cloud",
            "--harness",
            str(remote),
            "--auth-profile",
            "journey-remote",
            "--create-environment",
        ]
        for name in selected:
            args.extend(["--env", name])
        preview = self.command(*args)
        require(
            not preview["write"] and not self.results[-1]["events"],
            "configure preview performed external work",
        )
        created = self.command(*args, "--write", "--yes")
        require(
            created["ok"] and created["secret_state"] == "created",
            "controller configure create failed",
        )
        values = self.rpc.secrets[created["environment_name"], created["secret_name"]]
        require(
            set(values) == set(selected) | {"TETRABENCH_AUTH_CONFIG_CONTENT"},
            "unselected variable transported",
        )
        transport = json.loads(values["TETRABENCH_AUTH_CONFIG_CONTENT"])
        require(
            set(transport) == {"schema_version", "profiles"}
            and set(transport["profiles"]) == {"journey-remote"},
            "profile/runtime selection wrong",
        )
        updated = self.command(*args, "--update", "--write", "--yes")
        require(
            updated["ok"] and updated["secret_state"] == "updated", "SDK update failed"
        )
        self.command(*args, "--env", "UNSELECTED_VALUE", "--update", "--write", "--yes")
        self.command(*args, "--update", "--write", "--yes")
        require(
            values["UNSELECTED_VALUE"] == os.environ["UNSELECTED_VALUE"],
            "merge update removed existing key",
        )
        self.rpc.fail = "SecretUpdate"
        failed = self.command(*args, "--update", "--write", "--yes", expected=2)
        require(
            not failed["ok"] and failed["secret_state"] == "unknown",
            "ambiguous write reported safe",
        )
        require(
            sum(
                e.get("operation") == "SecretUpdate" for e in self.results[-1]["events"]
            )
            == 1,
            "public Secret mutation retried",
        )
        self.rpc.fail = None
        self.command(
            "doctor",
            "--profile",
            "cloud",
            "--harness",
            str(remote),
            "--online",
            expected=2,
        )
        deployed = self.command("controller", "deploy", "--profile", "cloud", "--yes")
        require(
            deployed["deployed"] and bool(self.graphs),
            "public deploy did not construct graph",
        )
        doctor = self.command(
            "doctor", "--profile", "cloud", "--harness", str(remote), "--online"
        )
        require(
            doctor["controller"]["status"] == "ok", "online controller metadata failed"
        )
        require(
            doctor["authentication"]["remote_runtime_checked"]["status"] == "unproven",
            "doctor invented runtime proof",
        )
        self.controller_runtime(transport, values)
        self.rpc.fail = "FunctionGet"
        self.command(
            "doctor",
            "--profile",
            "cloud",
            "--harness",
            str(remote),
            "--online",
            expected=2,
        )
        self.rpc.fail = None

    def controller_runtime(self, transport, values):
        """Fake provider delivers a generated callback; no onboarding helper call."""
        from tetrabench.controller import ControllerInvocation
        from tetrabench.harness_config import ResolvedHarness
        from tetrabench.local_execution import local_paths
        from tetrabench.plan import canonical_model_bytes
        from tetrabench.storage import request_key

        raw = self.graphs[-1].registered_functions["controller"].get_raw_f()
        controller_home = self.work / "actual-controller-home"
        controller_home.mkdir(mode=0o700)
        observations = []
        harness = ResolvedHarness.model_validate(self.remote_plan["harness"])

        class NoInference(Exception):
            pass

        class ComputeBoundary:
            def __init__(self, store, volume, runner, observer, **kwargs):
                self.runner = runner

            def run(self, invocation, **kwargs):
                try:
                    with self.runner._credential_context(
                        harness, local_paths(controller_home / "outputs")
                    ) as scope:
                        observations.append(
                            scope.directory.is_relative_to(controller_home)
                        )
                        require(
                            scope.claim.snapshot.state.owner
                            == "fc-synthetic-controller",
                            "controller owner not bound",
                        )
                        # No model was executed. Unwind rather than claiming a
                        # completed consumer to the production custody guard.
                        raise NoInference()
                except NoInference:
                    pass
                return SimpleNamespace(
                    attempt_id="synthetic",
                    detail="synthetic runtime context only",
                    run_id=invocation.run_id,
                    state="synthetic",
                    terminal_sha256=None,
                )

        invocation = ControllerInvocation(
            schema_version=1,
            run_id="synthetic-runtime",
            request_sha256="a" * 64,
            plan_sha256="b" * 64,
            request_key=request_key("synthetic-runtime", "a" * 64, prefix="journey"),
            storage=self.remote_plan["storage"],
        )
        body = canonical_model_bytes(invocation)
        with (
            patch.dict(
                os.environ, {"HOME": str(controller_home), **values}, clear=True
            ),
            patch(
                "tetrabench.modal_app.modal.current_function_call_id",
                return_value="fc-synthetic-controller",
            ),
            patch("tetrabench.modal_app.ControllerRuntime", ComputeBoundary),
        ):
            raw(body, hashlib.sha256(body).hexdigest())
        require(observations == [True], "controller used submitter HOME")
        self.events.append(
            {
                "transport": "runtime",
                "operation": "generated-controller-context",
                "controller_home_verified": True,
                "auth_profiles_selected": list(transport["profiles"]),
                "inference": False,
            }
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    work = args.work.absolute()
    require(not work.exists(), "journey work directory must be fresh")
    require(
        not work.is_relative_to(Path(__file__).resolve().parents[1]),
        "journey must run outside its tool snapshot",
    )
    os.umask(0o077)
    work.mkdir(mode=0o700)
    for key, name in {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_DATA_HOME": "data",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "XDG_RUNTIME_DIR": "runtime",
        "TMPDIR": "tmp",
    }.items():
        (work / name).mkdir(mode=0o700)
        os.environ[key] = str(work / name)
    # No inherited operator credentials, native cache selectors, or package paths.
    selected = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("XDG_") or key in {"HOME", "TMPDIR"}
    }
    os.environ.clear()
    os.environ.update(
        selected
        | {
            "PATH": os.defpath,
            "AWS_EC2_METADATA_DISABLED": "true",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    def audit(event, arguments):
        if event == "socket.connect":
            raise JourneyFailure("network access forbidden in synthetic journey")
        if (
            event == "open"
            and isinstance(arguments[0], str)
            and arguments[0].startswith(
                ("/home/tetraslam/.config/", "/home/tetraslam/.modal")
            )
        ):
            raise JourneyFailure("operator credential/config access forbidden")

    sys.addaudithook(audit)
    from importlib.metadata import version

    import tetrabench
    from tetrabench.modal_app import _controller_wheel

    require(
        Path(tetrabench.__file__).is_relative_to(Path(sys.prefix)),
        "not running installed package",
    )
    require(version("modal") == "1.5.4", "Modal SDK pin changed")
    _, wheel = _controller_wheel()
    journey = Journeys(work)
    status = "failed"
    try:
        with ExitStack() as stack:
            journey.install_boundaries(stack)
            journey.command("init", str(journey.project))
            journey.inputs()
            journey.api_journey()
            journey.local_oauth_journey()
            journey.remote_journey()
        status = "passed"
    finally:
        report = {
            "status": status,
            "wheel_sha256": hashlib.sha256(wheel).hexdigest(),
            "installed_package": tetrabench.__file__,
            "commands": journey.results,
            "transport_events": journey.events,
            "live_credentials": False,
            "live_provider_calls": False,
            "inference": False,
            "limitations": [
                "Synthetic issuer is not a newly approved login.",
                "Docker execution stops at a synthetic boundary; "
                "no native model/Docker score claimed.",
                "Configure uses real Modal SDK RPC construction; deploy is graph-only.",
            ],
        }
        journey.scan(json.dumps(report))
        journey.write("REPORT.json", json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": status,
                "commands": len(journey.results),
                "report": str(work / "REPORT.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
