import re
from collections.abc import Mapping, Sequence
from typing import Any


CAUSAL_WORDING = re.compile(
    r"\b(cause|caused|causes|causing|because|due to|drives?|driven by)\b"
    r"|导致|造成|因果|归因于|驱动",
    re.IGNORECASE,
)


def wording_warnings(
    claims: Sequence[Mapping[str, Any]],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    warnings = []
    for index, claim in enumerate(claims):
        text = str(claim.get("text", ""))
        if not CAUSAL_WORDING.search(text):
            continue
        refs = claim.get("evidence_refs", ())
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
    return warnings
