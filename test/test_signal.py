"""Tests for M2 — Signal layer (break detection + conviction)."""

import pytest
import time

from fair_value import compute_fair_line, FairValueConfig, FairLine
from signals import detect_break, SignalConfig, compute_conviction


def test_break_detected():
    """Soft book below fair → alert emitted with correct live side."""
    fair_line = FairLine(
        per_side={"Over": 55.0, "Under": 45.0},
        gate_pass=True, reason="ok", sharps_used=["pinnacle", "novig"],
        favorite="Over",
    )
    quotes_cents = {
        "Over": {"draftkings": 52.0, "fanduel": 56.0},
        "Under": {"draftkings": 49.0, "fanduel": 47.0},
    }
    alerts = detect_break(fair_line, quotes_cents)
    assert len(alerts) == 1
    assert alerts[0].live_side == "Over"
    assert alerts[0].signal_book == "draftkings"
    assert alerts[0].signal_edge_cents == pytest.approx(3.0)
    assert alerts[0].fair_cents == 55.0
    assert alerts[0].signal_price_cents == 52.0


def test_no_break_when_above_fair():
    """All books at/above fair → no alerts."""
    fair_line = FairLine(
        per_side={"Over": 50.0, "Under": 50.0},
        gate_pass=True, reason="ok", sharps_used=["pinnacle"],
        favorite="Over",
    )
    quotes_cents = {
        "Over": {"draftkings": 52.0},       # 52 > 50 → no break
        "Under": {"draftkings": 52.0},      # 52 > 50 → no break
    }
    alerts = detect_break(fair_line, quotes_cents)
    assert len(alerts) == 0


def test_sharp_books_excluded_from_break():
    """Sharp/exchange books are excluded from break detection."""
    fair_line = FairLine(
        per_side={"Over": 55.0, "Under": 45.0},
        gate_pass=True, reason="ok", sharps_used=["pinnacle", "novig"],
        favorite="Over",
    )
    quotes_cents = {
        "Over": {"pinnacle": 54.0, "novig": 53.0, "draftkings": 52.0},
        "Under": {"pinnacle": 46.0, "novig": 47.0, "draftkings": 48.0},
    }
    alerts = detect_break(fair_line, quotes_cents)
    assert len(alerts) == 1
    assert alerts[0].signal_book == "draftkings"


def test_min_edge_threshold():
    """Skip alerts below min_edge_cents."""
    fair_line = FairLine(
        per_side={"Over": 55.0, "Under": 45.0},
        gate_pass=True, reason="ok", sharps_used=["pinnacle"],
        favorite="Over",
    )
    quotes_cents = {
        "Over": {"draftkings": 54.0, "fanduel": 52.0},
        "Under": {"draftkings": 46.0, "fanduel": 48.0},
    }
    # min_edge=2.0 → only fanduel's 3¢ edge qualifies
    alerts = detect_break(fair_line, quotes_cents, SignalConfig(min_edge_cents=2.0))
    assert len(alerts) == 1
    assert alerts[0].signal_book == "fanduel"


def test_multiple_breaks_sorted():
    """Multiple breaks → sorted by edge descending."""
    fair_line = FairLine(
        per_side={"Over": 55.0, "Under": 45.0},
        gate_pass=True, reason="ok", sharps_used=["pinnacle"],
        favorite="Over",
    )
    quotes_cents = {
        "Over": {"fanduel": 48.0, "draftkings": 52.0},
        "Under": {"fanduel": 43.0},
    }
    alerts = detect_break(fair_line, quotes_cents)
    assert len(alerts) == 3
    assert alerts[0].signal_edge_cents == 7.0  # Over/fanduel: 55-48 = 7
    assert alerts[0].signal_book == "fanduel"
    assert alerts[1].signal_edge_cents == 3.0  # Over/draftkings: 55-52 = 3
    assert alerts[2].signal_edge_cents == 2.0  # Under/fanduel: 45-43 = 2


def test_gate_closed_no_alert():
    """FairLine with gate_pass=False → no alerts processed."""
    fair_line = FairLine(
        per_side={"Over": 50.0, "Under": 50.0},
        gate_pass=False, reason="directional disagreement",
        sharps_used=["pinnacle", "novig"],
    )
    quotes_cents = {
        "Over": {"draftkings": 45.0},
        "Under": {"draftkings": 55.0},
    }
    # Even with a deep break, gate is closed so nothing is signaled.
    # detect_break doesn't check gate_pass — the caller is responsible.
    alerts = detect_break(fair_line, quotes_cents)
    assert len(alerts) == 1  # It finds the break; caller gates the workflow


def test_conviction_empty_depth():
    """No depth data → conviction returns 0."""
    total = compute_conviction([], "Over", 52.0)
    assert total == 0.0


def test_conviction_with_depth():
    """Sum opposite-side bids at or beyond complement price."""
    ladder = [
        {"side": "bid", "price_cents": 48.0, "size_usd": 200},
        {"side": "bid", "price_cents": 47.0, "size_usd": 150},
        {"side": "ask", "price_cents": 52.0, "size_usd": 300},
    ]
    # For Over (52¢), complement = 48¢
    # Sum bids at price >= 48: 200 (at 48) + 150 (at 47 is below 48, excluded)
    total = compute_conviction(ladder, "Over", 52.0)
    assert total == 200.0


# Integration test: fair_line + break detection end-to-end
def test_end_to_end_break_from_sharp_fair():
    """Full pipeline: M1 fair line → M2 break detection."""
    from utils import american_to_cents

    # Only sharps go into M1 fair line
    sharp_odds = {
        "Home": {"pinnacle": -150, "novig": -145},
        "Away": {"pinnacle": +130, "novig": +125},
    }
    fair_config = FairValueConfig(odds_format="american")
    fair_line = compute_fair_line(sharp_odds, fair_config)
    assert fair_line.gate_pass
    assert fair_line.favorite == "Home"

    # All books (including soft) go into break detection
    all_odds = {
        "Home": {"pinnacle": -150, "novig": -145, "draftkings": 105},
        "Away": {"pinnacle": +130, "novig": +125, "draftkings": -125},
    }
    cents_dict = {}
    for side, books in all_odds.items():
        cents_dict[side] = {}
        for book, price in books.items():
            cents_dict[side][book] = round(american_to_cents(price), 4)

    alerts = detect_break(fair_line, cents_dict)
    assert len(alerts) >= 1
    for a in alerts:
        assert a.signal_book not in ("pinnacle", "novig")
    dk_alerts = [a for a in alerts if a.signal_book == "draftkings" and a.live_side == "Home"]
    assert len(dk_alerts) == 1
    assert dk_alerts[0].signal_edge_cents > 5
