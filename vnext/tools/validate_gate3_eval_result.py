#!/usr/bin/env python3
"""Validate Gate 3 per-cell results with strict three-layer aggregation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "evals" / "gate3"
SCHEMA_PATH = (
    EVAL_ROOT / "evaluation-run-result.schema.json"
)
RUN_MANIFEST_PATH = EVAL_ROOT / "manifests" / "run-manifest.json"
GRADER_REGISTRY_PATH = EVAL_ROOT / "registries" / "grader-registry.json"
AUTHORITY_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "authority-conformance-profiles.json"
)
WORLD_PROFILES_PATH = (
    EVAL_ROOT / "profiles" / "cross-gate-world-profiles.json"
)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonical_authority() -> dict[str, Any]:
    return {
        "run_manifest": json.loads(
            RUN_MANIFEST_PATH.read_text(encoding="utf-8")
        ),
        "grader_registry": json.loads(
            GRADER_REGISTRY_PATH.read_text(encoding="utf-8")
        ),
        "authority_profiles": json.loads(
            AUTHORITY_PROFILES_PATH.read_text(encoding="utf-8")
        ),
        "world_profiles": json.loads(
            WORLD_PROFILES_PATH.read_text(encoding="utf-8")
        ),
        "artifact_index": None,
    }


def derive_final_verdict(result: Mapping[str, Any]) -> str:
    if result["leakage_detected"]:
        return "invalid"
    layer_verdicts = [
        result["layer_results"][layer]["verdict"]
        for layer in (
            "product_behavior",
            "authority_conformance",
            "implementation",
        )
    ]
    if result["critical_vetoes"] or "fail" in layer_verdicts:
        return "fail"
    if "invalid" in layer_verdicts:
        return "invalid"
    if not result["artifact_completeness"] or "blocked" in layer_verdicts:
        return "blocked"
    if all(verdict == "pass" for verdict in layer_verdicts):
        return "pass"
    return "invalid"


def validate_result(
    result: Mapping[str, Any],
    *,
    authority: Mapping[str, Any] | None = None,
) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    findings = [
        "{}: {}".format(
            "/".join(str(part) for part in error.absolute_path) or "<root>",
            error.message,
        )
        for error in Draft202012Validator(schema).iter_errors(result)
    ]
    if findings:
        return findings
    authority = authority or _canonical_authority()
    run_manifest = authority["run_manifest"]
    grader_registry = authority["grader_registry"]
    authority_profiles = authority["authority_profiles"]
    world_profiles = authority["world_profiles"]
    if run_manifest["status"] != "frozen":
        findings.append("run manifest must be frozen")
        return findings
    if result["run_manifest_sha256"] != _canonical_sha256(run_manifest):
        findings.append("run_manifest_sha256 does not bind canonical manifest")
    run_cells = {
        cell["run_cell_id"]: cell for cell in run_manifest["run_cells"]
    }
    if len(run_cells) != len(run_manifest["run_cells"]):
        findings.append("run manifest contains duplicate run_cell_id values")
    cell = run_cells.get(result["run_cell_id"])
    if cell is None:
        findings.append("run_cell_id is absent from frozen run manifest")
        return findings
    for field in (
        "episode_id",
        "episode_core_sha256",
        "world_profile_ref",
        "world_profile_sha256",
        "authority_profile_ref",
        "authority_profile_sha256",
        "product_grader_profile_ref",
    ):
        if result[field] != cell[field]:
            findings.append("{} does not match frozen run cell".format(field))
    world_profiles_by_id = {
        profile["profile_id"]: profile
        for profile in world_profiles["profiles"]
    }
    authority_profiles_by_id = {
        profile["profile_id"]: profile
        for profile in authority_profiles["profiles"]
    }
    grader_profiles_by_id = {
        profile["profile_id"]: profile
        for profile in grader_registry["profiles"]
    }
    world_profile = world_profiles_by_id.get(cell["world_profile_ref"])
    authority_profile = authority_profiles_by_id.get(
        cell["authority_profile_ref"]
    )
    product_profile = grader_profiles_by_id.get(
        cell["product_grader_profile_ref"]
    )
    if (
        world_profile is None
        or _canonical_sha256(world_profile) != cell["world_profile_sha256"]
    ):
        findings.append("frozen run cell has invalid world profile binding")
    if (
        authority_profile is None
        or _canonical_sha256(authority_profile)
        != cell["authority_profile_sha256"]
    ):
        findings.append(
            "frozen run cell has invalid authority profile binding"
        )
    if product_profile is None:
        findings.append("frozen run cell has unknown product grader profile")
        required_product_checks: set[str] = set()
    else:
        required_product_checks = set(
            product_profile["required_predicate_ids"]
        )
    required_checks = {
        "product_behavior": required_product_checks,
        "authority_conformance": set(
            authority_profile["required_invariant_ids"]
            if authority_profile is not None
            else []
        ),
        "implementation": {
            item["check_id"]
            for item in grader_registry["implementation_checks"]
        },
    }
    for layer_name, layer_result in result["layer_results"].items():
        check_ids = [
            check["check_id"] for check in layer_result["check_results"]
        ]
        if len(check_ids) != len(set(check_ids)):
            findings.append(
                "{} contains duplicate check ids".format(layer_name)
            )
        if set(check_ids) != required_checks[layer_name]:
            findings.append(
                "{} check set does not match frozen authority".format(
                    layer_name
                )
            )
    if result["derived_final_verdict"] == "pass":
        artifact_index = authority.get("artifact_index")
        if artifact_index is None:
            findings.append(
                "passing result requires a runner-verified artifact index"
            )
        else:
            indexed_layers = artifact_index.get(result["run_cell_id"], {})
            for layer_name, layer_result in result[
                "layer_results"
            ].items():
                if set(layer_result["artifact_sha256s"]) != set(
                    indexed_layers.get(layer_name, [])
                ):
                    findings.append(
                        "{} artifacts do not match runner index".format(
                            layer_name
                        )
                    )
    expected = derive_final_verdict(result)
    if result["derived_final_verdict"] != expected:
        findings.append(
            "derived_final_verdict must be {}, got {}".format(
                expected, result["derived_final_verdict"]
            )
        )
    for layer_name, layer_result in result["layer_results"].items():
        child_verdicts = {
            check["verdict"] for check in layer_result["check_results"]
        }
        if layer_result["verdict"] == "pass" and child_verdicts != {"pass"}:
            findings.append(
                "{} cannot pass with child verdicts {}".format(
                    layer_name, sorted(child_verdicts)
                )
            )
    return findings


def contract_self_test() -> list[str]:
    artifact_hashes = {
        "product_behavior": "a" * 64,
        "authority_conformance": "b" * 64,
        "implementation": "c" * 64,
    }
    world_profile = {"profile_id": "WORLD-PROFILE-SELF-TEST"}
    authority_profile = {
        "profile_id": "AUTHORITY-PROFILE-SELF-TEST",
        "required_invariant_ids": ["authority_check"],
    }
    grader_registry = {
        "profiles": [
            {
                "profile_id": "GRADER-PRODUCT-SELF-TEST",
                "required_predicate_ids": ["product_check"],
            }
        ],
        "implementation_checks": [{"check_id": "implementation_check"}],
    }
    run_cell = {
        "run_cell_id": "CELL-SELF-TEST",
        "episode_id": "G3-TEST-001",
        "episode_core_sha256": "d" * 64,
        "world_profile_ref": world_profile["profile_id"],
        "world_profile_sha256": _canonical_sha256(world_profile),
        "authority_profile_ref": authority_profile["profile_id"],
        "authority_profile_sha256": _canonical_sha256(authority_profile),
        "product_grader_profile_ref": "GRADER-PRODUCT-SELF-TEST",
    }
    run_manifest = {"status": "frozen", "run_cells": [run_cell]}
    authority = {
        "run_manifest": run_manifest,
        "grader_registry": grader_registry,
        "authority_profiles": {"profiles": [authority_profile]},
        "world_profiles": {"profiles": [world_profile]},
        "artifact_index": {
            run_cell["run_cell_id"]: {
                layer: [digest]
                for layer, digest in artifact_hashes.items()
            }
        },
    }
    required_checks = {
        "product_behavior": "product_check",
        "authority_conformance": "authority_check",
        "implementation": "implementation_check",
    }
    result = {
        "result_version": "gate3.eval-result.v1",
        "run_cell_id": run_cell["run_cell_id"],
        "run_manifest_sha256": _canonical_sha256(run_manifest),
        **{
            field: run_cell[field]
            for field in (
                "episode_id",
                "episode_core_sha256",
                "world_profile_ref",
                "world_profile_sha256",
                "authority_profile_ref",
                "authority_profile_sha256",
                "product_grader_profile_ref",
            )
        },
        "layer_results": {
            layer: {
                "verdict": "pass",
                "check_results": [
                    {
                        "check_id": required_checks[layer],
                        "verdict": "pass",
                    }
                ],
                "artifact_sha256s": [artifact_hashes[layer]],
            }
            for layer in required_checks
        },
        "critical_vetoes": [],
        "artifact_completeness": True,
        "leakage_detected": False,
        "derived_final_verdict": "pass",
    }
    findings: list[str] = []
    if validate_result(result, authority=authority):
        findings.append("trusted result fixture did not pass")
    forged = json.loads(json.dumps(result))
    forged["run_manifest_sha256"] = "0" * 64
    forged["layer_results"]["product_behavior"]["check_results"] = [
        {"check_id": "dummy", "verdict": "pass"}
    ]
    if not validate_result(forged, authority=authority):
        findings.append("forged manifest/check fixture passed")
    no_artifacts = dict(authority)
    no_artifacts["artifact_index"] = None
    if not validate_result(result, authority=no_artifacts):
        findings.append("result without runner artifact index passed")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=Path)
    arguments = parser.parse_args()
    result = json.loads(arguments.result.read_text(encoding="utf-8"))
    findings = validate_result(result)
    print(
        json.dumps(
            {
                "status": "failed" if findings else "passed",
                "findings": findings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
