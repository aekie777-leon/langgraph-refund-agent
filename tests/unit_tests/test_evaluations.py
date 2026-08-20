"""Regression tests for the versioned offline AI evaluation subsystem."""

import argparse
import copy
import json
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent.evals.__main__ import _run
from agent.evals.dataset import load_evaluation_manifest
from agent.evals.models import (
    EvaluationGates,
    EvaluationManifest,
    RoutingExpected,
    RoutingScenario,
    WorkflowSafetyExpected,
)
from agent.evals.reporting import report_json, report_markdown
from agent.evals.runner import run_evaluation

pytestmark = pytest.mark.anyio


async def test_packaged_evaluation_passes_every_configured_gate() -> None:
    manifest = load_evaluation_manifest()

    report = await run_evaluation(manifest)

    assert report.ready is True
    assert report.total == 57
    assert report.passed == report.total
    assert {summary.suite for summary in report.suites} == {
        "routing",
        "order_detection",
        "risk_rules",
        "semantic_risk",
        "formal_complaint",
        "operation_extraction",
        "workflow_safety",
    }
    assert report.coverage.languages == ("en", "zh")
    assert report.coverage.safety_categories == (
        "legal",
        "regulatory",
        "reputation",
        "self_harm",
        "violence",
    )


async def test_wrong_golden_expectation_fails_overall_and_safety_gates() -> None:
    manifest = load_evaluation_manifest()
    target = next(
        scenario
        for scenario in manifest.scenarios
        if scenario.id == "workflow-hard-critical-en"
    )
    changed = target.model_copy(
        update={"expected": WorkflowSafetyExpected(route="order_query")}
    )
    failing = manifest.model_copy(
        update={
            "scenarios": tuple(
                changed if scenario.id == target.id else scenario
                for scenario in manifest.scenarios
            )
        }
    )

    report = await run_evaluation(EvaluationManifest.model_validate(failing))

    assert report.ready is False
    assert next(
        result for result in report.results if result.id == target.id
    ).passed is False
    assert {gate.name: gate.passed for gate in report.gates} == {
        "overall_pass_rate": False,
        "safety_pass_rate": False,
        "required_language_coverage": True,
    }


async def test_reports_are_deterministic_and_omit_scenario_inputs() -> None:
    manifest = load_evaluation_manifest()

    first = await run_evaluation(manifest)
    second = await run_evaluation(manifest)
    json_evidence = report_json(first)
    markdown_evidence = report_markdown(first)

    assert report_json(second) == json_evidence
    assert "Please refund ORD-10001." not in json_evidence
    assert "Please refund ORD-10001." not in markdown_evidence
    assert all("input" not in result for result in json.loads(json_evidence)["results"])
    assert "does not claim production-model accuracy" in markdown_evidence


async def test_cli_verifier_rejects_a_stale_committed_report(tmp_path) -> None:
    report_path = tmp_path / "evaluation-report.json"
    report_path.write_text("{}\n", encoding="utf-8")
    arguments = argparse.Namespace(
        dataset=None,
        json_output=None,
        markdown_output=None,
        verify_json=report_path,
        quiet=True,
    )

    assert await _run(arguments) == 1

    current = await run_evaluation(load_evaluation_manifest())
    report_path.write_text(report_json(current), encoding="utf-8")
    assert await _run(arguments) == 0


async def test_manifest_rejects_duplicate_ids_and_unknown_fields() -> None:
    raw = cast(dict[str, Any], load_evaluation_manifest().model_dump(mode="json"))
    unknown = copy.deepcopy(raw)
    unknown["scenarios"][0]["unexpected"] = True

    with pytest.raises(ValidationError) as error:
        EvaluationManifest.model_validate(unknown)

    assert "unexpected" in str(error.value)

    duplicate = copy.deepcopy(raw)
    duplicate["scenarios"][1]["id"] = duplicate["scenarios"][0]["id"]
    with pytest.raises(ValidationError, match="duplicate evaluation scenario IDs"):
        EvaluationManifest.model_validate(duplicate)


async def test_manifest_rejects_incomplete_suite_coverage() -> None:
    scenario = RoutingScenario(
        id="routing-only-en",
        suite="routing",
        language="en",
        input="Refund this order.",
        expected=RoutingExpected(
            intent="refund_request",
            human_handoff_requested=False,
        ),
    )

    with pytest.raises(ValidationError, match="missing suites"):
        EvaluationManifest(
            schema_version=1,
            dataset_version="1.0.0",
            gates=EvaluationGates(
                overall_min_pass_rate=1.0,
                safety_min_pass_rate=1.0,
                required_languages=("en", "zh"),
            ),
            scenarios=(scenario,),
        )
