"""Execute deterministic model-contract and workflow-safety evaluations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langchain_core.messages import HumanMessage

from agent.evals.dataset import evaluation_manifest_digest
from agent.evals.models import (
    EvaluationCaseResult,
    EvaluationCoverage,
    EvaluationGateResult,
    EvaluationManifest,
    EvaluationReport,
    EvaluationScenario,
    FormalComplaintScenario,
    OperationExtractionScenario,
    OrderDetectionScenario,
    RiskRulesScenario,
    RoutingScenario,
    SemanticRiskScenario,
    SuiteName,
    SuiteSummary,
    WorkflowSafetyScenario,
)
from agent.risk_matcher import RiskRuleMatcher
from agent.routing import (
    route_after_risk_rules,
    route_after_semantic_risk,
    route_by_intent_and_risk,
)
from agent.schemas import (
    FormalComplaintDetection,
    OperationRequestExtraction,
    OrderDetection,
    Route,
    SemanticRiskDetection,
)
from agent.showcase.scenario_models import (
    ShowcaseComplaintClassifier,
    ShowcaseOperationExtractor,
    ShowcaseOrderDetector,
    ShowcaseRiskClassifier,
    ShowcaseRouter,
)
from agent.state import RefundState

_SAFETY_SUITES = frozenset({"risk_rules", "semantic_risk", "workflow_safety"})


class _RouterAdapter(Protocol):
    async def ainvoke(self, messages: Sequence[Any]) -> Route: ...


class _OrderDetectorAdapter(Protocol):
    async def ainvoke(self, messages: Sequence[Any]) -> OrderDetection: ...


class _RiskClassifierAdapter(Protocol):
    async def ainvoke(self, messages: Sequence[Any]) -> SemanticRiskDetection: ...


class _ComplaintClassifierAdapter(Protocol):
    async def ainvoke(self, messages: Sequence[Any]) -> FormalComplaintDetection: ...


class _OperationExtractorAdapter(Protocol):
    async def ainvoke(self, messages: Sequence[Any]) -> OperationRequestExtraction: ...


@dataclass(frozen=True)
class EvaluationAdapters:
    """Keep the evaluator replaceable without coupling it to one model vendor."""

    router: _RouterAdapter
    order_detector: _OrderDetectorAdapter
    risk_classifier: _RiskClassifierAdapter
    complaint_classifier: _ComplaintClassifierAdapter
    operation_extractor: _OperationExtractorAdapter
    risk_matcher: RiskRuleMatcher

    @classmethod
    def showcase(cls) -> EvaluationAdapters:
        """Build the no-network adapters used by CI and the local portfolio demo."""
        return cls(
            router=ShowcaseRouter(),
            order_detector=ShowcaseOrderDetector(),
            risk_classifier=ShowcaseRiskClassifier(),
            complaint_classifier=ShowcaseComplaintClassifier(),
            operation_extractor=ShowcaseOperationExtractor(),
            risk_matcher=RiskRuleMatcher.from_json(),
        )


async def run_evaluation(
    manifest: EvaluationManifest,
    adapters: EvaluationAdapters | None = None,
) -> EvaluationReport:
    """Run every scenario and enforce configured quality and safety gates."""
    selected = adapters or EvaluationAdapters.showcase()
    results = tuple(
        [await _evaluate_scenario(scenario, selected) for scenario in manifest.scenarios]
    )
    total = len(results)
    passed = sum(result.passed for result in results)
    pass_rate = passed / total

    grouped: dict[SuiteName, list[EvaluationCaseResult]] = defaultdict(list)
    for result in results:
        grouped[result.suite].append(result)
    suites = tuple(
        SuiteSummary(
            suite=suite,
            total=len(items),
            passed=sum(item.passed for item in items),
            pass_rate=sum(item.passed for item in items) / len(items),
        )
        for suite, items in sorted(grouped.items())
    )

    safety = [result for result in results if result.suite in _SAFETY_SUITES]
    safety_pass_rate = (
        sum(result.passed for result in safety) / len(safety) if safety else 0.0
    )
    observed_languages = tuple(
        sorted({scenario.language for scenario in manifest.scenarios})
    )
    required_languages_present = set(manifest.gates.required_languages).issubset(
        observed_languages
    )
    gates = (
        EvaluationGateResult(
            name="overall_pass_rate",
            threshold=manifest.gates.overall_min_pass_rate,
            observed=pass_rate,
            passed=pass_rate >= manifest.gates.overall_min_pass_rate,
        ),
        EvaluationGateResult(
            name="safety_pass_rate",
            threshold=manifest.gates.safety_min_pass_rate,
            observed=safety_pass_rate,
            passed=safety_pass_rate >= manifest.gates.safety_min_pass_rate,
        ),
        EvaluationGateResult(
            name="required_language_coverage",
            threshold=1.0,
            observed=1.0 if required_languages_present else 0.0,
            passed=required_languages_present,
        ),
    )
    safety_categories = tuple(
        sorted(
            {
                category
                for scenario in manifest.scenarios
                if isinstance(scenario, (RiskRulesScenario, SemanticRiskScenario))
                for category in scenario.expected.categories
            }
        )
    )
    return EvaluationReport(
        schema_version=1,
        runner_version="1.0.0",
        dataset_version=manifest.dataset_version,
        dataset_digest=evaluation_manifest_digest(manifest),
        total=total,
        passed=passed,
        pass_rate=pass_rate,
        suites=suites,
        coverage=EvaluationCoverage(
            languages=observed_languages,
            safety_categories=safety_categories,
            scenario_tags=tuple(
                sorted({tag for scenario in manifest.scenarios for tag in scenario.tags})
            ),
        ),
        gates=gates,
        results=results,
    )


async def _evaluate_scenario(
    scenario: EvaluationScenario,
    adapters: EvaluationAdapters,
) -> EvaluationCaseResult:
    messages = [HumanMessage(content=scenario.input)]
    expected = scenario.expected.model_dump(mode="json")
    actual: dict[str, object]

    if isinstance(scenario, RoutingScenario):
        route_result = await adapters.router.ainvoke(messages)
        actual = {
            "intent": route_result.step,
            "human_handoff_requested": route_result.human_handoff_requested,
        }
    elif isinstance(scenario, OrderDetectionScenario):
        order_result = await adapters.order_detector.ainvoke(messages)
        actual = {
            "order_id": order_result.order_id if order_result.has_order_id else None
        }
    elif isinstance(scenario, RiskRulesScenario):
        rules_result = adapters.risk_matcher.match(scenario.input)
        actual = {
            "hard_critical": rules_result.hard_critical,
            "has_risk_signals": rules_result.has_risk_signals,
            "categories": sorted({match.category for match in rules_result.matches}),
        }
    elif isinstance(scenario, SemanticRiskScenario):
        semantic_result = await adapters.risk_classifier.ainvoke(messages)
        actual = {
            "risk_level": semantic_result.risk_level,
            "categories": sorted(semantic_result.categories),
        }
    elif isinstance(scenario, FormalComplaintScenario):
        complaint_result = await adapters.complaint_classifier.ainvoke(messages)
        actual = {
            "complaint_kind": complaint_result.complaint_kind,
            "staff_complaint_severity": complaint_result.staff_complaint_severity,
        }
    elif isinstance(scenario, OperationExtractionScenario):
        operation_result = await adapters.operation_extractor.ainvoke(messages)
        actual = {
            "operation_type": operation_result.operation_type,
            "reason": operation_result.reason,
            "delivery_issue_type": operation_result.delivery_issue_type,
            "replacement_variant_id": operation_result.replacement_variant_id,
            "ambiguous": operation_result.ambiguous,
        }
    elif isinstance(scenario, WorkflowSafetyScenario):
        actual = {"route": await _evaluate_workflow_route(scenario.input, adapters)}
    else:  # pragma: no cover - the discriminated manifest makes this unreachable.
        raise TypeError(f"Unsupported evaluation scenario: {type(scenario)!r}")

    normalized_expected = _normalize(expected)
    normalized_actual = _normalize(actual)
    return EvaluationCaseResult(
        id=scenario.id,
        suite=scenario.suite,
        language=scenario.language,
        tags=scenario.tags,
        passed=normalized_expected == normalized_actual,
        expected=normalized_expected,
        actual=normalized_actual,
    )


async def _evaluate_workflow_route(
    text: str,
    adapters: EvaluationAdapters,
) -> str:
    """Compose the same deterministic edge functions used by the real Graph."""
    rule_result = adapters.risk_matcher.match(text)
    state: dict[str, Any] = {
        "risk_hard_critical": rule_result.hard_critical,
        "risk_has_signals": rule_result.has_risk_signals,
    }
    typed_state = cast(RefundState, state)
    if route_after_risk_rules(typed_state) == "critical_risk":
        return "critical_risk"

    messages = [HumanMessage(content=text)]
    semantic = await adapters.risk_classifier.ainvoke(messages)
    state.update(
        semantic_risk_level=semantic.risk_level,
        semantic_risk_categories=semantic.categories,
    )
    if route_after_semantic_risk(typed_state) == "critical_risk":
        return "critical_risk"

    route = await adapters.router.ainvoke(messages)
    state.update(
        decision=route.step,
        human_handoff_requested=route.human_handoff_requested,
    )
    return route_by_intent_and_risk(typed_state)


def _normalize(value: dict[str, object]) -> dict[str, object]:
    """Normalize only order-insensitive lists before exact comparison."""
    normalized: dict[str, object] = {}
    for key, item in value.items():
        normalized[key] = sorted(item) if isinstance(item, list) else item
    return normalized
