"""Tests for M5 — Market matching + settlement verification."""

import os
import tempfile
import pytest

from signals import Alert
from matching import (
    MarketKey,
    settlement_id_from_market_type,
    parse_market_id,
    MarketMatcher,
)


# ---- parse_market_id tests ----

def test_parse_game_h2h():
    event_id, mtype, player, line, raw = parse_market_id("evt_lal_bos|h2h")
    assert event_id == "evt_lal_bos"
    assert mtype == "h2h"
    assert player is None
    assert line is None


def test_parse_spread():
    event_id, mtype, player, line, raw = parse_market_id("evt_lal_bos|spreads")
    assert event_id == "evt_lal_bos"
    assert mtype == "spread"
    assert player is None
    assert line is None


def test_parse_total():
    event_id, mtype, player, line, raw = parse_market_id("evt_lal_bos|totals")
    assert event_id == "evt_lal_bos"
    assert mtype == "total"


def test_parse_prop():
    event_id, mtype, player, line, raw = parse_market_id(
        "ee78|Chase Burns|player_strikeouts|6.5")
    assert event_id == "ee78"
    assert mtype == "player_prop"
    assert player == "Chase Burns"
    assert line == 6.5
    assert raw == "player_strikeouts"


# ---- settlement_id tests ----

def test_settlement_id_deterministic():
    s1 = settlement_id_from_market_type("h2h")
    s2 = settlement_id_from_market_type("h2h")
    assert s1 == s2


def test_settlement_id_differs_by_type():
    assert settlement_id_from_market_type("h2h") != settlement_id_from_market_type("total")


def test_settlement_id_differs_by_line():
    s1 = settlement_id_from_market_type("player_prop", 6.5)
    s2 = settlement_id_from_market_type("player_prop", 7.5)
    assert s1 != s2


# ---- MarketMatcher tests ----

@pytest.fixture
def registry_path():
    """Create a temporary registry file with known mappings."""
    data = {
        "version": 1,
        "mappings": {
            "evt_lal_bos|h2h|Lakers": {
                "kalshi": {
                    "outcome_id": "KX_LAL_WIN",
                    "opposite_outcome_id": "KX_BOS_WIN",
                },
                "polymarket": {
                    "outcome_id": "0x1234",
                    "opposite_outcome_id": "0x5678",
                },
            },
            "evt_lal_bos|h2h|Celtics": {
                "default": {
                    "outcome_id": "celtic_win_pm",
                    "opposite_outcome_id": "laker_win_pm",
                },
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        import json
        json.dump(data, f)
        path = f.name
    yield path
    os.unlink(path)


def test_resolve_alert_known_h2h(registry_path):
    matcher = MarketMatcher(registry_path)
    alert = Alert(market="evt_lal_bos|h2h", live_side="Lakers")
    result, oid, opp_oid = matcher.resolve_alert(alert, venues=["kalshi", "polymarket"])
    assert result is not None
    assert result.market_key.event_id == "evt_lal_bos"
    assert result.market_key.market_type == "h2h"
    assert oid == "KX_LAL_WIN"
    assert opp_oid == "KX_BOS_WIN"


def test_resolve_alert_known_default_venue(registry_path):
    matcher = MarketMatcher(registry_path)
    alert = Alert(market="evt_lal_bos|h2h", live_side="Celtics")
    result, oid, opp_oid = matcher.resolve_alert(alert, venues=["kalshi"])
    assert oid == "celtic_win_pm"
    assert opp_oid == "laker_win_pm"


def test_resolve_alert_unknown(registry_path):
    matcher = MarketMatcher(registry_path)
    alert = Alert(market="evt_unknown|h2h", live_side="Home")
    result, oid, opp_oid = matcher.resolve_alert(alert, venues=["kalshi"])
    assert result is None
    assert oid == "evt_unknown|h2h:Home"  # placeholder
    assert opp_oid is None


def test_resolve_alert_prop(registry_path):
    matcher = MarketMatcher(registry_path)
    alert = Alert(market="ee78|Chase Burns|player_strikeouts|6.5", live_side="Over")
    result, oid, opp_oid = matcher.resolve_alert(alert, venues=["kalshi"])
    assert result is None  # no mapping for this in registry
    assert "placeholder" not in oid  # just a string ID


def test_resolve_alerts_batch(registry_path):
    matcher = MarketMatcher(registry_path)
    alerts = [
        Alert(market="evt_lal_bos|h2h", live_side="Lakers"),
        Alert(market="evt_lal_bos|h2h", live_side="Celtics"),
        Alert(market="evt_unknown|h2h", live_side="Home"),
    ]
    om, opp = matcher.resolve_alerts(alerts, venues=["kalshi"])

    # Lakers has a venue-specific mapping
    assert om["evt_lal_bos|h2h"]["Lakers"] == "KX_LAL_WIN"
    assert opp["evt_lal_bos|h2h"]["Lakers"] == "KX_BOS_WIN"

    # Celtics uses default
    assert om["evt_lal_bos|h2h"]["Celtics"] == "celtic_win_pm"
    assert opp["evt_lal_bos|h2h"]["Celtics"] == "laker_win_pm"

    # Unknown uses placeholder
    assert om["evt_unknown|h2h"]["Home"] == "evt_unknown|h2h:Home"
    assert "evt_unknown|h2h" not in opp or "Home" not in opp.get("evt_unknown|h2h", {})


def test_add_mapping(registry_path):
    matcher = MarketMatcher(registry_path)
    matcher.add_mapping(
        "evt_test|h2h", "Home", "kalshi",
        outcome_id="KX_HOME", opposite_outcome_id="KX_AWAY",
    )
    # Verify in-memory
    entry = matcher.get_mapping("evt_test|h2h", "Home")
    assert entry is not None
    assert entry["kalshi"]["outcome_id"] == "KX_HOME"


def test_remove_mapping(registry_path):
    matcher = MarketMatcher(registry_path)
    assert matcher.remove_mapping("evt_lal_bos|h2h", "Lakers", "kalshi")
    entry = matcher.get_mapping("evt_lal_bos|h2h", "Lakers")
    assert entry is not None
    assert "kalshi" not in entry  # polymarket entry remains


def test_reload(registry_path):
    matcher = MarketMatcher(registry_path)
    assert matcher.resolve_alert(
        Alert(market="evt_lal_bos|h2h", live_side="Lakers"),
        venues=["kalshi"],
    )[1] == "KX_LAL_WIN"
