"""Small local utility entrypoint for Workbench operators."""
from __future__ import annotations

import argparse
import json

from .config import Settings
from .system_health import render_posture_rows, run_posture_audit


def _run_posture(as_json: bool) -> int:
    """Render the observational posture audit for the ambient configuration.

    Builds the report from the *same* :func:`run_posture_audit` runner the
    System Health API calls, so the CLI and the browser can never drift: for a
    given configuration they render identical findings.  ``checked_at`` is left
    unset so the rendered findings are a pure, timestamp-free function of the
    configuration.
    """
    report = run_posture_audit(Settings.from_env())
    if as_json:
        print(json.dumps(report.findings(), indent=2, sort_keys=True))
    else:
        for row in render_posture_rows(report):
            print(row)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anvil-workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Show the installed Workbench version.")
    posture = subparsers.add_parser(
        "posture", help="Run the read-only observational posture audit (no mutation)."
    )
    posture.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    args = parser.parse_args(argv)
    if args.command == "version":
        print("anvil-workbench 0.1.0")
        return 0
    if args.command == "posture":
        return _run_posture(args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI shim
    raise SystemExit(main())
