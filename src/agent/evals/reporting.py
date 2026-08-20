"""Render stable, input-redacted evaluation evidence."""

import json

from agent.evals.models import EvaluationReport


def report_json(report: EvaluationReport) -> str:
    """Serialize canonical JSON for committed-report verification."""
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def report_markdown(report: EvaluationReport) -> str:
    """Render a concise GitHub-facing report without scenario input text."""
    status = "PASS" if report.ready else "FAIL"
    lines = [
        "# AI evaluation report",
        "",
        f"**Status:** {status}  ",
        f"**Dataset:** {report.dataset_version} (`{report.dataset_digest[:12]}…`)  ",
        f"**Result:** {report.passed}/{report.total} ({report.pass_rate:.1%})",
        "",
        "## Suite results",
        "",
        "| Suite | Passed | Total | Rate |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| `{suite.suite}` | {suite.passed} | {suite.total} | {suite.pass_rate:.1%} |"
        for suite in report.suites
    )
    lines.extend(
        [
            "",
            "## Quality gates",
            "",
            "| Gate | Observed | Threshold | Status |",
            "|---|---:|---:|---|",
        ]
    )
    lines.extend(
        f"| `{gate.name}` | {gate.observed:.1%} | {gate.threshold:.1%} | "
        f"{'PASS' if gate.passed else 'FAIL'} |"
        for gate in report.gates
    )
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            f"- Languages: {', '.join(report.coverage.languages)}",
            f"- Safety categories: {', '.join(report.coverage.safety_categories)}",
            f"- Scenario tags: {', '.join(report.coverage.scenario_tags)}",
            "",
            "## Interpretation",
            "",
            "This is a deterministic, synthetic regression evaluation. It proves that the "
            "documented no-network adapters and the real workflow routing functions satisfy "
            "the versioned golden contract. It does not claim production-model accuracy, "
            "replace human safety review, or use customer conversations.",
            "",
            "Scenario inputs are intentionally omitted from this report; only stable IDs and "
            "structured expected/actual values exist in the JSON artifact.",
            "",
        ]
    )
    return "\n".join(lines)
