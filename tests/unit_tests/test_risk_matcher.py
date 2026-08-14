"""Unit tests for deterministic risk-rule loading and matching."""

import pytest

from agent.risk_matcher import RiskRuleMatcher, load_risk_rules


@pytest.fixture(scope="module")
def matcher() -> RiskRuleMatcher:
    return RiskRuleMatcher.from_json()


def test_default_configuration_has_bilingual_metadata() -> None:
    rules = load_risk_rules()
    expected_categories = {
        "self_harm",
        "violence",
        "legal",
        "regulatory",
        "reputation",
    }

    assert rules.schema_version == 1
    assert rules.hard_critical
    assert rules.risk_signals
    assert {rule.category for rule in rules.hard_critical} == expected_categories
    assert {rule.category for rule in rules.risk_signals} == expected_categories
    assert {rule.language for rule in rules.hard_critical} == {"en", "zh"}
    assert {rule.language for rule in rules.risk_signals} == {"en", "zh"}
    assert all(rule.severity == "critical" for rule in rules.hard_critical)
    assert all(rule.severity != "critical" for rule in rules.risk_signals)


def test_hard_critical_match_is_explicitly_preserved(
    matcher: RiskRuleMatcher,
) -> None:
    result = matcher.match(
        "You will regret this. I will kill you if this is not fixed."
    )

    assert result.hard_critical is True
    assert result.has_risk_signals is True
    assert result.hard_critical_matches[0].category == "violence"
    assert result.hard_critical_matches[0].severity == "critical"
    assert result.hard_critical_matches[0].rule_type == "hard_critical"


def test_signal_match_does_not_become_critical(
    matcher: RiskRuleMatcher,
) -> None:
    result = matcher.match("You will hear from my lawyer about this refund.")

    assert result.hard_critical is False
    assert result.has_risk_signals is True
    assert result.hard_critical_matches == ()
    assert result.risk_signal_matches[0].category == "legal"
    assert result.risk_signal_matches[0].rule_type == "risk_signal"
    assert result.risk_signal_matches[0].severity == "medium"


def test_no_match_returns_empty_structured_result(
    matcher: RiskRuleMatcher,
) -> None:
    result = matcher.match("Could you check the delivery status of ORD-10001?")

    assert result.hard_critical is False
    assert result.has_risk_signals is False
    assert result.matches == ()


def test_english_matching_normalizes_case_whitespace_and_apostrophes(
    matcher: RiskRuleMatcher,
) -> None:
    result = matcher.match("  I   CAN’T   GO ON LIKE THIS.  ")

    assert result.normalized_text == "i can't go on like this."
    assert result.hard_critical is False
    assert result.has_risk_signals is True
    assert result.risk_signal_matches[0].category == "self_harm"


@pytest.mark.parametrize(
    ("text", "expected_rule_type", "expected_category"),
    [
        ("如果还不给我处理，我要自杀。", "hard_critical", "self_harm"),
        ("你们等着瞧，这件事没完。", "risk_signal", "violence"),
        ("我要向消费者协会投诉。", "risk_signal", "regulatory"),
        ("我要把这件事曝光给媒体。", "hard_critical", "reputation"),
    ],
)
def test_chinese_text_matching(
    matcher: RiskRuleMatcher,
    text: str,
    expected_rule_type: str,
    expected_category: str,
) -> None:
    result = matcher.match(text)

    assert len(result.matches) == 1
    assert result.matches[0].language == "zh"
    assert result.matches[0].rule_type == expected_rule_type
    assert result.matches[0].category == expected_category
    assert result.hard_critical is (expected_rule_type == "hard_critical")


@pytest.mark.parametrize(
    "text",
    [
        "This delivery delay is killing me, but I only need a status update.",
        "Our new mouse has killer features.",
        "I will not kill you; I am quoting the policy example.",
        "My sister is a lawyer.",
        "The media file attached to my order is corrupted.",
        "I watched a documentary about suicide prevention.",
        "这本书讨论的是自杀预防教育。",
        "请告诉我正式投诉渠道在哪里。",
        "订单中附带的媒体文件损坏了。",
    ],
)
def test_obvious_false_positive_phrases_do_not_match(
    matcher: RiskRuleMatcher,
    text: str,
) -> None:
    assert matcher.match(text).matches == ()
