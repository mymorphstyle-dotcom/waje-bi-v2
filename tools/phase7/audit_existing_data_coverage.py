from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bi_agent.conversation.postgres_store import PostgresConversationStore
from bi_agent.runtime.coverage_audit import audit_existing_data_coverage
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit current runtime data coverage")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--permission-scope", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        as_of = datetime.fromisoformat(args.as_of)
        store = PostgresConversationStore.from_env()
        store.runtime_evidence_resolver()
        audit = audit_existing_data_coverage(
            RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH),
            snapshot_records=store.list_dataset_snapshots(),
            release_resolver=store,
            as_of=as_of,
            permission_scope=args.permission_scope,
        )
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"artifact": str(output), "summary": audit["summary"]}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"coverage audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        store = locals().get("store")
        if store is not None:
            store.connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
