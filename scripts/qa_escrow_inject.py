#!/usr/bin/env python3
"""QA fixture harness — legacy entrypoint.

All the behaviour now lives in :mod:`app.qa_fixtures` so it can ship
inside the PyInstaller binary via ``spacerouter-node --qa-inject
<scenario>``. This script is kept for source-install / systemd users
who invoke the harness by path.

Usage (same as before):
    SR_RECEIPT_STORE_PATH=~/.spacerouter/receipts.db \\
    SR_ALLOW_TEST_FIXTURES=1 \\
    python scripts/qa_escrow_inject.py --scenario <name> [--uuid <uuid>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the provider's ``app`` package importable when this script is run
# directly (systemd / manual invocation from /root/space-router-node).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.qa_fixtures import all_scenarios, run_cli  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", required=True, choices=all_scenarios(),
        help="Failure scenario to inject.",
    )
    parser.add_argument(
        "--uuid",
        help=(
            "Target receipt UUID. Defaults to the oldest row matching "
            "the scenario's signature requirement."
        ),
    )
    args = parser.parse_args()
    sys.exit(run_cli(args.scenario, args.uuid))


if __name__ == "__main__":
    main()
