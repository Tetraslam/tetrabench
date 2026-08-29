"""Submit the integration fixture to an already deployed controller.

This source-checkout helper is intentionally absent from the installed CLI and
benchmark catalog. It performs cloud mutations only with --yes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tetrabench.config import load_project_config
from tetrabench.controller import ModalControllerClient
from tetrabench.integration import prepare_fixture_submission
from tetrabench.modal_app import controller_deployment_spec
from tetrabench.receipts import ReceiptStore
from tetrabench.s3 import create_s3_store
from tetrabench.submission import SubmissionService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--run-id", required=True)
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
    spec = controller_deployment_spec(config, args.profile)
    prepared = prepare_fixture_submission(fixture, config, run_id=args.run_id)
    receipt = SubmissionService(
        create_s3_store(config.storage),
        ModalControllerClient(
            spec.app_name,
            spec.function_name,
            environment_name=spec.environment_name,
        ),
        ReceiptStore(),
    ).submit(prepared)
    call_id = receipt.attempts[-1].controller_calls[-1].call_id
    print(f"submitted {receipt.run_id} as {call_id}")


if __name__ == "__main__":
    main()
