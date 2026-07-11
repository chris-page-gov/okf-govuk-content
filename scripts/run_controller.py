#!/usr/bin/env python3
"""Inspect or operate the durable programme controller."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from govuk_okf.controller import Controller, ControllerError, materialize_contracts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["materialize", "check", "init", "status", "ready", "transition"])
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--task")
    parser.add_argument("--state")
    args = parser.parse_args()
    if args.command in {"materialize", "check"}:
        errors = materialize_contracts(check=args.command == "check")
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("task contracts are synchronized" if args.command == "check" else "wrote task contracts")
        return 0

    run_root = ROOT / "runs" / args.run_id
    controller = Controller(run_root / "state.sqlite", run_root / "events.jsonl")
    try:
        controller.bootstrap()
        if args.command == "init":
            print(json.dumps(controller.summary(), sort_keys=True))
        elif args.command == "status":
            print(json.dumps(controller.summary(), sort_keys=True))
        elif args.command == "ready":
            print("\n".join(controller.ready()))
        elif args.command == "transition":
            if not args.task or not args.state:
                parser.error("transition requires --task and --state")
            print(controller.transition(args.task, args.state))
    except ControllerError as exc:
        print(f"controller error: {exc}", file=sys.stderr)
        return 1
    finally:
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

