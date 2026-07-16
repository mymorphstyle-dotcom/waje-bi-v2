# LLM Task Split And Stability Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for every behavior change and superpowers:verification-before-completion before reporting results.

**Goal:** Separate machine route planning from route business narration, route high-impact LLM decisions to the configured Pro model, and compare Flash/Pro with thinking enabled/disabled on the Case B replay inputs.

**Architecture:** `analysis_route_plan` returns only the proposed machine route and analysis requirements. The local compiler remains authoritative. `final_route_narrative` receives a Chinese business projection of the accepted route and returns display-only sections bound by opaque step references; failure of this advisory narration is recorded without stopping the accepted route. The shared LLM client selects the configured model tier per task. A standalone replay runner calls the official OpenAI-compatible endpoint concurrently and stores final JSON, validation outcomes, usage, and latency while discarding hidden reasoning content.

**Tech Stack:** Python, LangGraph, OpenAI-compatible Chat Completions, unittest/pytest-compatible tests, JSON artifacts.

## Global Constraints

- Preserve all existing dirty-worktree changes; do not reset, clean, checkout, stage, commit, or overwrite artifacts.
- Keep SQL safety, permission, data contracts, evidence provenance, snapshot/release authority, and verifier decisions as hard local boundaries.
- Do not add rules for a specific date, question string, Case ID, or one DeepSeek output.
- Preserve every provider final response used by the experiment; do not store or expose `reasoning_content`.
- Do not add backward compatibility for the old combined LLM task.
- Do not set `max_tokens`; use an explicit positive timeout only for the live experiment.

---

### Task 1: Split route planning and route narration

**Files:**
- Modify: `bi_agent/runtime/llm_prompts.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `tests/phase4/fake_llm.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Produces: `analysis_route_plan -> {requested_nodes, analysis_requirements, display_summary}`.
- Produces: `final_route_narrative -> {route_summary, sections, decision_summary, display_summary}`.
- Preserves: accepted `analysis_route` machine fields and local compiler authority.

- [ ] Write tests proving the two tasks have disjoint schemas and non-conflicting prompts.
- [ ] Run the focused tests and confirm they fail because the tasks do not exist yet.
- [ ] Implement the two prompt contracts and switch workflow calls.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Project narration input and isolate advisory failure

**Files:**
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Consumes: accepted capability IDs and local capability cards.
- Produces: a narration payload containing only `step_ref`, Chinese business labels, purpose, role, target/baseline labels, and direction status.
- Produces on failure: `route_narrative_status=unavailable` plus preserved LLM audit; execution continues with the accepted route.

- [ ] Write tests proving machine IDs and budget state never enter the narration prompt.
- [ ] Write a test proving exhausted narration retries do not prevent schema inspection.
- [ ] Run the tests and observe the expected failures.
- [ ] Implement the projection, validator, local binding, and failure isolation.
- [ ] Run the tests and confirm the accepted machine route remains unchanged.

### Task 3: Route high-impact tasks to the configured critical model

**Files:**
- Modify: `bi_agent/runtime/llm_client.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Test: `tests/phase4/test_llm_workflow.py`

**Interfaces:**
- Consumes: `WAJE_LLM_CRITICAL_MODEL` and an internal critical-task classification.
- Produces: actual model identity in each LLM audit.

- [ ] Write tests proving critical tasks select the critical model and advisory tasks retain the default model.
- [ ] Run the tests and observe the critical task still uses the default model.
- [ ] Implement model-tier selection in the shared provider client.
- [ ] Run focused client and workflow tests.

### Task 4: Build and run the concurrent replay experiment

**Files:**
- Create: `tools/phase7/run_llm_node_stability_replay.py`
- Test: `tests/phase7/test_llm_node_stability_replay.py`
- Create at runtime: `artifacts/phase7/human-led-q1/case-b-llm-stability-<timestamp>/`

**Interfaces:**
- Consumes: saved Case B prompt messages plus reconstructed post-compiler narration input.
- Runs: `deepseek-v4-flash` and `deepseek-v4-pro`, each with thinking explicitly enabled and disabled.
- Produces: one record per call, aggregate schema/semantic/leakage/stability scores, latency percentiles, and token usage.

- [ ] Write tests for replay extraction, thinking request options, validators, and aggregation.
- [ ] Run tests and observe failures before the runner exists.
- [ ] Implement the runner with bounded concurrency and no secret output.
- [ ] Run a one-call connectivity probe for every model/mode combination.
- [ ] Run five repetitions per selected node and combination concurrently.
- [ ] Review raw final outputs, aggregate stability, and outliers before recommending routing policy.

### Task 5: Verification and handoff

**Files:**
- Review only: all files changed above and the new experiment artifact.

- [ ] Run focused prompt, workflow, client, and replay-runner tests.
- [ ] Inspect the working-tree diff for accidental overlap or secret material.
- [ ] Report selected nodes, exact experimental matrix, schema pass rate, semantic pass rate, leakage rate, latency, consistency, and a model/thinking recommendation for user confirmation.
