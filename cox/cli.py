"""cox — control-plane CLI (T-01 completes this).

Subcommands per DESIGN.md §2: status, dispatch, gate, fix, ship, merge,
teardown, watch, await-wake, peek, pause, resume-ops, cost.
Guardrails (DESIGN §4) are enforced HERE, not in the orchestrator's prose.
"""

import argparse
import sys

SUBCOMMANDS: dict[str, str] = {
    "status": "T-08",
    "dispatch": "T-06/T-07",
    "gate": "T-09",
    "fix": "T-10",
    "ship": "T-11",
    "merge": "T-11",
    "teardown": "T-12",
    "watch": "T-08",
    "await-wake": "T-08",
    "peek": "T-08",
    "pause": "T-01",
    "resume-ops": "T-01",
    "cost": "T-07",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cox", description=__doc__)
    parser.add_argument("command", choices=sorted(SUBCOMMANDS))
    args, _rest = parser.parse_known_args(argv)
    print(
        f"cox {args.command}: not implemented yet (see TASKS.md {SUBCOMMANDS[args.command]})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
