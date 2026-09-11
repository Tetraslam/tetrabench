"""Exercise a release wheel outside its checkout, without cloud authentication.

Run with --wheel DIST.whl --work-dir NEW_DIRECTORY [--docker] [--category NAME].
The private directory is retained for inspection and an explicitly authorized
remote smoke using its installed CLI. Its local wheel is resolved through PEP 610.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil

# Trusted release tools are invoked with explicit argv, never through a shell.
import subprocess  # nosec B404
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(argv, cwd=cwd, env=env, check=True)  # nosec B603


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def wheel_contents(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def installed_harness_smoke(work: Path, entries: list[dict]) -> list[dict]:
    """Capture installed adapters only; no native process, account, or model run."""
    from unittest.mock import patch

    from harbor.agents.factory import AgentFactory
    from harbor.models.agent.context import AgentContext

    from tetrabench.auth_config import AuthSpec, EnvAuthReference
    from tetrabench.harness_config import HarnessConfig, ResolvedHarness, ResourceSource
    from tetrabench.harnesses import compile_agent_config, seal_harness
    from tetrabench.plan import canonical_model_bytes, parse_canonical_model

    class Capture:
        default_user = None

        def __init__(self, name: str, version: str):
            self.name = name
            self.version = version
            self.uploads: dict[str, bytes] = {}

        async def exec(self, **kwargs):
            stdout = ""
            if "--version" in kwargs["command"]:
                stdout = {
                    "codex": f"codex-cli {self.version}",
                    "claude-code": f"{self.version} (Claude Code)",
                }.get(self.name, self.version)
            return SimpleNamespace(return_code=0, stdout=stdout, stderr="")

        async def upload_file(self, source, destination):
            self.uploads[str(destination)] = Path(source).read_bytes()

        async def download_dir(self, *args, **kwargs):
            pass

        async def download_file(self, *args, **kwargs):
            pass

    evidence = []
    for entry in entries:
        name = entry["name"]
        root = work / "harnesses" / name
        root.mkdir(parents=True)
        source = root / "instructions.md"
        resource_bytes = b"Distribution smoke instruction resource.\n"
        source.write_bytes(resource_bytes)
        key = "ANTHROPIC_API_KEY" if name == "claude-code" else "OPENAI_API_KEY"
        spec = HarnessConfig(
            name=name,
            version=entry["stable_version"],
            model="anthropic/claude-sonnet-4-6"
            if name == "claude-code"
            else "openai/gpt-5",
            resources=[
                ResourceSource(source="instructions.md", destination="instructions.md")
            ],
            discovery="isolated" if name != "codex" else "native",
            env={key: "${DISTRIBUTION_SMOKE_TOKEN}"},
        )
        sealed = seal_harness(spec, root)
        payload = canonical_model_bytes(sealed)
        require(
            parse_canonical_model(payload, ResolvedHarness) == sealed,
            "harness roundtrip failed",
        )
        if name in {"codex", "claude-code"}:
            explicit = HarnessConfig(
                **(
                    spec.model_dump(exclude={"env", "auth"})
                    | {
                        "auth": AuthSpec(
                            mode="api_key",
                            reference=EnvAuthReference(name="DISTRIBUTION_SMOKE_TOKEN"),
                        ),
                    }
                )
            )
            bound = seal_harness(explicit, root)
            require(
                parse_canonical_model(canonical_model_bytes(bound), ResolvedHarness)
                == bound,
                "explicit auth reference roundtrip failed",
            )
        source.unlink()
        with patch.dict(
            os.environ,
            {"DISTRIBUTION_SMOKE_TOKEN": "synthetic-smoke-token"},  # nosec B105
        ):
            config = compile_agent_config(sealed)
            require(
                "synthetic-smoke-token" not in config.model_dump_json(),
                "credential serialized",
            )
            agent: Any = AgentFactory.create_agent_from_config(
                config, logs_dir=root / "logs"
            )
            capture = Capture(name, entry["stable_version"])
            asyncio.run(agent.setup(capture))
            asyncio.run(
                agent.run("Capture only; no execution.", capture, AgentContext())
            )
        require(
            resource_bytes in capture.uploads.values(),
            "sealed resource was not uploaded",
        )
        provenance = (root / "logs/tetrabench-harness.json").read_text()
        require("synthetic-smoke-token" not in provenance, "credential in provenance")
        evidence.append(
            {
                "name": name,
                "version": entry["stable_version"],
                "resource_snapshot": "passed",
                "adapter_capture": "passed",
                "auth": "reference-only"
                if name in {"codex", "claude-code"}
                else "not-run",
            }
        )
    return evidence


def installed_smoke(
    work: Path, *, docker: bool, category: str | None, expected_version: str
) -> None:
    """Runs only under the wheel-installed interpreter with Python isolation."""
    from importlib.metadata import distribution
    from importlib.resources import files
    from unittest.mock import patch

    import tetrabench
    from tetrabench.canonical_json import dumps_canonical_json, loads_canonical_json
    from tetrabench.diagnostics import PreflightError
    from tetrabench.modal_app import (
        ControllerDeploymentSpec,
        build_modal_controller,
        deploy_controller,
    )
    from tetrabench.preflight import check_runtime

    package = distribution("tetrabench")
    require(package.version == expected_version, "wrong release version")
    require(Path(tetrabench.__file__).is_relative_to(work / "venv"), "checkout import")
    require(package.metadata["License-Expression"] == "MIT", "missing MIT metadata")
    require(package.metadata["Requires-Python"] == "<3.13,>=3.12", "wrong Python range")
    require(check_runtime("doctor")["status"] == "ok", "runtime preflight failed")
    require(
        bool(files("tetrabench").joinpath("native_reasoning.mjs").read_bytes()),
        "missing packaged native metadata helper",
    )
    require(
        "LICENSE" in package.metadata.get_all("License-File", []), "missing license"
    )
    require(
        any(
            item.name == "tetrabench" and item.value == "tetrabench.cli:main"
            for item in package.entry_points
        ),
        "missing console entrypoint",
    )
    project = work / "project"
    cli = work / "venv/bin/tetrabench"

    def command(*arguments: str, cwd: Path = project) -> dict:
        # Only the absolute isolated CLI is executable here.
        completed = subprocess.run(  # nosec B603
            [str(cli), *arguments, "--json"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise RuntimeError(
                f"installed CLI failed ({completed.returncode}): {completed.stderr}"
            )
        return json.loads(completed.stdout)

    require(
        command("init", str(project), cwd=work)["status"] == "created", "init failed"
    )
    config = tomllib.loads((project / "tetrabench.toml").read_text())
    catalog_path = project / config["catalog_path"]
    catalog = tomllib.loads(catalog_path.read_text())
    if category:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", category):
            raise ValueError("smoke category must be a lowercase portable identifier")
        require(category not in catalog["sections"], "smoke category already exists")
        section_root = catalog_path.parent / category
        section_root.mkdir()
        (section_root / "README.md").write_text("# Release smoke\n")
        with catalog_path.open("a") as stream:
            stream.write(
                f'\n[sections."{category}"]\n'
                'description = "Isolated installed-distribution smoke."\n'
                f'readme = "{category}/README.md"\n'
            )
        new_task = command("task", "new", category, "release-smoke")
        command("task", "add", category, "release-smoke", new_task["fixture"])
        catalog = tomllib.loads(catalog_path.read_text())
    else:
        category = next(
            name
            for name, section in catalog["sections"].items()
            if section.get("tasks")
        )
    selected = catalog["sections"][category]
    for task in selected["tasks"]:
        require(
            command("task", "validate", task["harbor_task"])["status"] == "ok",
            "fixture validation failed",
        )
    doctor = command("doctor")
    require(
        all(check["status"] == "ok" for check in doctor["checks"][:2]), "doctor failed"
    )
    plan = command("plan", category)
    require(plan["runnable"] is True, "plan is not runnable")
    require(len(plan["trials"]) == len(selected["tasks"]), "plan lost tasks")
    entries = command("agents")["harnesses"]
    require(
        {entry["name"] for entry in entries}
        == {"opencode", "codex", "claude-code", "pi"},
        "missing controlled harnesses",
    )
    harness_evidence = installed_harness_smoke(work, entries)

    # Native Modal 1.5.4 graph construction only: no hydration, auth, or deployment.
    name = "tetrabench-release-smoke"
    bundle = build_modal_controller(
        ControllerDeploymentSpec(
            profile=None,
            app_name=name,
            function_name="controller",
            environment_name=name,
            volume_name="tetrabench-release-smoke-controller",
            secret_name=name,
        )
    )
    require(
        tuple(bundle.app.registered_functions) == ("controller",), "missing function"
    )
    # An unsupported caller must fail before artifact lookup or provider creation.
    with (
        patch("tetrabench.preflight.sys.version_info", (3, 13, 0)),
        patch(
            "tetrabench.modal_app._controller_wheel",
            side_effect=AssertionError("artifact lookup"),
        ),
        patch(
            "tetrabench.modal_app.modal.Client.from_env",
            side_effect=AssertionError("provider access"),
        ),
    ):
        try:
            deploy_controller(bundle.spec)
        except PreflightError as error:
            require(
                error.code == "unsupported_python", "wrong compatibility diagnostic"
            )
            report = error.as_dict()
            require(
                loads_canonical_json(dumps_canonical_json(report)) == report,
                "noncanonical diagnostic",
            )
            require(
                f"--python 3.12 tetrabench=={expected_version}" in str(error),
                "missing install advice",
            )
        else:
            raise RuntimeError("unsupported Python reached deployment")
    if docker:
        report = command("run", category, "--output", str(work / "docker-run"))
        require(report["outcome"] == "succeeded", "Docker run did not succeed")
        require(report["summary"]["aggregate"] == "1", "Docker reward was not one")
        require(
            report["summary"]["pass_count"] == report["summary"]["sample_count"],
            "Docker samples failed",
        )
    result = {
        "version": package.version,
        "wheel_sha256": bundle.wheel_sha256,
        "category": category,
        "docker": "passed" if docker else "not-run",
        "modal_graph": "passed",
        "modal_deployment": "not-run",
        "runtime_preflight": "passed",
        "harnesses": harness_evidence,
        "project": str(project),
        "cli": str(cli),
    }
    (work / "smoke.json").write_text(json.dumps(result, indent=2) + "\n")
    bundle.artifacts.cleanup()
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path, help="also rebuild and compare the sdist")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--category", help="also prove arbitrary-category authoring")
    parser.add_argument("--installed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expected-version", help=argparse.SUPPRESS)
    parser.add_argument("--tag", help="require release tag v<distribution version>")
    args = parser.parse_args()
    work = args.work_dir.expanduser().resolve()
    if args.installed:
        if not args.expected_version:
            parser.error("--installed requires --expected-version")
        installed_smoke(
            work,
            docker=args.docker,
            category=args.category,
            expected_version=args.expected_version,
        )
        return
    if args.wheel is None:
        parser.error("--wheel is required")
    root = Path(__file__).resolve().parents[1]
    if work.is_relative_to(root):
        parser.error("--work-dir must be outside the source checkout")
    wheel = args.wheel.expanduser().resolve()
    wheel_bytes = wheel.read_bytes()
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        metadata = BytesParser().parsebytes(
            archive.read(
                next(name for name in archive.namelist() if name.endswith("/METADATA"))
            )
        )
        if args.tag and args.tag != f"v{metadata['Version']}":
            parser.error("release tag does not match wheel version")
        work.mkdir(mode=0o700)
        lock = work / "lock"
        lock.mkdir(mode=0o700)
        for name in ("pyproject.toml", "uv.lock"):
            (lock / name).write_bytes(archive.read(f"tetrabench/_distribution/{name}"))
        project = tomllib.loads((lock / "pyproject.toml").read_text())
        require(
            project["project"]["version"] == metadata["Version"],
            "embedded version mismatch",
        )
    retained_wheel = work / wheel.name
    retained_wheel.write_bytes(wheel_bytes)
    # Native clients and provider SDKs must never inherit the operator's logins.
    environment = {"PATH": os.environ["PATH"]}
    environment.update(
        HOME=str(work / "home"),
        XDG_CONFIG_HOME=str(work / "config"),
        XDG_STATE_HOME=str(work / "state"),
        XDG_DATA_HOME=str(work / "data"),
        XDG_CACHE_HOME=str(work / "cache"),
        AWS_EC2_METADATA_DISABLED="true",
        UV_NO_CONFIG="true",
    )
    (work / "home").mkdir(mode=0o700)
    if args.sdist:
        rebuilt = work / "rebuilt"
        run(
            [
                "uv",
                "build",
                "--python",
                "3.12",
                "--wheel",
                str(args.sdist.resolve()),
                "--out-dir",
                str(rebuilt),
            ],
            cwd=work,
            env=environment,
        )
        rebuilt_wheel = next(rebuilt.glob("*.whl"))
        if wheel_contents(rebuilt_wheel) != wheel_contents(retained_wheel):
            raise ValueError("source distribution does not reproduce the release wheel")
    python = work / "venv/bin/python"
    # Exercise the same native dependency operation used by Modal Image.uv_sync.
    environment["UV_PROJECT_ENVIRONMENT"] = str(work / "venv")
    run(
        [
            "uv",
            "sync",
            "--python",
            "3.12",
            "--frozen",
            "--no-install-workspace",
            "--no-default-groups",
        ],
        cwd=lock,
        env=environment,
    )
    wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
    wheel_requirement = work / "controller-wheel.txt"
    wheel_requirement.write_text(
        f"tetrabench @ {retained_wheel.as_uri()}#sha256={wheel_digest} "
        f"--hash=sha256:{wheel_digest}\n"
    )
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--require-hashes",
            "--requirements",
            str(wheel_requirement),
        ],
        cwd=work,
        env=environment,
    )
    run(["uv", "pip", "check", "--python", str(python)], cwd=work, env=environment)
    worker = work / "distribution_smoke.py"
    shutil.copyfile(__file__, worker)
    command = [
        str(python),
        "-I",
        str(worker),
        "--installed",
        "--work-dir",
        str(work),
        "--expected-version",
        metadata["Version"],
    ]
    if args.docker:
        command.append("--docker")
    if args.category:
        command.extend(["--category", args.category])
    run(command, cwd=work, env=environment)
    require(
        hashlib.sha256(wheel.read_bytes()).digest()
        == hashlib.sha256(retained_wheel.read_bytes()).digest(),
        "release artifact changed during smoke",
    )


if __name__ == "__main__":
    main()
