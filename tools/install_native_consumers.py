"""Install locked official test consumers outside the checkout (Linux x86-64).

Use Node 24.21.0 LTS, then run:
    uv run python tools/install_native_consumers.py --prefix /tmp/native-consumers

Export TETRABENCH_NATIVE_NODE_MODULES=<prefix>/node_modules for pytest. Downloads
occur only here, never in the native-consumer tests. Lifecycle scripts are disabled;
tests use the packaged platform binaries directly when an npm shim needs postinstall.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess  # nosec B404
from pathlib import Path


def install(prefix: Path) -> None:
    source = Path(__file__).resolve().parent / "native_consumers"
    root = source.parent.parent
    prefix = prefix.expanduser().resolve()
    if prefix.is_relative_to(root):
        raise ValueError("native packages must be installed outside the checkout")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("native consumer CI currently targets Linux x86-64")
    # Never pass registry tokens, provider keys, or an operator's npmrc to npm.
    prefix.mkdir(mode=0o700)
    home = prefix / "home"
    home.mkdir(mode=0o700)
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "NPM_CONFIG_USERCONFIG": str(home / ".npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(home / "global.npmrc"),
        "NPM_CONFIG_CACHE": str(prefix / "cache"),
    }
    for name in ("package.json", "package-lock.json", ".npmrc"):
        shutil.copyfile(source / name, prefix / name)
    subprocess.run(  # nosec B603 B607
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=prefix,
        env=environment,
        check=True,
        timeout=300,
    )
    print(json.dumps({"TETRABENCH_NATIVE_NODE_MODULES": str(prefix / "node_modules")}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    install(parser.parse_args().prefix)


if __name__ == "__main__":
    main()
