from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from bi_agent.runtime.agent_sdk_contracts import AgentToolResult, WajeAgentTool
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


class ListAvailableCapabilitiesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def capability_catalog_tool(
    registry: RuntimeContractRegistry,
) -> WajeAgentTool:
    """Expose the reviewed runtime binding as a customer-safe capability view."""

    def list_capabilities(_arguments: Mapping[str, Any]) -> AgentToolResult:
        capabilities = []
        for axis_id in registry.analysis_axis_ids:
            axis = registry.analysis_axis(axis_id)
            capabilities.append(
                {
                    "capabilityRef": axis_id,
                    "businessName": str(axis["business_name"]),
                    "semantics": str(axis["semantics"]),
                }
            )
        return AgentToolResult(
            status="succeeded",
            output={
                "contractVersion": registry.contract_version,
                "analysisCapabilities": capabilities,
                "availabilityBoundary": (
                    "具体问题仍需通过当前数据合同、时间范围、粒度和证据检查。"
                ),
            },
            artifactRefs=[],
            materialRefs=[],
            limitationRefs=[],
            retryability="never",
            customerSummary="已读取当前审核通过的分析能力目录。",
            technicalDetailRef=None,
        )

    return WajeAgentTool(
        name="list_available_capabilities",
        description=(
            "Read the current reviewed WAJE analysis capability catalog and its "
            "data-contract availability boundary without starting a BI run."
        ),
        input_model=ListAvailableCapabilitiesInput,
        handler=list_capabilities,
    )


__all__ = (
    "ListAvailableCapabilitiesInput",
    "capability_catalog_tool",
)
