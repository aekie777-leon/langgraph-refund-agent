# AI evaluation report

**Status:** PASS  
**Dataset:** 1.0.0 (`7f6a1e23fea9…`)  
**Result:** 57/57 (100.0%)

## Suite results

| Suite | Passed | Total | Rate |
|---|---:|---:|---:|
| `formal_complaint` | 5 | 5 | 100.0% |
| `operation_extraction` | 7 | 7 | 100.0% |
| `order_detection` | 4 | 4 | 100.0% |
| `risk_rules` | 18 | 18 | 100.0% |
| `routing` | 9 | 9 | 100.0% |
| `semantic_risk` | 6 | 6 | 100.0% |
| `workflow_safety` | 8 | 8 | 100.0% |

## Quality gates

| Gate | Observed | Threshold | Status |
|---|---:|---:|---|
| `overall_pass_rate` | 100.0% | 100.0% | PASS |
| `safety_pass_rate` | 100.0% | 100.0% | PASS |
| `required_language_coverage` | 100.0% | 100.0% | PASS |

## Coverage

- Languages: en, zh
- Safety categories: legal, regulatory, reputation, self_harm, violence
- Scenario tags: ambiguous, cancellation, case, complaint, delivery, entity, exchange, formal, handoff, hard-critical, intent, legal, manual-review, negative-boundary, noncritical-risk, normalization, operation, order, order-id, refund, regulatory, reputation, return, risk-signal, safety, self-harm, semantic, staff-conduct, violence, workflow

## Interpretation

This is a deterministic, synthetic regression evaluation. It proves that the documented no-network adapters and the real workflow routing functions satisfy the versioned golden contract. It does not claim production-model accuracy, replace human safety review, or use customer conversations.

Scenario inputs are intentionally omitted from this report; only stable IDs and structured expected/actual values exist in the JSON artifact.
