from __future__ import annotations

import argparse
import json

from bi_agent.conversation.agent_core import ConversationAgentCore
from tools.phase7.run_live_conversation_system_test import load_env_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--clarification-answer")
    args = parser.parse_args()

    load_env_file("/Users/luka/work/waje-bi-v2/.env")
    core = ConversationAgentCore.from_environment(
        real_llm=True,
        real_clickhouse=True,
    )
    answer = str(args.clarification_answer or "").strip()
    result = core.run_message(
        thread_id=args.thread,
        run_id=args.run,
        user_message=answer or args.question,
        role="analyst",
        runtime_permission_scope="analyst",
        artifact_root=args.artifact_root,
        clarification={"answer_text": answer} if answer else None,
    )
    print(
        json.dumps(
            {
                "status": result.get("status"),
                "run_id": result.get("run_id"),
                "thread_id": args.thread,
                "topic_id": result.get("topic_id"),
                "failure_reason": result.get("failure_reason"),
                "artifact_path": result.get("artifact_path"),
                "clarification": result.get("clarification"),
                "accepted_graph": result.get("accepted_graph"),
                "answer_package": result.get("answer_package"),
                "llm_calls": result.get("llm_calls", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
