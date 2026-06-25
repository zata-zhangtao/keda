"""Tests for the agent runner live terminal view."""

from __future__ import annotations

from backend.engines.agent_runner.live_panels import PanelState


def test_agent_panel_state_joins_fragmented_chunks_on_current_line() -> None:
    """Token-sized stream chunks should render as one readable line."""
    panel_state = PanelState("synthesizer", "claude")

    for streamed_text_chunk in (
        "(`",
        "play",
        "wright",
        "_b",
        "rowser.py",
        ":82",
        "-",
        "109",
        "`), ",
        "不是",
        "叠加",
    ):
        panel_state.append(streamed_text_chunk)

    assert panel_state.lines == ["(`playwright_browser.py:82-109`), 不是叠加"]


def test_agent_panel_state_splits_only_on_real_newlines() -> None:
    """Line cache should respect explicit newlines without splitting chunks."""
    panel_state = PanelState("skeptic", "kimi")

    panel_state.append("alpha\n")
    panel_state.append("beta")
    panel_state.append("\n")
    panel_state.append("gamma\n\ndelta")

    assert panel_state.lines == ["alpha", "beta", "gamma", "", "delta"]
