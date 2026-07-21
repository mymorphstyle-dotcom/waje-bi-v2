from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from bi_agent.runtime.general_agent_deployment import (
    validate_general_agent_deployment,
    write_deployment_report,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the WAJE General Agent repository contract, optional "
            "PostgreSQL v12 authority, and optional live mainland-model path."
        )
    )
    parser.add_argument(
        "--database",
        action="store_true",
        help="Run the PostgreSQL audit in a repeatable-read, read-only transaction.",
    )
    parser.add_argument(
        "--live-provider",
        action="store_true",
        help=(
            "Run the real DeepSeek capability probe plus P2 summary, tool discovery, "
            "controlled delegation, outbound, and WAJE trace checks."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run repository, database, and live-provider checks.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optionally persist the customer-independent deployment report.",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(
        validate_general_agent_deployment(
            include_database=args.database or args.all,
            include_live_provider=args.live_provider or args.all,
        )
    )
    if args.json_output is not None:
        write_deployment_report(report, args.json_output)
    print(
        json.dumps(
            report.to_contract(),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
