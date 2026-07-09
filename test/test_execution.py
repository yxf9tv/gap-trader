"""Tests for M3 — Execution layer (PMXT integration)."""

import pytest
from dataclasses import dataclass

from signals import Alert
from pmxt_execution import LadderLevel, ladder_from_order_book, \
    best_ask_cents, best_bid_cents, executable_depth, merged_asks, \
    conviction_liquidity
from execution import ExecutionEngine, ExecutionConfig, compute_kelly, take_or_post


# ---- Mock PMXT types ----

@dataclass
class MockLevel:
    price: float
    size: float


@dataclass
class MockOrderBook:
    asks: list
    bids: list


# ---- Test fixtures ----

@pytest.fixture
def kalshi_ladder():
    ob = MockOrderBook(
        asks=[MockLevel(0.55, 200), MockLevel(0.57, 150)],
        bids=[MockLevel(0.53, 300), MockLevel(0.51, 80)],
    )
    return ladder_from_order_book("kalshi", ob)


@pytest.fixture
def pm_ladder():
    ob = MockOrderBook(
        asks=[MockLevel(0.52, 400), MockLevel(0.54, 250)],
        bids=[MockLevel(0.50, 100), MockLevel(0.48, 60)],
    )
    return ladder_from_order_book("polymarket", ob)


# ---- pmxt_execution utility tests ----

def test_best_ask_cents(kalshi_ladder):
    assert best_ask_cents(kalshi_ladder) == 55.0


def test_best_ask_no_side(kalshi_ladder):
    """No ask levels exist for the query → None."""
    assert best_ask_cents([]) is None


def test_best_bid_cents(kalshi_ladder):
    assert best_bid_cents(kalshi_ladder) == 53.0


def test_executable_depth(kalshi_ladder):
    """Only asks strictly below fair_cents count."""
    assert executable_depth(kalshi_ladder, 56.0) == 200.0  # 55<56 → 200, 57>56 → excluded
    assert executable_depth(kalshi_ladder, 58.0) == 350.0  # both below
    assert executable_depth(kalshi_ladder, 54.0) == 0.0    # neither below


def test_merged_asks(kalshi_ladder, pm_ladder):
    merged = merged_asks([kalshi_ladder, pm_ladder])
    assert len(merged) == 4
    # Sorted by price ascending
    assert merged[0].price_cents == 52.0  # polymarket
    assert merged[1].price_cents == 54.0  # polymarket
    assert merged[2].price_cents == 55.0  # kalshi
    assert merged[3].price_cents == 57.0  # kalshi
    # Venue labels preserved
    assert merged[0].venue == "polymarket"
    assert merged[2].venue == "kalshi"


# ---- ExecutionEngine tests (mocked clients) ----

class _MockClient:
    """Simulates a PMXT venue client with a fixed order book."""

    def __init__(self, ob):
        self._ob = ob

    def fetch_order_book(self, outcome_id):
        return self._ob


def test_enrich_alert_real_trade():
    """PM has an ask below fair → traded='real', edge_cents set, M4 fields."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi", "polymarket"]))
    engine.clients = {
        "kalshi": _MockClient(MockOrderBook(
            asks=[MockLevel(0.58, 100)],
            bids=[MockLevel(0.55, 200)],
        )),
        "polymarket": _MockClient(MockOrderBook(
            asks=[MockLevel(0.53, 300)],  # below fair=55 — real edge
            bids=[MockLevel(0.50, 150)],
        )),
    }

    alert = Alert(
        market="evt_test",
        live_side="Over",
        fair_cents=55.0,
        signal_book="draftkings",
        signal_price_cents=52.0,
        signal_edge_cents=3.0,
    )

    engine.enrich_alert(alert, outcome_id="outcome_1")

    assert alert.traded == "real"
    # gross=2¢, slippage=53*0.0005=0.027¢, net=1.973¢
    assert alert.edge_cents == pytest.approx(1.973, abs=0.01)
    assert alert.gross_edge_cents == pytest.approx(2.0)
    assert alert.best_pm["polymarket"] == 53.0
    assert alert.executable_depth > 0
    # M4: shadow since no opposite_outcome_id
    assert alert.conviction_liquidity == 0.0
    # kelly/stake/mode populated since traded=real
    assert alert.kelly_pct is not None and alert.kelly_pct > 0
    assert alert.max_stake is not None and alert.max_stake > 0
    assert alert.execution_mode is not None
    # Kelly = net_edge(1.973) / payout(46.973) × frac(0.25) → ~1.05%
    assert alert.kelly_pct == pytest.approx(1.05, abs=0.1)


def test_enrich_alert_shadow():
    """No PM ask below fair → traded='shadow', edge_cents=None."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    engine.clients = {
        "kalshi": _MockClient(MockOrderBook(
            asks=[MockLevel(0.58, 100)],   # above fair=55
            bids=[MockLevel(0.55, 200)],
        )),
    }

    alert = Alert(
        market="evt_test",
        live_side="Over",
        fair_cents=55.0,
        signal_book="draftkings",
        signal_price_cents=52.0,
        signal_edge_cents=3.0,
    )

    engine.enrich_alert(alert, outcome_id="outcome_1")

    assert alert.traded == "shadow"
    assert alert.edge_cents is None
    assert alert.best_pm == {"kalshi": 58.0}


