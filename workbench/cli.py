"""Small local utility entrypoint for Workbench operators."""
from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anvil-workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Show the installed Workbench version.")
    args = parser.parse_args(argv)
    if args.command == "version":
        print("anvil-workbench 0.1.0")
    return 0
