from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


RECOMMENDATION_MARKER = "（推荐）"


def strip_recommendation_marker(value: Any) -> str:
    label = str(value or "").strip()
    while label.endswith(RECOMMENDATION_MARKER):
        label = label[: -len(RECOMMENDATION_MARKER)].rstrip()
    return label


def clarification_labels_match(left: Any, right: Any) -> bool:
    return (
        strip_recommendation_marker(left).rstrip("。")
        == strip_recommendation_marker(right).rstrip("。")
    )


def project_clarification_recommendation(
    clarification: Mapping[str, Any],
    *,
    recommended_choice_id: str = "",
) -> dict[str, Any]:
    """Project one stable recommended choice into all user-visible labels."""

    output = dict(clarification)
    actions = [
        dict(action)
        for action in clarification.get("choice_actions") or ()
        if isinstance(action, Mapping)
    ]
    business_actions = [
        action
        for action in actions
        if str(action.get("action_kind") or "") != "user_redirect"
        and str(action.get("choice_id") or "")
    ]
    selected_id = str(recommended_choice_id or "").strip()
    if selected_id not in {
        str(action.get("choice_id") or "") for action in business_actions
    }:
        raw_recommended = clarification.get("recommended_assumption") or {}
        recommended_label = (
            raw_recommended.get("option")
            if isinstance(raw_recommended, Mapping)
            else raw_recommended
        )
        selected_id = next(
            (
                str(action.get("choice_id") or "")
                for action in business_actions
                if clarification_labels_match(
                    action.get("business_label")
                    or action.get("business_semantics"),
                    recommended_label,
                )
            ),
            "",
        )
    if not selected_id and business_actions:
        selected_id = str(business_actions[0].get("choice_id") or "")

    original_actions = [dict(action) for action in actions]
    projected_actions: list[dict[str, Any]] = []
    for action in actions:
        choice_id = str(action.get("choice_id") or "")
        raw_label = action.get("business_label") or action.get(
            "business_semantics"
        )
        base_label = strip_recommendation_marker(raw_label)
        if choice_id == selected_id and str(
            action.get("action_kind") or ""
        ) != "user_redirect":
            visible_label = f"{base_label}{RECOMMENDATION_MARKER}"
        else:
            visible_label = base_label
        projected_actions.append(
            {
                **action,
                "business_label": visible_label,
            }
        )

    projected_by_id = {
        str(action.get("choice_id") or ""): action
        for action in projected_actions
        if str(action.get("choice_id") or "")
    }
    questions = []
    raw_questions = clarification.get("questions") or ()
    if isinstance(raw_questions, Sequence) and not isinstance(
        raw_questions, (str, bytes)
    ):
        for question in raw_questions:
            if not isinstance(question, Mapping):
                continue
            rendered_options = []
            for option in question.get("options") or ():
                matching_action = next(
                    (
                        action
                        for action in original_actions
                        if clarification_labels_match(
                            action.get("business_label")
                            or action.get("business_semantics"),
                            option,
                        )
                    ),
                    None,
                )
                if matching_action is None:
                    rendered_options.append(str(option))
                    continue
                choice_id = str(matching_action.get("choice_id") or "")
                rendered_options.append(
                    str(
                        projected_by_id.get(choice_id, {}).get(
                            "business_label",
                            option,
                        )
                    )
                )
            questions.append(
                {
                    **dict(question),
                    "options": rendered_options,
                }
            )

    selected_action = projected_by_id.get(selected_id, {})
    selected_label = str(selected_action.get("business_label") or "")
    output["choice_actions"] = projected_actions
    if questions:
        output["questions"] = questions
    if selected_id and selected_label:
        output["recommended_choice_id"] = selected_id
        output["recommended_assumption"] = {"option": selected_label}
    return output
