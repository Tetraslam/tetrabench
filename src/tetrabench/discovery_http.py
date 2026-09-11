"""Explicit, unauthenticated public metadata reads, with no ambient auth/proxy."""

from __future__ import annotations

import http.client
import re
from urllib.parse import urlsplit

from tetrabench.capabilities import (
    MAX_METADATA_BYTES,
    MetadataError,
    parse_metadata,
    safe_url,
)


def refresh_public_metadata(url: str, *, refresh: bool = False) -> str:
    return read_public_metadata(url, refresh=refresh)[0]


def read_public_metadata(
    url: str, *, refresh: bool = False, full_catalog: bool = False
) -> tuple[str, dict[str, str]]:
    """GET only known catalog paths. No redirects, environment, netrc, or retries."""
    if not refresh:
        raise MetadataError("public metadata HTTP requires explicit refresh")
    safe_url(url)
    parts = urlsplit(url)
    allowed = (
        (
            parts.hostname in {"models.dev", "models.opencode.ai"}
            and parts.path == "/api.json"
        )
        or (
            parts.hostname == "openrouter.ai"
            and (
                parts.path == "/api/v1/models"
                or re.fullmatch(r"/api/v1/model/[\w-]+/[\w.:-]+", parts.path)
                is not None
            )
        )
        or (
            parts.hostname == "pi.dev"
            and re.fullmatch(r"/api/models/providers/[\w-]+", parts.path) is not None
        )
    )
    if parts.scheme != "https" or parts.port not in {None, 443} or not allowed:
        raise MetadataError("URL is not an allowed public model metadata endpoint")
    connection = http.client.HTTPSConnection(parts.hostname, timeout=15)
    try:
        connection.request("GET", parts.path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise MetadataError(
                "public metadata read failed; no redirect or auth attempted"
            )
        limit = 16 * 1024 * 1024 if full_catalog else MAX_METADATA_BYTES
        body = response.read(limit + 1)
        if len(body) > limit:
            raise MetadataError("public metadata exceeds byte limit")
        if not full_catalog:
            parse_metadata(body)
        headers = {
            name: value
            for name in ("last-modified", "etag")
            if (value := response.getheader(name)) is not None
        }
        return body.decode("utf-8"), headers
    except (OSError, http.client.HTTPException, UnicodeError):
        raise MetadataError("public metadata read failed") from None
    finally:
        connection.close()
