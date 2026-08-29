#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _post(transition: dict[str, object]) -> bytes:
    capability = os.environ.get("FORGE_RUN_CAPABILITY", "")
    if not capability:
        raise RuntimeError("FORGE_RUN_CAPABILITY is unavailable")
    request = urllib.request.Request(
        os.environ.get("FORGE_URL", "http://forge:8080") + "/transitions",
        data=_canonical(transition),
        headers={
            "Content-Type": "application/json",
            "X-Forge-Capability": capability,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(prog="forge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit-pr")
    submit.add_argument("--base", required=True)
    submit.add_argument("--head", required=True)
    submit.add_argument("--head-oid", required=True)
    args = parser.parse_args()
    common = {
        "base": args.base,
        "head": args.head,
        "head_oid": args.head_oid,
        "schema_version": 1,
    }
    try:
        _post(
            {
                **common,
                "request_id": "opened-" + secrets.token_hex(16),
                "type": "pull_request.opened",
            }
        )
        final = _post(
            {
                **common,
                "request_id": "submitted-" + secrets.token_hex(16),
                "type": "pull_request.submitted",
            }
        )
        sys.stdout.buffer.write(final)
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            sys.stderr.buffer.write(exc.read())
        else:
            print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
