"use client";

import { Check, RefreshCw } from "lucide-react";

import type {
  TraceReasoning,
  TraceReasoningIssue,
} from "./run-reasoning-contracts";

export function ReasoningTimeline({
  reasoning,
}: {
  reasoning: TraceReasoning;
}) {
  const acceptedFacts = reasoning.claims.filter(
    (claim) => claim.verificationStatus === "accepted",
  );
  const issueIds = new Set(reasoning.issues.map((issue) => issue.issueId));
  const rootIssues = reasoning.issues.filter(
    (issue) => !issue.parentIssueId || !issueIds.has(issue.parentIssueId),
  );
  const repairNotices = reasoning.repairNotices ?? [];

  return (
    <div className="reasoning-timeline">
      <ReasoningRepairNotices notices={repairNotices} />

      {reasoning.issues.length ? (
        <section className="reasoning-plan" aria-label="分析问题">
          <div className="reasoning-question-trees">
            {rootIssues.map((rootIssue) => (
              <PlannerQuestionTree
                allIssues={reasoning.issues}
                key={rootIssue.issueId}
                rootIssue={rootIssue}
              />
            ))}
          </div>
        </section>
      ) : null}

      {acceptedFacts.length ? (
        <section className="reasoning-facts" aria-label="核验事实">
          <ol>
            {acceptedFacts.map((fact, index) => (
              <li id={`verified-fact-${index + 1}`} key={fact.claimRef}>
                <div className="reasoning-fact-heading">
                  <span aria-hidden="true"><Check size={12} /></span>
                  <strong>{fact.summary}</strong>
                  <small>{fact.usedInAnswer ? "已用于回答" : "待写入回答"}</small>
                </div>
                {fact.facts.length ? (
                  <dl>
                    {fact.facts.map((item) => (
                      <div key={`${item.name}:${item.value}`}>
                        <dt>{item.name}</dt>
                        <dd>{item.value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
              </li>
            ))}
          </ol>
        </section>
      ) : null}
    </div>
  );
}

export function ReasoningRepairNotices({
  notices,
}: {
  notices: string[];
}) {
  if (!notices.length) return null;
  return (
    <section className="reasoning-repairs" aria-label="分析过程修正">
      <div className="reasoning-repair-heading">
        <RefreshCw aria-hidden="true" size={14} />
        <strong>分析过程修正</strong>
      </div>
      <ol>
        {notices.map((notice) => (
          <li key={notice}>{notice}</li>
        ))}
      </ol>
    </section>
  );
}

function PlannerQuestionTree({
  rootIssue,
  allIssues,
}: {
  rootIssue: TraceReasoningIssue;
  allIssues: TraceReasoningIssue[];
}) {
  const children = childIssues(rootIssue.issueId, allIssues);
  return (
    <article className="reasoning-question-tree">
      <div className="reasoning-core-question">
        <span>核心问题</span>
        <p>{rootIssue.question}</p>
      </div>
      {children.length ? (
        <div className="reasoning-support-plan">
          <p className="reasoning-support-intro">
            为回答核心问题，需要完成以下支撑问题
          </p>
          <ol className="reasoning-support-branches">
            {children.map((issue, index) => (
              <PlannerQuestionBranch
                allIssues={allIssues}
                index={index}
                issue={issue}
                key={issue.issueId}
              />
            ))}
          </ol>
        </div>
      ) : null}
    </article>
  );
}

function PlannerQuestionBranch({
  issue,
  allIssues,
  index,
}: {
  issue: TraceReasoningIssue;
  allIssues: TraceReasoningIssue[];
  index: number;
}) {
  const children = childIssues(issue.issueId, allIssues);
  return (
    <li>
      <div className="reasoning-branch-heading">
        <span>{String(index + 1).padStart(2, "0")}</span>
        <div>
          <small>支撑问题</small>
          <p>{issue.question}</p>
        </div>
      </div>
      {children.length ? (
        <div className="reasoning-verification-group">
          <span>进一步拆分</span>
          <ul>
            {children.map((child) => (
              <PlannerVerificationIssue
                allIssues={allIssues}
                issue={child}
                key={child.issueId}
              />
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  );
}

function PlannerVerificationIssue({
  issue,
  allIssues,
}: {
  issue: TraceReasoningIssue;
  allIssues: TraceReasoningIssue[];
}) {
  const children = childIssues(issue.issueId, allIssues);
  return (
    <li>
      <p>{issue.question}</p>
      {children.length ? (
        <ul>
          {children.map((child) => (
            <PlannerVerificationIssue
              allIssues={allIssues}
              issue={child}
              key={child.issueId}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function childIssues(
  issueId: string,
  allIssues: TraceReasoningIssue[],
) {
  return allIssues.filter((issue) => issue.parentIssueId === issueId);
}
