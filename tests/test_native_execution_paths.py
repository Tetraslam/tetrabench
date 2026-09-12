"""Installation and later native phases share HOME-owned executable locations."""

import asyncio
import os
import subprocess
from typing import Any

import pytest
from harbor.agents.factory import AgentFactory
from test_controlled_harnesses import CaptureEnvironment

from tetrabench.harness_agents import native_credential_hooks
from tetrabench.harness_config import HarnessConfig
from tetrabench.harnesses import STABLE_VERSIONS, compile_agent_config, seal_harness
from tetrabench.native_execution import (
    NATIVE_SHELL_PREFIX,
    native_process_environment,
    native_shell_command,
)
from tetrabench.nativeauth import isolated_auth_environment


def test_native_path_setup_is_home_owned_not_xdg_owned(tmp_path):
    home = tmp_path / "owned home"
    (home / ".nvm").mkdir(parents=True)
    (home / ".local/bin").mkdir(parents=True)
    (home / ".nvm/nvm.sh").write_text('test "$NVM_DIR" = "$HOME/.nvm" || exit 42\n')
    binary = home / ".local/bin/claude"
    binary.write_text("#!/bin/sh\nprintf 'native-fixture\\n'\n")
    binary.chmod(0o700)
    env = {
        "HOME": str(home),
        "PATH": os.defpath,
        "XDG_CONFIG_HOME": str(tmp_path / "auth/config"),
        "NVM_DIR": "/unrelated/nvm",
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            native_shell_command('claude; test "$XDG_CONFIG_HOME" != "$HOME/.config"'),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "native-fixture\n"
    assert native_shell_command(native_shell_command("true")) == native_shell_command(
        "true"
    )


def test_argv_auth_environment_preserves_xdg_isolation(tmp_path):
    tmp_path.chmod(0o700)
    env = isolated_auth_environment("pi", tmp_path, base={"PATH": os.defpath})
    assert env["NVM_DIR"] == env["HOME"] + "/.nvm"
    assert env["PATH"].split(":")[0] == env["HOME"] + "/.local/bin"
    assert env["XDG_CONFIG_HOME"] != env["HOME"] + "/.config"
    assert native_process_environment(env) == env


@pytest.mark.parametrize("name", ["claude-code", "pi", "opencode"])
@pytest.mark.parametrize("mode", ["api_key", "oauth"])
def test_native_installer_and_version_use_shared_owned_prefix(tmp_path, name, mode):
    from tetrabench.auth_config import AuthSpec, EnvAuthReference, NativeAuthReference

    auth = (
        AuthSpec(mode="api_key", reference=EnvAuthReference(name="SYNTHETIC_KEY"))
        if mode == "api_key"
        else AuthSpec(
            mode="claude_setup_token",
            reference=EnvAuthReference(name="SYNTHETIC_TOKEN"),
        )
        if name == "claude-code"
        else AuthSpec(
            mode="chatgpt_oauth",
            reference=NativeAuthReference(
                profile="fixture", binding="local", generation=1
            ),
        )
    )
    model = (
        "anthropic/model"
        if name == "claude-code"
        else "openai-codex/model"
        if name == "pi" and mode == "oauth"
        else "openai/model"
    )
    spec = HarnessConfig(
        name=name, version=STABLE_VERSIONS[name], model=model, auth=auth
    )
    instance: Any = AgentFactory.create_agent_from_config(
        compile_agent_config(seal_harness(spec, tmp_path)), logs_dir=tmp_path / "logs"
    )
    environment = CaptureEnvironment(name, STABLE_VERSIONS[name])

    class Capsule:
        async def prepare(self, agent, environment):
            pass

        def exec_environment(self, agent, env):
            return {
                **env,
                "HOME": "/home/native-owner",
                "XDG_CONFIG_HOME": "/private/auth/config",
            }

        async def capture(self, agent, environment):
            pass

    async def dependencies(*_args):
        pass

    instance.ensure_system_dependencies = dependencies
    if name == "claude-code":

        async def absent(_environment):
            return False

        instance._installed_claude_satisfies_version = absent
    with native_credential_hooks(lambda _: Capsule()):
        asyncio.run(instance.setup(environment))
    install = next(
        row
        for row in environment.commands
        if "npm install -g" in row["command"]
        or "npm i -g" in row["command"]
        or "bootstrap.sh" in row["command"]
    )
    assert NATIVE_SHELL_PREFIX in install["command"]
    assert install["env"]["HOME"] == "/home/native-owner"
    assert install["env"]["XDG_CONFIG_HOME"] == "/private/auth/config"
    assert instance.get_version_command().startswith(NATIVE_SHELL_PREFIX)
    if name == "pi":
        assert "nvm install 24 && nvm alias default 24" in install["command"]
        assert "nvm install 22" not in install["command"]
        assert "@earendil-works/pi-coding-agent@0.85.1" in install["command"]
    if name in {"pi", "opencode"}:
        assert install["command"].index('export NVM_DIR="$HOME/.nvm"') < install[
            "command"
        ].index("raw.githubusercontent.com/nvm-sh")


def test_runtime_auth_status_uses_the_same_native_prefix():
    from tetrabench.runtime_auth import _NATIVE_PREFIX

    assert _NATIVE_PREFIX == NATIVE_SHELL_PREFIX