def test_enrich_alert_no_venue():
    """No venue clients → fields stay at defaults."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    # No clients at all
    alert = Alert(
        market="evt_test",
        live_side="Over",
        fair_cents=55.0,
        signal_book="draftkings",
        signal_price_cents=52.0,
        signal_edge_cents=3.0,
    )

    engine.enrich_alert(alert, outcome_id="outcome_1")

    assert alert.traded == "shadow"
    assert alert.edge_cents is None
    assert alert.best_pm == {}


def test_enrich_alerts_batch():
    """enrich_alerts processes multiple alerts with an outcome map."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    engine.clients = {
        "kalshi": _MockClient(MockOrderBook(
            asks=[MockLevel(0.52, 200)],
            bids=[MockLevel(0.48, 100)],
        )),
    }

    alerts = [
        Alert(market="m1", live_side="Over", fair_cents=55.0),
        Alert(market="m2", live_side="Under", fair_cents=50.0),
    ]
    outcome_map = {
        "m1": {"Over": "outcome_1"},
        "m2": {"Under": "outcome_2"},
    }

    engine.enrich_alerts(alerts, outcome_map)

    assert alerts[0].traded == "real"
    # gross=3¢, slippage=52*0.0005=0.026¢, net=2.974¢
    assert alerts[0].edge_cents == pytest.approx(2.974, abs=0.01)
    assert alerts[1].traded == "shadow"
    assert alerts[1].edge_cents is None


