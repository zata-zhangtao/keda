"""Tests for the independent verifier verdict protocol (PR#2 T3)."""

from __future__ import annotations

import pytest

from backend.core.use_cases.run_verifier_agent import (
    format_verifier_verdict_marker,
    parse_verifier_verdict,
)


def test_verdict_marker_roundtrip() -> None:
    """Each risk level round-trips through format → parse."""
    for risk in ("green", "yellow", "red"):
        marker = format_verifier_verdict_marker(risk)
        verdict = parse_verifier_verdict(f"some verifier report...\n{marker}\n")
        assert verdict.risk == risk


def test_green_and_yellow_pass_red_blocks() -> None:
    """green/yellow pass (yellow warns, does not block); red blocks."""
    green = parse_verifier_verdict(format_verifier_verdict_marker("green"))
    yellow = parse_verifier_verdict(format_verifier_verdict_marker("yellow"))
    red = parse_verifier_verdict(format_verifier_verdict_marker("red"))
    assert green.passed and not green.blocks
    assert yellow.passed and not yellow.blocks
    assert red.blocks and not red.passed


def test_missing_or_malformed_marker_fails_safe_to_red() -> None:
    """No verdict / malformed marker must NOT silently pass — fail safe to red."""
    for text in (
        "",
        "no marker here at all",
        "<!-- iar:verifier-verdict risk=bogus -->",
        "<!-- iar:verifier-verdict -->",
    ):
        verdict = parse_verifier_verdict(text)
        assert verdict.risk == "red"
        assert verdict.blocks


def test_latest_marker_wins() -> None:
    """When a repair re-runs the verifier, the latest verdict is authoritative."""
    text = (
        f"{format_verifier_verdict_marker('red')}\n"
        f"...repaired...\n{format_verifier_verdict_marker('green')}\n"
    )
    assert parse_verifier_verdict(text).risk == "green"


def test_findings_preserved() -> None:
    """Caller-supplied findings are carried into the verdict."""
    verdict = parse_verifier_verdict(
        format_verifier_verdict_marker("yellow"), findings="edge case X untested"
    )
    assert verdict.findings == "edge case X untested"


def test_format_rejects_invalid_risk() -> None:
    """Only the three known risk levels can be formatted."""
    with pytest.raises(ValueError):
        format_verifier_verdict_marker("orange")
