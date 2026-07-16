# Human-led Q1 material clarification and factor framework plan

## Goal

Make a real question run preserve three business truths across clarification and resume:

1. Relative dates are resolved once against the run clock and written back as exact dates.
2. A selected clarification option carries a typed material change, so choosing the previous day binds `previous_day` and the same baseline question does not reopen.
3. User-mentioned factors remain priority checks inside the paid-amount decomposition framework; they do not replace the contract-backed formula closure.

The implementation must stay generic across dates, wording, cases, and provider output. Existing permission, SQL safety, snapshot/release, evidence, provenance, and verifier boundaries remain unchanged.

## Constraints

- Preserve the dirty worktree and all existing edits.
- Do not reset, clean, checkout, overwrite, stage, commit, or run the full suite.
- Edit with small patches only.
- Use `ConversationAgentCore` or the Gateway for the real rerun.
- Keep DeepSeek business output unchanged in artifacts.

## RED tests

1. A material baseline clarification exposes stable local option IDs and typed actions. Exactly one option is recommended, with `previous_day` recommended for a daily-change question, plus the free-form escape.
2. A Gateway-shaped answer carrying `selectedOptionId` resolves the stored action into `clarification_choice={answer_text, baseline_candidates}`.
3. A boundary-discovered baseline ambiguity is persisted into the resume authority even when the initial intent omitted `ambiguous_slots`.
4. Resuming with the typed previous-day choice binds `baseline_candidates=[previous_day]` and closes the baseline ambiguity.
5. A relative daily target such as `yesterday` is resolved from the captured run `as_of` and business timezone, then written back to accepted intent and analysis context as an exact date.
6. Paid-amount change compilation includes the formula-decomposition obligation even when the provider route omits it; explicitly requested factors keep user-priority provenance and cannot close the formula candidate set.
7. Formula evidence without contribution, residual, and reconciliation cannot claim quantified decomposition.

Run each focused test first and record the expected failure before touching production code.

## Implementation

1. Add a local material-choice builder for baseline ambiguity, backed by canonical baseline IDs.
2. Persist stable `choice_id` values in `ClarificationOption` and resolve selections by `selectedOptionId` first, with exact-label and recommended-choice fallbacks.
3. Project the validated action's material patch into the workflow clarification choice; keep display text separate from authority fields.
4. Merge material ambiguity found at the boundary into the accepted intent/source envelope before interruption.
5. Resolve relative dates locally from one captured `as_of` and the metric business timezone; write exact dates into the run's intent and analysis context.
6. Enforce paid-amount formula decomposition in the local capability obligation/contract layer and record explicit factors as priority provenance. Preserve contract-required sibling metrics.
7. Tighten formula claim strength so field presence alone cannot produce quantified evidence.

## Verification and Case A rerun

1. Run only the new focused tests and directly adjacent existing clarification/contract tests.
2. Inspect the diff for accidental changes and verify the original dirty files remain preserved.
3. Rerun the original Case A question with no fixed clock through the Gateway or `ConversationAgentCore`.
4. Apply the already-approved previous-day option through its real option ID.
5. Save the rerun under a new `artifacts/phase7/human-led-q1/` directory and report the business path, data boundary, raw DeepSeek answer if reached, claims, verifier outcome, and any remaining gap.
