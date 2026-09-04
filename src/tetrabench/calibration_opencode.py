"""OpenCode adapter used by the source-only calibration runner."""

from typing import ClassVar

from harbor.agents.installed.base import CliFlag
from harbor.agents.installed.opencode import OpenCode


class CalibrationOpenCode(OpenCode):
    """Disable OpenCode's background title-generation model request."""

    CLI_FLAGS: ClassVar[list[CliFlag]] = [
        *OpenCode.CLI_FLAGS,
        CliFlag(
            "title",
            cli="--title",
            type="str",
            default="tetrabench-calibration",
        ),
    ]
