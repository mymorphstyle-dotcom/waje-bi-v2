# Design QA

## Source and implementation

- Visual reference: `/var/folders/jp/30h2c1fs7wd5rt_qx6h8ykv80000gn/T/codex-clipboard-28d5fe52-00ab-40dd-bfdb-81b8341f1b47.png`
- Reference size: 2920 × 1888 px
- Implementation viewport: 1980 × 1280 CSS px, matching the reference aspect ratio
- State: active analysis with four Planner issues and the overall status `分析中`
- Comparison method: source and implementation were scaled to the same dimensions, placed side by side in one temporary comparison image, and inspected together. The temporary QA image was removed after review.

## Fidelity review

- Placement: the card sits independently at the upper-right of the chat workspace and does not enter the conversation log or composer stack.
- Density: the card is 312 px wide, uses a 46 px header, 13 px title, 12 px issue copy, compact dividers, and no explanatory chrome.
- Hierarchy: one title, one status dot and label, and a flat ordered issue list. The visual weight stays below the main conversation.
- Colors: the card uses the existing WAJE neutral surfaces and the AI Elements primary blue only for the active status.
- Layout safety: at wide viewports the card occupies the right rail; at narrower desktop widths the conversation and composer reserve space for it; mobile keeps it as a bounded, scrollable overlay.
- Assets: no new image assets, handcrafted SVGs, or decorative illustrations were introduced.

## Interaction and accessibility

- The card is an `aside` labelled `本轮待解决问题`.
- The header is a real button with `aria-expanded`.
- Browser verification confirmed that collapse changes `aria-expanded` from `true` to `false` and removes the issue list from the visible DOM.
- The visible issue list comes only from the Planner projection bound to the accepted `plan_revision_id` and `planner_proposal_id`.
- Browser console warnings and errors in the verified state: none.

## Result

final result: passed
