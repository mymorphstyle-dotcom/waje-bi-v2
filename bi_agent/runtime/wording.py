import re
from collections.abc import Mapping, Sequence
from typing import Any


CAUSAL_WORDING = re.compile(
    r"\b(cause|caused|causes|causing|because|due to|drives?|driven by)\b"
    r"|导致|造成|因果|归因于|驱动",
    re.IGNORECASE,
)
OVER_STRONG_WORDING = re.compile(
    r"\b(reliable|strong confidence|high confidence)\b|可靠|高置信",
    re.IGNORECASE,
)
SINGLE_PERIOD_CONFIDENCE = re.compile(
    r"\b(statistical confidence|non-random|statistically significant)\b"
    r"|统计置信|非随机|显著",
    re.IGNORECASE,
)


def wording_warnings(
    claims: Sequence[Mapping[str, Any]],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    warnings = []
    for index, claim in enumerate(claims):
        text = str(claim.get("text", ""))
        refs = claim.get("evidence_refs", ())
        if CAUSAL_WORDING.search(text):
            has_causal_evidence = any(
                evidence_by_ref.get(ref, {}).get("evidence_type") == "causal_evidence"
                for ref in refs
            )
            if not has_causal_evidence:
                warnings.append(
                    {
                        "code": "causal_wording_without_causal_evidence",
                        "claim_index": index,
                        "message": "Causal wording requires causal_evidence.",
                    }
                )

        if OVER_STRONG_WORDING.search(text) and any(
            evidence_by_ref.get(ref, {}).get("strength") == "medium" for ref in refs
        ):
            warnings.append(
                {
                    "code": "over_strong_evidence_wording",
                    "claim_index": index,
                    "message": "Medium evidence cannot use high-confidence wording.",
                }
            )

        if SINGLE_PERIOD_CONFIDENCE.search(text) and any(
            _comparable_periods(evidence_by_ref.get(ref, {})) <= 1 for ref in refs
        ):
            warnings.append(
                {
                    "code": "single_period_confidence_wording",
                    "claim_index": index,
                    "message": "Single-period evidence cannot support statistical confidence wording.",
                }
            )
    return warnings


def _comparable_periods(evidence: Mapping[str, Any]) -> int:
    try:
        return int(evidence.get("typed_payload", {}).get("comparable_periods", 0))
    except (TypeError, ValueError):
        return 0
