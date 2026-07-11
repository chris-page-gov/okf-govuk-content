"""Command-line entry point."""

from __future__ import annotations

import argparse

from .contract import synchronize
from .controller import materialize_contracts


def main() -> int:
    parser = argparse.ArgumentParser(prog="govuk-okf")
    parser.add_argument("command", choices=["sync-contract", "check-contract", "sync-tasks", "check-tasks"])
    args = parser.parse_args()
    check = args.command.startswith("check")
    errors = synchronize(check=check) if args.command.endswith("contract") else materialize_contracts(check=check)
    if errors:
        parser.error("; ".join(errors))
    return 0

