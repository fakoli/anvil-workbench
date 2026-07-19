"""Run-scoped Codex provider credential helper.

The managed Codex supervisor invokes this helper outside its tool sandbox.
The URL is deliberately restricted to a nonce-bearing IPv4 loopback endpoint;
the helper has no access to the bridge environment or credential files.
"""
from __future__ import annotations

import re
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_TOKEN_PATH = re.compile(r"^/token/[A-Za-z0-9_-]{43}$")


def fetch_token(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or parsed.query
        or parsed.fragment
        or _TOKEN_PATH.fullmatch(parsed.path) is None
    ):
        raise RuntimeError("Codex credential helper accepts only a run-scoped loopback endpoint")
    request = Request(endpoint, data=b"", method="POST")
    with urlopen(request, timeout=5) as response:  # nosec B310: strict loopback validation above
        body = response.read(16_385)
    if not body or len(body) > 16_384:
        raise RuntimeError("Codex credential broker returned an invalid token")
    return body.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 1:
        raise SystemExit("usage: python -m workbench.codex_auth LOOPBACK_URL")
    sys.stdout.write(fetch_token(values[0]))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by Codex itself
    raise SystemExit(main())