def test_enrich_alert_crash_safe():
    """Client fetch error → alert stays as shadow, no crash."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    class _BrokenClient:
        def fetch_order_book(self, outcome_id):
            raise RuntimeError("network error")
    engine.clients = {"kalshi": _BrokenClient()}

    alert = Alert(market="m1", live_side="Over", fair_cents=55.0)
    engine.enrich_alert(alert, outcome_id="outcome_1")
    assert alert.traded == "shadow"
    assert alert.edge_cents is None
    assert alert.best_pm == {}


# ---- M4: conviction liquidity tests ----

@dataclass
class _OrderBookMap:
    """MockClient that returns a different book per outcome_id."""
    _books: dict

    def fetch_order_book(self, outcome_id):
        return self._books[outcome_id]


def test_conviction_liquidity():
    """Sum opposite-side bids at complementary price."""
    # Simulate two venue ladders for the opposite outcome
    ladder_a = [
        LadderLevel("kalshi", "bid", 48.0, 200),
        LadderLevel("kalshi", "bid", 47.0, 150),
        LadderLevel("kalshi", "ask", 52.0, 300),
    ]
    ladder_b = [
        LadderLevel("polymarket", "bid", 49.0, 400),
        LadderLevel("polymarket", "ask", 52.0, 100),
    ]
    # For live side "Over" at 52¢ → complement = 48¢
    # Sum bids >= 48: 200 + 400 = 600
    total = conviction_liquidity([ladder_a, ladder_b], 52.0)
    assert total == 600.0


def test_conviction_liquidity_empty():
    assert conviction_liquidity([], 52.0) == 0.0


def test_conviction_liquidity_no_bids_above():
    """No bids at complementary price → 0."""
    ladder = [LadderLevel("kalshi", "bid", 45.0, 200)]
    # For 60¢ → complement = 40¢, but bids are at 45 > 40 → includes 200
    # For 55¢ → complement = 45¢, bid at 45 >= 45 → includes 200
    # For 50¢ → complement = 50¢, bid at 45 < 50 → excludes → 0
    assert conviction_liquidity([ladder], 50.0) == 0.0


# ---- M4: compute_kelly tests ----

def test_kelly_exact():
    """Kelly = edge / payout for a binary bet."""
    pct, stake = compute_kelly(60.0, 50.0, bankroll=10000, kelly_fraction=0.25)
    # edge = 10, payout = 50, full = 10/50 = 0.20, frac = 0.20 * 0.25 = 0.05
    assert pct == pytest.approx(0.05)
    assert stake == pytest.approx(500.0)


def test_kelly_no_edge():
    """No edge → full Kelly = 0."""
    pct, stake = compute_kelly(50.0, 50.0, bankroll=10000)
    assert pct == 0.0
    assert stake == 0.0


def test_kelly_full():
    """Full Kelly (kelly_fraction=1) returns the full f*."""
    pct, stake = compute_kelly(60.0, 50.0, bankroll=10000, kelly_fraction=1.0)
    assert pct == pytest.approx(0.20)
    assert stake == pytest.approx(2000.0)


def test_kelly_clamped():
    """f* < 0 → clamped to 0."""
    pct, stake = compute_kelly(40.0, 50.0, bankroll=10000)  # no edge
    assert pct == 0.0
    assert stake == 0.0


# ---- M4: take_or_post tests ----

def test_take_or_post_take():
    """Best ask below fair → take."""
    assert take_or_post(52.0, 50.0, 55.0) == "take"


def test_take_or_post_post():
    """Wide spread with small edge → post."""
    assert take_or_post(54.0, 44.0, 55.0, min_spread_to_post=2.0) == "post"


def test_take_or_post_none():
    """No ask below fair → None."""
    assert take_or_post(56.0, 54.0, 55.0) is None


def test_take_or_post_take_when_tight():
    """Tight spread → take regardless."""
    assert take_or_post(54.5, 54.0, 55.0, min_spread_to_post=2.0) == "take"


# ---- M4: enrich_alert with conviction + sizing ----

def test_enrich_with_conviction():
    """opposite_outcome_id provided → conviction_liquidity populated."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    engine.clients = {
        "kalshi": _OrderBookMap({
            "live": MockOrderBook(
                asks=[MockLevel(0.53, 200)],  # below fair=55 → real
                bids=[MockLevel(0.48, 100)],
            ),
            "opp": MockOrderBook(
                asks=[MockLevel(0.47, 300)],
                bids=[MockLevel(0.49, 500), MockLevel(0.46, 200)],
            ),
        }),
    }

    alert = Alert(
        market="evt_test",
        live_side="Over",
        fair_cents=55.0,
        signal_book="draftkings",
        signal_price_cents=52.0,
        signal_edge_cents=3.0,
    )
    engine.enrich_alert(alert, outcome_id="live", opposite_outcome_id="opp")

    # Live side ask at 53 < fair 55 → real
    assert alert.traded == "real"
    # gross=2¢, slippage=53*0.0005=0.027¢, net=1.973¢
    assert alert.edge_cents == pytest.approx(1.973, abs=0.01)

    # Conviction: opposite bids at complement >= 48 (100-52)
    # opp has bid 49@500, 46@200 → only 49 >= 48 → 500
    assert alert.conviction_liquidity == 500.0

    # Kelly: net_edge=1.973, payout=46.973, full=1.973/46.973≈0.0420, frac=0.0105
    # kelly_pct stored as percentage → 1.05%
    assert alert.kelly_pct == pytest.approx(1.05, abs=0.01)
    assert alert.max_stake == pytest.approx(105.0, abs=1.0)
    # Spread=5 with net_edge≈2 → post rather than take
    assert alert.execution_mode == "post"


