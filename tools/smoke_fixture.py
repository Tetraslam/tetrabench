"""Submit the integration fixture to an already deployed controller.

This source-checkout helper is intentionally absent from the installed CLI and
benchmark catalog. It performs cloud mutations only with --yes.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tetrabench.config import load_project_config
from tetrabench.controller import ModalControllerClient
from tetrabench.integration import prepare_fixture_submission
from tetrabench.receipts import ReceiptStore
from tetrabench.s3 import create_s3_store
from tetrabench.submission import SubmissionService

MAX_HOLD_SECONDS = 300


def _hold_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("hold seconds must be an integer") from error
    if not 0 <= seconds <= MAX_HOLD_SECONDS:
        raise argparse.ArgumentTypeError(
            f"hold seconds must be between 0 and {MAX_HOLD_SECONDS}"
        )
    return seconds


@contextmanager
def fixture_with_hold(fixture: Path, hold_seconds: int) -> Iterator[Path]:
    """Copy the source-only fixture and delay its real oracle process."""
    if not 0 <= hold_seconds <= MAX_HOLD_SECONDS:
        raise ValueError(f"hold seconds must be between 0 and {MAX_HOLD_SECONDS}")
    if hold_seconds == 0:
        yield fixture
        return
    with tempfile.TemporaryDirectory(prefix="tetrabench-cancel-fixture-") as temporary:
        copied = Path(temporary) / fixture.name
        shutil.copytree(fixture, copied)
        solution = copied / "solution/solve.sh"
        body = solution.read_text()
        marker = "set -eu\n"
        if body.count(marker) != 1:
            raise ValueError("fixture solution has no unique shell setup marker")
        solution.write_text(body.replace(marker, f"{marker}sleep {hold_seconds}\n"))
        yield copied


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--hold-seconds",
        type=_hold_seconds,
        default=0,
        help=f"delay the oracle child for cancellation (0-{MAX_HOLD_SECONDS})",
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if not args.yes:
        parser.error("fixture submission mutates S3 and Modal; pass --yes")

    root = Path(__file__).parents[1]
    fixture = root / "tests/fixtures/harbor_task"
    config = load_project_config(root, profile=args.profile)
    if config.controller.kind != "modal" or config.execution.kind != "modal":
        parser.error("fixture smoke requires a Modal controller and execution profile")
    if config.storage is None:
        parser.error("fixture smoke requires storage")
    with fixture_with_hold(fixture, args.hold_seconds) as selected_fixture:
        prepared = prepare_fixture_submission(
            selected_fixture, config, run_id=args.run_id, profile=args.profile
        )
        launch = prepared.controller_launch
        if launch is None:
            parser.error("fixture smoke requires a prepared Modal endpoint")
        storage = prepared.plan.storage
        if storage is None:
            parser.error("fixture smoke requires prepared storage")
        receipt = SubmissionService(
            create_s3_store(storage),
            ModalControllerClient(
                launch.app_name,
                launch.function_name,
                environment_name=launch.environment_name,
            ),
            ReceiptStore(),
        ).submit(prepared)
    call_id = receipt.attempts[-1].controller_calls[-1].call_id
    print(f"submitted {receipt.run_id} as {call_id}")


if __name__ == "__main__":
    main()
