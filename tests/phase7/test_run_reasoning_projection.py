from __future__ import annotations

import textwrap

from tests.phase7.test_gateway_typescript_contract import _run_typescript


def test_reasoning_projection_keeps_question_answers_and_claim_summaries_distinct() -> None:
    result = _run_typescript(
        textwrap.dedent(
            """
            const { traceReasoningFromPersistedState } = await import(
              "./app/api/_runReasoningProjection.ts"
            );

            const fixture = (narrativeDocument) => ({
              runId: "run:test",
              request: { message: "分析月初和月末差异" },
              planRevisionId: "plan:test",
              plan: {
                capability_tasks: [],
                claim_obligations: [
                  {
                    obligation_id: "obligation:primary",
                    claim_kind: "comparative_change",
                    success_policy: { issue_ref: "issue:primary" },
                  },
                  {
                    obligation_id: "obligation:driver",
                    claim_kind: "driver",
                    success_policy: { issue_ref: "issue:driver" },
                  },
                ],
              },
              plannerProposal: {
                issue_tree: [
                  {
                    issue_id: "issue:primary",
                    parent_issue_id: null,
                    question: "月初是否普遍高于月末？",
                    target_claim_kind: "comparative_change",
                  },
                  {
                    issue_id: "issue:driver",
                    parent_issue_id: "issue:primary",
                    question: "主要驱动因素是什么？",
                    target_claim_kind: "driver",
                  },
                ],
              },
              claimSettlement: {
                checkpoint: {
                  proposed_claims: [
                    {
                      claim_ref: "proposed:primary",
                      claim_key: "key:primary",
                      claim_class: "observed_fact",
                      factual_payload: {
                        obligation_id: "obligation:primary",
                        business_readout: "第一项事实",
                      },
                      limitation_refs: [],
                    },
                    {
                      claim_ref: "proposed:driver",
                      claim_key: "key:driver",
                      claim_class: "observed_fact",
                      factual_payload: {
                        obligation_id: "obligation:driver",
                        business_readout: "第二项事实",
                      },
                      limitation_refs: [],
                    },
                  ],
                },
                accepted_claims: [
                  {
                    claim_ref: "claim:primary",
                    claim_key: "key:primary",
                    claim_class: "observed_fact",
                    factual_payload: {
                      obligation_id: "obligation:primary",
                      business_readout: "第一项事实",
                    },
                    limitation_refs: [],
                  },
                  {
                    claim_ref: "claim:driver",
                    claim_key: "key:driver",
                    claim_class: "observed_fact",
                    factual_payload: {
                      obligation_id: "obligation:driver",
                      business_readout: "第二项事实",
                    },
                    limitation_refs: [],
                  },
                ],
                accepted_claim_keys: [
                  {
                    claim_key: "key:primary",
                    claim_kind: "comparative_change",
                  },
                  { claim_key: "key:driver", claim_kind: "driver" },
                ],
                obligation_coverage: [
                  {
                    obligation_id: "obligation:primary",
                    status: "satisfied",
                    claim_refs: ["claim:primary"],
                    limitation_refs: [],
                  },
                  {
                    obligation_id: "obligation:driver",
                    status: "satisfied",
                    claim_refs: ["claim:driver"],
                    limitation_refs: [],
                  },
                ],
                verifier_report: {
                  proposed_to_verified: {
                    "proposed:primary": "claim:primary",
                    "proposed:driver": "claim:driver",
                  },
                  verification_decisions: [
                    {
                      subject_ref: "proposed:primary",
                      disposition: "accepted",
                      limitation_refs: [],
                    },
                    {
                      subject_ref: "proposed:driver",
                      disposition: "accepted",
                      limitation_refs: [],
                    },
                  ],
                },
              },
              materialProjection: {
                claims: [
                  {
                    claim_handle: "c_primary",
                    claim_ref: "claim:primary",
                    material_handles: [],
                    verified_claim_payload: {
                      business_readout: "第一项事实",
                    },
                  },
                  {
                    claim_handle: "c_driver",
                    claim_ref: "claim:driver",
                    material_handles: [],
                    verified_claim_payload: {
                      business_readout: "第二项事实",
                    },
                  },
                ],
                evidence_materials: [],
                limitations: [],
                publication_requirements: [
                  {
                    requirement_handle: "pr_primary",
                    issue_ref: "issue:primary",
                    status: "satisfied",
                  },
                  {
                    requirement_handle: "pr_driver",
                    issue_ref: "issue:driver",
                    status: "satisfied",
                  },
                ],
              },
              narrativeDocument,
              customerPublication: {
                blocks: [
                  {
                    block_id: "answer:final",
                    role: "executive_answer",
                    text: "最终综合答案",
                    claim_refs: ["claim:primary", "claim:driver"],
                    limitation_refs: [],
                  },
                ],
              },
              taskOutcomes: [],
              evidenceEntries: [],
              supportEdges: [],
              queryRuns: [],
              runNodes: [
                {
                  node_name: "understand_business_intent",
                  status: "completed",
                  payload: {
                    repair_notices: [
                      "生成的日期比较口径没有完整覆盖业务问题，已重新分析。"
                    ],
                  },
                },
              ],
            });

            const explicit = traceReasoningFromPersistedState(fixture({
              blocks: [
                {
                  block_id: "question:primary",
                  text: "逐题回答一",
                  requirement_handles: ["pr_primary"],
                  claim_handles: ["c_primary"],
                  limitation_handles: [],
                },
                {
                  block_id: "question:driver",
                  text: "逐题回答二",
                  requirement_handles: ["pr_driver"],
                  claim_handles: ["c_driver"],
                  limitation_handles: [],
                },
                {
                  block_id: "answer:final",
                  text: "最终综合答案",
                  requirement_handles: [],
                  claim_handles: ["c_primary", "c_driver"],
                  limitation_handles: [],
                },
              ],
            }));
            const historical = traceReasoningFromPersistedState(fixture({
              blocks: [
                {
                  block_id: "answer:final",
                  text: "最终综合答案",
                  requirement_handles: [],
                  claim_handles: ["c_primary", "c_driver"],
                  limitation_handles: [],
                },
              ],
            }));
            console.log(JSON.stringify({
              explicit: {
                answers: explicit.issues.map((issue) => issue.answerText),
                statuses: explicit.issues.map((issue) => issue.status),
                summaries: explicit.claims.map((claim) => claim.summary),
                repairNotices: explicit.repairNotices,
              },
              historical: {
                answers: historical.issues.map((issue) => issue.answerText ?? null),
                statuses: historical.issues.map((issue) => issue.status),
              },
            }));
            """
        )
    )

    assert result == {
        "explicit": {
            "answers": ["逐题回答一", "逐题回答二"],
            "statuses": ["answered", "answered"],
            "summaries": ["第一项事实", "第二项事实"],
            "repairNotices": [
                "生成的日期比较口径没有完整覆盖业务问题，已重新分析。"
            ],
        },
        "historical": {
            "answers": [None, None],
            "statuses": ["unbound", "unbound"],
        },
    }
