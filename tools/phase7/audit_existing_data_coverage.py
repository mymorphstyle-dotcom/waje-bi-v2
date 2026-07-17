from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.coverage_audit import audit_existing_data_coverage
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


class CoverageCLIError(Exception):
    def __init__(self, error_code: str, owner: str, impact: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.owner = owner
        self.impact = impact


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CoverageCLIError(
            "coverage_cli_arguments_invalid",
            "audit_operator",
            "the coverage audit command arguments are invalid",
        )


def run_audit(args: argparse.Namespace, *, store: Any) -> dict[str, Any]:
    try:
        as_of = datetime.fromisoformat(args.as_of)
    except (TypeError, ValueError) as exc:
        raise CoverageCLIError(
            "coverage_request_invalid", "audit_operator", "the coverage audit request is invalid"
        ) from exc
    try:
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    except Exception as exc:
        raise CoverageCLIError(
            "coverage_runtime_contract_invalid",
            "analysis_contract_owner",
            "the reviewed runtime contract could not be loaded or validated",
        ) from exc
    try:
        store.runtime_evidence_resolver()
        snapshots = store.list_dataset_snapshots()
    except Exception as exc:
        raise CoverageCLIError(
            "coverage_database_unavailable",
            "runtime_operations_owner",
            "current coverage authority could not be read",
        ) from exc
    try:
        audit = audit_existing_data_coverage(
            registry,
            snapshot_records=snapshots,
            release_resolver=store,
            as_of=as_of,
        )
    except Exception as exc:
        raise CoverageCLIError(
            "coverage_release_authority_invalid",
            "data_operations_owner",
            "snapshot or release authority failed integrity validation",
        ) from exc
    output = Path(args.out)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise CoverageCLIError(
            "coverage_artifact_write_failed",
            "runtime_operations_owner",
            "the local coverage artifact could not be written",
        ) from exc
    return {"ok": True, "artifact": str(output), "summary": audit["summary"]}


def main(
    argv: list[str] | None = None,
    *,
    store_factory: Callable[[], Any] = PostgresConversationStore.from_env,
) -> int:
    parser = SafeArgumentParser(description="Audit current runtime data coverage")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--out", required=True)
    store = None
    result = None
    failure = None
    try:
        args = parser.parse_args(argv)
        try:
            store = store_factory()
        except Exception as exc:
            raise CoverageCLIError(
                "coverage_database_unavailable",
                "runtime_operations_owner",
                "current coverage authority could not be read",
            ) from exc
        result = run_audit(args, store=store)
    except CoverageCLIError as exc:
        failure = exc
    finally:
        connection = getattr(store, "connection", None)
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                if failure is None:
                    failure = CoverageCLIError(
                        "coverage_database_close_failed",
                        "runtime_operations_owner",
                        "the coverage database connection could not be closed cleanly",
                    )
    if failure is not None:
        print(json.dumps({
            "ok": False,
            "error_code": failure.error_code,
            "owner": failure.owner,
            "impact": failure.impact,
        }, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
