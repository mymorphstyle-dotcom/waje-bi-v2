import pytest

from bi_agent.runtime.controlled_investigation_runtime import (
    ControlledInvestigationOutput,
)
from bi_agent.runtime.controlled_investigation_workflow import (
    _validate_numeric_closure,
)


def _output(text: str) -> ControlledInvestigationOutput:
    return ControlledInvestigationOutput.model_validate(
        {
            "findings": [
                {
                    "findingKind": "mechanism",
                    "text": text,
                    "sourceRefs": ["source:metric"],
                }
            ],
            "limitationRefs": [],
        }
    )


@pytest.mark.parametrize(
    "text",
    (
        "付费金额为308,240,309。",
        "环比增长约1.35%。",
        "环比增长约1.3%。",
    ),
)
def test_numeric_closure_accepts_exact_and_rounded_source_values(
    text: str,
) -> None:
    _validate_numeric_closure(
        _output(text),
        {
            "source:metric": {
                "paidAmount": 308_240_309,
                "changeRate": 0.013470681,
            }
        },
    )


def test_numeric_closure_rejects_unbound_numeric_claim() -> None:
    with pytest.raises(
        ValueError,
        match="controlled_investigation_numeric_conflict",
    ):
        _validate_numeric_closure(
            _output("环比增长约9.99%。"),
            {
                "source:metric": {
                    "paidAmount": 308_240_309,
                    "changeRate": 0.013470681,
                }
            },
        )
