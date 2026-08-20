"""Typed contracts for the versioned offline evaluation dataset and report."""

from __future__ import annotations

from typing import Annotated, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.operations.models import DeliveryIssueType, OperationReason, OperationType
from agent.schemas import (
    FormalComplaintKind,
    Intent,
    SemanticRiskCategory,
    SemanticRiskLevel,
    StaffComplaintSeverity,
)

Language = Literal["en", "zh"]
SuiteName = Literal[
    "routing",
    "order_detection",
    "risk_rules",
    "semantic_risk",
    "formal_complaint",
    "operation_extraction",
    "workflow_safety",
]


class StrictModel(BaseModel):
    """Reject silent dataset drift and keep evaluation inputs immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioBase(StrictModel):
    """Describe fields shared by every synthetic evaluation scenario."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    language: Language
    input: str = Field(min_length=1, max_length=1000)
    tags: tuple[str, ...] = ()


class RoutingExpected(StrictModel):
    """Define the expected structured intent decision."""

    intent: Intent
    human_handoff_requested: bool


class RoutingScenario(ScenarioBase):
    """Evaluate one intent-routing input."""

    suite: Literal["routing"]
    expected: RoutingExpected


class OrderDetectionExpected(StrictModel):
    """Define the normalized order ID or its intentional absence."""

    order_id: str | None = Field(default=None, pattern=r"^ORD-\d{5}$")


class OrderDetectionScenario(ScenarioBase):
    """Evaluate one order-identifier extraction input."""

    suite: Literal["order_detection"]
    expected: OrderDetectionExpected


class RiskRulesExpected(StrictModel):
    """Define exact deterministic rule-matcher evidence."""

    hard_critical: bool
    has_risk_signals: bool
    categories: tuple[SemanticRiskCategory, ...] = ()


class RiskRulesScenario(ScenarioBase):
    """Evaluate one hard-critical or signal-rule boundary."""

    suite: Literal["risk_rules"]
    expected: RiskRulesExpected


class SemanticRiskExpected(StrictModel):
    """Define a semantic risk level and normalized categories."""

    risk_level: SemanticRiskLevel
    categories: tuple[SemanticRiskCategory, ...]

    @model_validator(mode="after")
    def validate_categories(self) -> Self:
        """Keep none/empty semantics aligned with the production contract."""
        if (self.risk_level == "none") != (not self.categories):
            raise ValueError("risk_level none requires an empty category list")
        return self


class SemanticRiskScenario(ScenarioBase):
    """Evaluate one replaceable semantic-risk adapter input."""

    suite: Literal["semantic_risk"]
    expected: SemanticRiskExpected


class FormalComplaintExpected(StrictModel):
    """Define complaint kind without retaining free-form rationale."""

    complaint_kind: FormalComplaintKind
    staff_complaint_severity: StaffComplaintSeverity | None = None

    @model_validator(mode="after")
    def validate_staff_severity(self) -> Self:
        """Mirror the production complaint shape without storing model rationale."""
        if (
            self.complaint_kind == "staff_conduct"
            and self.staff_complaint_severity is None
        ):
            raise ValueError("staff_conduct requires staff_complaint_severity")
        if (
            self.complaint_kind != "staff_conduct"
            and self.staff_complaint_severity is not None
        ):
            raise ValueError("only staff_conduct may include staff_complaint_severity")
        return self


class FormalComplaintScenario(ScenarioBase):
    """Evaluate one formal-complaint classification input."""

    suite: Literal["formal_complaint"]
    expected: FormalComplaintExpected


class OperationExtractionExpected(StrictModel):
    """Define policy-safe fields extracted from an operation request."""

    operation_type: OperationType | None = None
    reason: OperationReason | None = None
    delivery_issue_type: DeliveryIssueType | None = None
    replacement_variant_id: str | None = None
    ambiguous: bool


class OperationExtractionScenario(ScenarioBase):
    """Evaluate one operation or delivery extraction input."""

    suite: Literal["operation_extraction"]
    expected: OperationExtractionExpected


WorkflowRoute = Literal[
    "critical_risk",
    "confirm_order_priority",
    "order_query",
    "operation_flow",
    "formal_complaint",
    "confirm_human_handoff",
    "support_case_status",
]


class WorkflowSafetyExpected(StrictModel):
    """Define the expected production conditional-edge destination."""

    route: WorkflowRoute


class WorkflowSafetyScenario(ScenarioBase):
    """Evaluate a safety decision chain built from real Graph routes."""

    suite: Literal["workflow_safety"]
    expected: WorkflowSafetyExpected


EvaluationScenario = Annotated[
    RoutingScenario
    | OrderDetectionScenario
    | RiskRulesScenario
    | SemanticRiskScenario
    | FormalComplaintScenario
    | OperationExtractionScenario
    | WorkflowSafetyScenario,
    Field(discriminator="suite"),
]


class EvaluationGates(StrictModel):
    """Define fail-closed thresholds for the deterministic regression suite."""

    overall_min_pass_rate: float = Field(ge=0, le=1)
    safety_min_pass_rate: float = Field(ge=0, le=1)
    required_languages: tuple[Language, ...] = ("en", "zh")


class EvaluationManifest(StrictModel):
    """Version the synthetic dataset independently from implementation code."""

    schema_version: Literal[1]
    dataset_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    gates: EvaluationGates
    scenarios: tuple[EvaluationScenario, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario_ids(self) -> Self:
        """Reject ambiguous IDs or an incomplete suite masquerading as evidence."""
        ids = [scenario.id for scenario in self.scenarios]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate evaluation scenario IDs: {', '.join(duplicates)}")
        required_suites = set(get_args(SuiteName))
        observed_suites = {scenario.suite for scenario in self.scenarios}
        missing_suites = sorted(required_suites - observed_suites)
        if missing_suites:
            raise ValueError(
                f"evaluation manifest is missing suites: {', '.join(missing_suites)}"
            )
        return self


class EvaluationCaseResult(StrictModel):
    """Record redacted expected and actual evidence for one scenario."""

    id: str
    suite: SuiteName
    language: Language
    tags: tuple[str, ...]
    passed: bool
    expected: dict[str, object]
    actual: dict[str, object]


class SuiteSummary(StrictModel):
    """Aggregate one evaluation suite without exposing inputs."""

    suite: SuiteName
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)


class EvaluationCoverage(StrictModel):
    """Summarize language, safety-category, and scenario-tag coverage."""

    languages: tuple[Language, ...]
    safety_categories: tuple[SemanticRiskCategory, ...]
    scenario_tags: tuple[str, ...]


class EvaluationGateResult(StrictModel):
    """Record one configured release gate and its observation."""

    name: str
    threshold: float = Field(ge=0, le=1)
    observed: float = Field(ge=0, le=1)
    passed: bool


class EvaluationReport(StrictModel):
    """Contain deterministic, input-redacted evidence suitable for CI."""

    schema_version: Literal[1]
    runner_version: Literal["1.0.0"]
    dataset_version: str
    dataset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    suites: tuple[SuiteSummary, ...]
    coverage: EvaluationCoverage
    gates: tuple[EvaluationGateResult, ...]
    results: tuple[EvaluationCaseResult, ...]

    @property
    def ready(self) -> bool:
        """Require every configured gate to pass."""
        return all(gate.passed for gate in self.gates)