def test_enrich_with_conviction_batch():
    """enrich_alerts with opposite_map works."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    engine.clients = {
        "kalshi": _OrderBookMap({
            "live": MockOrderBook(
                asks=[MockLevel(0.52, 200)],
                bids=[MockLevel(0.48, 100)],
            ),
            "opp": MockOrderBook(
                asks=[MockLevel(0.50, 100)],
                bids=[MockLevel(0.49, 300)],
            ),
        }),
    }

    alerts = [Alert(market="m1", live_side="Over", fair_cents=55.0,
                    signal_price_cents=53.0)]
    engine.enrich_alerts(
        alerts,
        outcome_map={"m1": {"Over": "live"}},
        opposite_map={"m1": {"Over": "opp"}},
    )
    assert alerts[0].conviction_liquidity > 0
    assert alerts[0].traded == "real"


def test_shadow_alert_conviction_defaults():
    """Shadow alerts keep default values for M4 fields."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    engine.clients = {
        "kalshi": _MockClient(MockOrderBook(
            asks=[MockLevel(0.58, 100)],
            bids=[MockLevel(0.55, 200)],
        )),
    }
    alert = Alert(market="m1", live_side="Over", fair_cents=55.0,
                  signal_price_cents=52.0)
    engine.enrich_alert(alert, outcome_id="live", opposite_outcome_id="opp")
    assert alert.traded == "shadow"
    assert alert.kelly_pct is None
    assert alert.max_stake is None
    assert alert.execution_mode is None


# ---- M7: place_bet tests ----

def test_place_bet_skipped_shadow():
    """Shadow alert → skipped."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    alert = Alert(market="m1", live_side="Over", fair_cents=55.0, traded="shadow")
    result = engine.place_bet(alert, outcome_id="oid", confirm=False)
    assert result["status"] == "skipped"
    assert "shadow" in result["reason"]


def test_place_bet_skipped_no_stake():
    """No max_stake → skipped."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    alert = Alert(market="m1", live_side="Over", fair_cents=55.0,
                  traded="real", max_stake=0.0)
    result = engine.place_bet(alert, outcome_id="oid", confirm=False)
    assert result["status"] == "skipped"


def test_place_bet_skipped_no_client():
    """No client for venue → skipped."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    alert = Alert(market="m1", live_side="Over", fair_cents=55.0,
                  traded="real", max_stake=100.0,
                  best_pm={"kalshi": 52.0}, edge_cents=3.0)
    alert.execution_mode = "take"
    result = engine.place_bet(alert, outcome_id="oid", confirm=False)
    assert result["status"] == "skipped"
    assert "no client" in result["reason"].lower()


def test_place_bet_cancelled(monkeypatch):
    """User declines → cancelled."""
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    engine.clients = {
        "kalshi": _MockClient(MockOrderBook(
            asks=[MockLevel(0.52, 200)],
            bids=[MockLevel(0.50, 100)],
        )),
    }
    alert = Alert(market="m1", live_side="Over", fair_cents=55.0,
                  signal_price_cents=50.0, edge_cents=3.0,
                  traded="real", max_stake=100.0,
                  best_pm={"kalshi": 52.0},
                  executable_depth=500.0, kelly_pct=5.0,
                  execution_mode="take")
    result = engine.place_bet(alert, outcome_id="oid", confirm=True)
    assert result["status"] == "cancelled"


def test_place_bet_gated():
    """EXECUTION_ENABLED not set → gated."""
    engine = ExecutionEngine(ExecutionConfig(venue_names=["kalshi"]))
    engine.clients = {
        "kalshi": _MockClient(MockOrderBook(
            asks=[MockLevel(0.52, 200)],
            bids=[MockLevel(0.50, 100)],
        )),
    }
    alert = Alert(market="m1", live_side="Over", fair_cents=55.0,
                  signal_price_cents=50.0, edge_cents=3.0,
                  traded="real", max_stake=100.0,
                  best_pm={"kalshi": 52.0},
                  executable_depth=500.0, kelly_pct=5.0,
                  execution_mode="take")
    result = engine.place_bet(alert, outcome_id="oid", confirm=False)
    assert result["status"] == "gated"
