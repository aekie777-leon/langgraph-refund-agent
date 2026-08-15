"""Load and match deterministic customer-service risk rules."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal, cast

RiskCategory = Literal[
    "self_harm",
    "violence",
    "legal",
    "regulatory",
    "reputation",
]
RiskSeverity = Literal["critical", "high", "medium", "low"]
RiskRuleType = Literal["hard_critical", "risk_signal"]
RiskLanguage = Literal["en", "zh"]
MatchType = Literal["phrase", "word"]
UnicodeForm = Literal["NFC", "NFKC", "NFD", "NFKD"]

_RISK_CATEGORIES = frozenset(
    {"self_harm", "violence", "legal", "regulatory", "reputation"}
)
_RISK_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
_RISK_LANGUAGES = frozenset({"en", "zh"})
_MATCH_TYPES = frozenset({"phrase", "word"})
_RULE_FIELDS = frozenset(
    {
        "id",
        "pattern",
        "match_type",
        "language",
        "category",
        "severity",
        "rule_type",
    }
)
_NORMALIZATION_FIELDS = frozenset(
    {
        "unicode_form",
        "casefold",
        "collapse_whitespace",
        "normalize_apostrophes",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "normalization", "hard_critical", "risk_signals"}
)
_APOSTROPHE_TRANSLATION = str.maketrans({"’": "'", "‘": "'", "＇": "'"})


class RiskRuleConfigError(ValueError):
    """Report an invalid or unsupported risk-rule configuration."""


@dataclass(frozen=True)
class NormalizationConfig:
    """Describe deterministic text normalization used before matching."""

    unicode_form: UnicodeForm = "NFKC"
    casefold: bool = True
    collapse_whitespace: bool = True
    normalize_apostrophes: bool = True


@dataclass(frozen=True)
class RiskRule:
    """Represent one validated risk-matching rule."""

    id: str
    pattern: str
    match_type: MatchType
    language: RiskLanguage
    category: RiskCategory
    severity: RiskSeverity
    rule_type: RiskRuleType


@dataclass(frozen=True)
class RiskRuleSet:
    """Contain a complete, validated version of the risk-rule configuration."""

    schema_version: int
    normalization: NormalizationConfig
    hard_critical: tuple[RiskRule, ...]
    risk_signals: tuple[RiskRule, ...]


@dataclass(frozen=True)
class RiskMatch:
    """Describe one rule occurrence in normalized input text."""

    rule_id: str
    pattern: str
    matched_text: str
    start: int
    end: int
    language: RiskLanguage
    category: RiskCategory
    severity: RiskSeverity
    rule_type: RiskRuleType


@dataclass(frozen=True)
class RiskMatchResult:
    """Return hard-critical and contextual-signal matches separately."""

    normalized_text: str
    hard_critical: bool
    has_risk_signals: bool
    hard_critical_matches: tuple[RiskMatch, ...]
    risk_signal_matches: tuple[RiskMatch, ...]

    @property
    def matches(self) -> tuple[RiskMatch, ...]:
        """Return every match while preserving rule priority and file order."""
        return self.hard_critical_matches + self.risk_signal_matches


def normalize_text(
    text: str,
    config: NormalizationConfig | None = None,
) -> str:
    """Normalize text deterministically without language-model processing."""
    if not isinstance(text, str):
        raise TypeError("Risk input must be a string")

    settings = config or NormalizationConfig()
    normalized = unicodedata.normalize(settings.unicode_form, text)
    if settings.normalize_apostrophes:
        normalized = normalized.translate(_APOSTROPHE_TRANSLATION)
    if settings.casefold:
        normalized = normalized.casefold()
    if settings.collapse_whitespace:
        normalized = " ".join(normalized.split())
    return normalized


def load_risk_rules(path: str | Path | None = None) -> RiskRuleSet:
    """Load and validate risk rules from JSON or the packaged default file."""
    try:
        if path is None:
            raw_json = (
                resources.files("agent")
                .joinpath("data", "risk_rules.json")
                .read_text(encoding="utf-8")
            )
        else:
            raw_json = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw_json)
    except (OSError, json.JSONDecodeError) as exc:
        raise RiskRuleConfigError(
            f"Unable to load risk-rule configuration: {exc}"
        ) from exc

    root = _require_mapping(payload, "root")
    _reject_unknown_fields(root, _TOP_LEVEL_FIELDS, "root")

    schema_version = root.get("schema_version")
    if schema_version != 1:
        raise RiskRuleConfigError(
            f"Unsupported risk-rule schema_version: {schema_version!r}"
        )

    normalization = _parse_normalization(root.get("normalization"))
    hard_critical = _parse_rule_section(
        root.get("hard_critical"),
        section="hard_critical",
        expected_rule_type="hard_critical",
    )
    risk_signals = _parse_rule_section(
        root.get("risk_signals"),
        section="risk_signals",
        expected_rule_type="risk_signal",
    )

    rule_ids = [rule.id for rule in hard_critical + risk_signals]
    duplicate_ids = sorted(
        rule_id for rule_id in set(rule_ids) if rule_ids.count(rule_id) > 1
    )
    if duplicate_ids:
        raise RiskRuleConfigError(
            f"Duplicate risk rule IDs: {', '.join(duplicate_ids)}"
        )

    return RiskRuleSet(
        schema_version=1,
        normalization=normalization,
        hard_critical=hard_critical,
        risk_signals=risk_signals,
    )


class RiskRuleMatcher:
    """Match risk rules without making workflow or escalation decisions."""

    def __init__(self, rules: RiskRuleSet) -> None:
        """Compile a validated rule set for repeated deterministic matching."""
        self.rules = rules
        self._hard_patterns = self._compile_rules(rules.hard_critical)
        self._signal_patterns = self._compile_rules(rules.risk_signals)

    @classmethod
    def from_json(cls, path: str | Path | None = None) -> RiskRuleMatcher:
        """Build a matcher from a JSON file or the packaged default rules."""
        return cls(load_risk_rules(path))

    def match(self, text: str) -> RiskMatchResult:
        """Return all deterministic hard-critical and signal matches."""
        normalized_text = normalize_text(text, self.rules.normalization)
        hard_matches = self._find_matches(normalized_text, self._hard_patterns)
        signal_matches = self._find_matches(normalized_text, self._signal_patterns)
        return RiskMatchResult(
            normalized_text=normalized_text,
            hard_critical=bool(hard_matches),
            has_risk_signals=bool(signal_matches),
            hard_critical_matches=hard_matches,
            risk_signal_matches=signal_matches,
        )

    def _compile_rules(
        self,
        rules: tuple[RiskRule, ...],
    ) -> tuple[tuple[RiskRule, re.Pattern[str]], ...]:
        compiled: list[tuple[RiskRule, re.Pattern[str]]] = []
        for rule in rules:
            normalized_pattern = normalize_text(
                rule.pattern,
                self.rules.normalization,
            )
            escaped_pattern = re.escape(normalized_pattern)
            if rule.language == "en" or rule.match_type == "word":
                escaped_pattern = rf"(?<!\w){escaped_pattern}(?!\w)"
            compiled.append((rule, re.compile(escaped_pattern)))
        return tuple(compiled)

    @staticmethod
    def _find_matches(
        normalized_text: str,
        compiled_rules: tuple[tuple[RiskRule, re.Pattern[str]], ...],
    ) -> tuple[RiskMatch, ...]:
        matches: list[RiskMatch] = []
        for rule, pattern in compiled_rules:
            for occurrence in pattern.finditer(normalized_text):
                matches.append(
                    RiskMatch(
                        rule_id=rule.id,
                        pattern=rule.pattern,
                        matched_text=occurrence.group(0),
                        start=occurrence.start(),
                        end=occurrence.end(),
                        language=rule.language,
                        category=rule.category,
                        severity=rule.severity,
                        rule_type=rule.rule_type,
                    )
                )
        return tuple(matches)


def _parse_normalization(value: object) -> NormalizationConfig:
    config = _require_mapping(value, "normalization")
    _reject_unknown_fields(config, _NORMALIZATION_FIELDS, "normalization")

    unicode_form = _require_string(config.get("unicode_form"), "unicode_form")
    if unicode_form not in {"NFC", "NFKC", "NFD", "NFKD"}:
        raise RiskRuleConfigError(f"Unsupported Unicode form: {unicode_form!r}")

    return NormalizationConfig(
        unicode_form=cast(UnicodeForm, unicode_form),
        casefold=_require_bool(config.get("casefold"), "casefold"),
        collapse_whitespace=_require_bool(
            config.get("collapse_whitespace"),
            "collapse_whitespace",
        ),
        normalize_apostrophes=_require_bool(
            config.get("normalize_apostrophes"),
            "normalize_apostrophes",
        ),
    )


def _parse_rule_section(
    value: object,
    *,
    section: str,
    expected_rule_type: RiskRuleType,
) -> tuple[RiskRule, ...]:
    if not isinstance(value, list):
        raise RiskRuleConfigError(f"{section} must be a JSON array")

    rules: list[RiskRule] = []
    for index, item in enumerate(value):
        location = f"{section}[{index}]"
        entry = _require_mapping(item, location)
        _reject_unknown_fields(entry, _RULE_FIELDS, location)

        rule_id = _require_string(entry.get("id"), f"{location}.id")
        pattern = _require_string(entry.get("pattern"), f"{location}.pattern")
        if not pattern.strip():
            raise RiskRuleConfigError(f"{location}.pattern must not be empty")

        match_type = _require_choice(
            entry.get("match_type"),
            _MATCH_TYPES,
            f"{location}.match_type",
        )
        language = _require_choice(
            entry.get("language"),
            _RISK_LANGUAGES,
            f"{location}.language",
        )
        category = _require_choice(
            entry.get("category"),
            _RISK_CATEGORIES,
            f"{location}.category",
        )
        severity = _require_choice(
            entry.get("severity"),
            _RISK_SEVERITIES,
            f"{location}.severity",
        )
        rule_type = _require_string(entry.get("rule_type"), f"{location}.rule_type")

        if rule_type != expected_rule_type:
            raise RiskRuleConfigError(
                f"{location}.rule_type must be {expected_rule_type!r}"
            )
        if expected_rule_type == "hard_critical" and severity != "critical":
            raise RiskRuleConfigError(
                f"{location}.severity must be 'critical' for a hard rule"
            )
        if expected_rule_type == "risk_signal" and severity == "critical":
            raise RiskRuleConfigError(
                f"{location}.severity must not be 'critical' for a signal rule"
            )

        rules.append(
            RiskRule(
                id=rule_id,
                pattern=pattern,
                match_type=cast(MatchType, match_type),
                language=cast(RiskLanguage, language),
                category=cast(RiskCategory, category),
                severity=cast(RiskSeverity, severity),
                rule_type=expected_rule_type,
            )
        )
    return tuple(rules)


def _require_mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RiskRuleConfigError(f"{location} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise RiskRuleConfigError(f"{location} keys must be strings")
    return cast(Mapping[str, object], value)


def _require_string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise RiskRuleConfigError(f"{location} must be a string")
    return value


def _require_bool(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise RiskRuleConfigError(f"normalization.{location} must be a boolean")
    return value


def _require_choice(
    value: object,
    choices: Sequence[str] | frozenset[str],
    location: str,
) -> str:
    text = _require_string(value, location)
    if text not in choices:
        allowed = ", ".join(sorted(choices))
        raise RiskRuleConfigError(f"{location} must be one of: {allowed}")
    return text


def _reject_unknown_fields(
    value: Mapping[str, object],
    allowed: frozenset[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RiskRuleConfigError(f"Unknown fields in {location}: {', '.join(unknown)}")
