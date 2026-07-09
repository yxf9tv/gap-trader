"""
execution.py — M3+M4: Execution layer (PMXT integration + conviction/edge engine).

Takes Alerts from the M2 signal layer, fetches PM intl + Kalshi order books
via PMXT, and enriches each Alert with:
  - best_pm:            best ask per venue
  - edge_cents:         fair - best PM ask (the executable edge)
  - executable_depth:   sum of asks below fair across all PM venues
  - conviction_liquidity: opposite-side bid sizes at complementary price
  - kelly_pct/max_stake: depth-capped Kelly sizing
  - execution_mode:     "take" | "post" | None (shadow)
  - traded:             "real" if any PM ask is below fair, else "shadow"

Market matching (mapping signal side → PM outcome ID) is M5. For M3/M4, the
caller provides outcome_id + optional opposite_outcome_id per alert.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from signals import Alert
from pmxt_execution import LadderLevel, make_venue, get_ladder, \
    best_ask_cents, best_bid_cents, executable_depth, conviction_liquidity, \
    place_order, EXECUTION_ENABLED


DEFAULT_VENUES = ["kalshi", "polymarket"]
DEFAULT_BANKROLL = 10_000.0
DEFAULT_KELLY_FRAC = 0.25


# ---------------------------------------------------------------------------
# Velocity guard — rolling std filter on fair value time series
# ---------------------------------------------------------------------------

class VelocityGuard:
    """Track per-market fair value history and flag unstable (high-variance) periods.

    A fair value that deviates by more than std_multiplier × rolling std from
    the rolling mean is considered unstable — skip enrichment when velocity
    guard is enabled.
    """

    def __init__(self, max_history: int = 10, std_multiplier: float = 2.0):
        self.history: dict[str, list[float]] = {}
        self.max_history = max_history
        self.std_multiplier = std_multiplier

    def _key(self, market: str, side: str) -> str:
        return f"{market}_{side}"

    def record(self, market: str, side: str, fair_cents: float) -> None:
        k = self._key(market, side)
        if k not in self.history:
            self.history[k] = []
        self.history[k].append(fair_cents)
        if len(self.history[k]) > self.max_history:
            self.history[k] = self.history[k][-self.max_history:]

    def is_stable(self, market: str, side: str, fair_cents: float) -> bool:
        k = self._key(market, side)
        hist = self.history.get(k, [])
        if len(hist) < 3:
            return True  # not enough samples yet
        mean = sum(hist) / len(hist)
        variance = sum((x - mean) ** 2 for x in hist) / len(hist)
        std = variance ** 0.5
        if std == 0:
            return True  # perfectly flat = not jumping
        return abs(fair_cents - mean) <= self.std_multiplier * std


@dataclass
class ExecutionConfig:
    venue_names: list[str] = field(default_factory=lambda: DEFAULT_VENUES)
    min_pm_edge_cents: float = 0.0
    bankroll: float = DEFAULT_BANKROLL
    kelly_fraction: float = DEFAULT_KELLY_FRAC
    # Minimum spread (cents) below which we always take
    min_spread_to_post: float = 2.0
    # Fees and slippage
    venue_fee_cents: dict = field(default_factory=lambda: {"kalshi": 0.0, "polymarket": 0.0})
    slippage_bps: float = 5.0          # 0.05% slippage buffer for thin books
    min_net_edge_cents: float = 0.5    # minimum edge AFTER fees + slippage
    gap_trade: bool = False            # enable gap-trade mode (limit orders at fair)
    gap_venues: list[str] = field(default_factory=lambda: ["polymarket"])  # venues for gap-side check
    velocity_guard: bool = True        # skip enrichment when fair value is jumping
    velocity_window: int = 10          # rolling window for velocity guard
    velocity_std_mult: float = 2.0     # std multiplier for velocity guard


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------

def compute_kelly(
    fair_cents: float,
    exec_price_cents: float,
    bankroll: float = DEFAULT_BANKROLL,
    kelly_fraction: float = DEFAULT_KELLY_FRAC,
) -> tuple[float, float]:
    """Depth-capped Kelly stake for a binary PM bet.

    Returns (kelly_pct, max_stake_usd) where:
      kelly_pct  = fraction of bankroll (0-1) per full/frac Kelly
      max_stake  = kelly_pct * bankroll (un-capcced; caller caps at depth)

    For binary: f* = (fair_cents - exec_price_cents) / (100 - exec_price_cents)
    """
    if exec_price_cents <= 0 or exec_price_cents >= 100:
        return 0.0, 0.0
    payout = 100.0 - exec_price_cents
    if payout <= 0:
        return 0.0, 0.0
    full = (fair_cents - exec_price_cents) / payout
    pct = max(0.0, full * kelly_fraction)
    return pct, pct * bankroll


def take_or_post(
    best_ask: float | None,
    best_bid: float | None,
    fair_cents: float,
    min_spread_to_post: float = 2.0,
) -> str | None:
    """Decide execution mode.

    Returns "take", "post", or None (shadow/no trade).
    """
    if best_ask is None or best_ask >= fair_cents:
        return None
    if best_bid is None:
        return "take"
    spread = best_ask - best_bid
    if spread <= min_spread_to_post or (fair_cents - best_ask) > spread:
        return "take"
    return "post"


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """Manages PM venue connections and enriches alerts with order-book data."""

    def __init__(self, config: Optional[ExecutionConfig] = None):
        self.config = config or ExecutionConfig()
        self.clients: dict[str, object] = {}
        self.connected = False
        if self.config.velocity_guard:
            self.velocity = VelocityGuard(
                max_history=self.config.velocity_window,
                std_multiplier=self.config.velocity_std_mult,
            )
        else:
            self.velocity = None

    def connect(self) -> None:
        """Initialize self-custody venue clients from env creds."""
        for venue in self.config.venue_names:
            try:
                self.clients[venue] = make_venue(venue)
            except Exception as e:
                print(f"  !! {venue} connection failed: {e}")
        self.connected = bool(self.clients)

    def disconnect(self) -> None:
        self.clients.clear()
        self.connected = False

    def enrich_alert(
        self,
        alert: Alert,
        outcome_id: str,
        opposite_outcome_id: Optional[str] = None,
    ) -> Alert:
        """Fetch PM books, populate execution + conviction + sizing fields.

        The caller provides the PM outcome_id for the live side, and
        optionally the opposite_outcome_id for conviction liquidity.
        Market matching is M5.
        """
        # Velocity guard: skip enrichment if fair value is jumping
        if self.velocity is not None:
            self.velocity.record(alert.market, alert.live_side, alert.fair_cents)
            if not self.velocity.is_stable(alert.market, alert.live_side, alert.fair_cents):
                alert.traded = "shadow"
                alert.edge_cents = None
                return alert

        alert.best_pm = {}
        total_depth = 0.0
        best_ask_overall: float | None = None
        best_bid_overall: float | None = None
        opp_ladders: list[list[LadderLevel]] = []

        for venue in self.config.venue_names:
            client = self.clients.get(venue)
            if client is None:
                continue
            try:
                ladder = get_ladder(client, venue, outcome_id)
            except Exception:
                continue

            ask = best_ask_cents(ladder)
            if ask is not None:
                alert.best_pm[venue] = round(ask, 4)
                if best_ask_overall is None or ask < best_ask_overall:
                    best_ask_overall = ask

            bid = best_bid_cents(ladder)
            if bid is not None:
                if best_bid_overall is None or bid > best_bid_overall:
                    best_bid_overall = bid

            total_depth += executable_depth(ladder, alert.fair_cents)

            # Fetch opposite side for conviction
            if opposite_outcome_id:
                try:
                    opp_ladder = get_ladder(client, venue, opposite_outcome_id)
                    opp_ladders.append(opp_ladder)
                except Exception:
                    pass

        alert.executable_depth = round(total_depth, 2)

        # --- executable edge (net of fees + slippage) ---
        if best_ask_overall is not None and best_ask_overall < alert.fair_cents:
            gross_edge = alert.fair_cents - best_ask_overall
            alert.gross_edge_cents = round(gross_edge, 4)
            # Fee per venue: use the cheapest venue's fee as conservative estimate
            best_venue = min(alert.best_pm, key=lambda v: alert.best_pm[v]) \
                if alert.best_pm else "kalshi"
            fee = self.config.venue_fee_cents.get(best_venue, 0.0)
            slippage = best_ask_overall * (self.config.slippage_bps / 10000.0)
            net_edge = gross_edge - fee - slippage
            if net_edge >= self.config.min_net_edge_cents:
                alert.edge_cents = round(net_edge, 4)
                alert.traded = "real" if not self.config.gap_trade else "gap"
            else:
                alert.edge_cents = None
                alert.traded = "shadow"
        else:
            alert.edge_cents = None
            alert.gross_edge_cents = None
            alert.traded = "shadow"

        # --- conviction liquidity ---
        if opp_ladders:
            alert.conviction_liquidity = round(
                conviction_liquidity(opp_ladders, alert.signal_price_cents), 2)

        # --- sizing (use net edge for Kelly) ---
        if alert.traded in ("real", "gap") and best_ask_overall is not None \
                and alert.edge_cents is not None:
            exec_price = best_ask_overall + fee + slippage
            kelly_pct, kelly_stake = compute_kelly(
                alert.fair_cents, exec_price,
                self.config.bankroll, self.config.kelly_fraction)
            alert.kelly_pct = round(kelly_pct * 100, 2)  # store as percentage
            alert.max_stake = round(min(kelly_stake, alert.executable_depth), 2)
            alert.execution_mode = take_or_post(
                best_ask_overall, best_bid_overall,
                alert.fair_cents, self.config.min_spread_to_post)

        return alert

    def enrich_alerts(
        self,
        alerts: list[Alert],
        outcome_map: dict[str, dict[str, str]],
        opposite_map: Optional[dict[str, dict[str, str]]] = None,
    ) -> list[Alert]:
        """Enrich multiple alerts.

        outcome_map:  market -> {live_side: outcome_id}
        opposite_map: market -> {live_side: opposite_outcome_id} (optional)

        In gap-trade mode, also checks the complementary side for each alert
        and emits a second alert if the No outcome is also undervalued.
        """
        extra: list[Alert] = []
        for alert in alerts:
            by_side = outcome_map.get(alert.market, {})
            outcome_id = by_side.get(alert.live_side)
            if not outcome_id:
                continue
            opp_id = None
            if opposite_map:
                opp_by_side = opposite_map.get(alert.market, {})
                opp_id = opp_by_side.get(alert.live_side)
            self.enrich_alert(alert, outcome_id, opp_id)

            # Two-sided gap detection: check the complementary side
            if self.config.gap_trade and opp_id:
                fair_no = 100.0 - alert.fair_cents
                # Fetch No-side order book and check for undervalue
                for venue in self.config.gap_venues:
                    client = self.clients.get(venue)
                    if client is None:
                        continue
                    try:
                        from pmxt_execution import get_ladder, best_ask_cents
                        ladder = get_ladder(client, venue, opp_id)
                        ask = best_ask_cents(ladder)
                    except Exception:
                        continue
                    if ask is not None and ask < fair_no:
                        fee = self.config.venue_fee_cents.get(venue, 0.0)
                        slippage = ask * (self.config.slippage_bps / 10000.0)
                        net = fair_no - ask - fee - slippage
                        if net >= self.config.min_net_edge_cents:
                            no_alert = Alert(
                                market=alert.market,
                                live_side=f"{alert.live_side} (No)",
                                fair_cents=round(fair_no, 4),
                                signal_book=alert.signal_book,
                                signal_price_cents=round(100.0 - alert.signal_price_cents, 4),
                                signal_edge_cents=round(fair_no - (100.0 - alert.signal_price_cents), 4),
                                gap_side_label="No",
                                timestamp=alert.timestamp,
                                traded="gap",
                            )
                            self.enrich_alert(no_alert, opp_id)
                            extra.append(no_alert)
                    break  # use first venue with a No-side ask

        alerts.extend(extra)
        return alerts

    # ------------------------------------------------------------------
    # M7: Manual placement with PMXT
    # ------------------------------------------------------------------

    def place_bet(
        self,
        alert: Alert,
        outcome_id: str,
        venue: str | None = None,
        confirm: bool = True,
    ) -> dict:
        """Place a bet via PMXT with manual confirmation.

        Returns a dict with the order result or cancellation info.

        Requires:
          - alert.traded == "real"  (PM has edge below fair)
          - alert.max_stake is set  (kelly cap)
          - alert.edge_cents is set (executable edge)
          - EXECUTION_ENABLED=1 in env to actually send orders
        """
        if alert.traded != "real":
            return {"status": "skipped", "reason": "shadow alert (no executable edge)"}
        if alert.max_stake is None or alert.max_stake <= 0:
            return {"status": "skipped", "reason": "zero max_stake (no depth)"}

        # Pick the best venue (cheapest ask)
        if venue is None:
            best_venue = min(alert.best_pm, key=lambda v: alert.best_pm[v])
        else:
            best_venue = venue

        client = self.clients.get(best_venue)
        if client is None:
            return {"status": "skipped", "reason": f"no client for {best_venue}"}

        best_ask = alert.best_pm.get(best_venue)
        if best_ask is None:
            return {"status": "skipped", "reason": f"no ask for {best_venue}"}

        stake = alert.max_stake
        price_decimal = best_ask / 100.0  # cents → decimal

        # Print execution instruction
        print(f"\n{'─' * 60}")
        print(f"BET INSTRUCTION")
        print(f"{'─' * 60}")
        print(f"  Market:    {alert.market}")
        print(f"  Side:      {alert.live_side}")
        print(f"  Venue:     {best_venue}")
        print(f"  Price:     {best_ask:.1f}¢ ({price_decimal:.4f})")
        print(f"  Stake:     ${stake:.2f}")
        print(f"  Edge:      {alert.edge_cents:+.2f}¢")
        print(f"  Conviction: ${alert.conviction_liquidity:.0f}")
        print(f"  Kelly:     {alert.kelly_pct:.1f}%")
        print(f"  Depth:     ${alert.executable_depth:.0f}")
        print(f"{'─' * 60}")

        if confirm:
            try:
                response = input("  Place this bet? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                response = "n"
            if response.lower() not in ("y", "yes"):
                return {"status": "cancelled", "reason": "user declined"}

        # Attempt execution
        try:
            result = place_order(
                client,
                market_id=alert.market,
                outcome_id=outcome_id,
                side="buy",
                amount=stake,
                price=price_decimal,
            )
            order_id = getattr(result, "id", str(result))
            print(f"  ORDER PLACED: {order_id}")
            print(f"{'─' * 60}\n")
            return {"status": "placed", "order_id": order_id, "venue": best_venue,
                    "stake": stake, "price": best_ask}
        except RuntimeError as e:
            # GATE B enforcement: EXECUTION_ENABLED != 1
            print(f"  !! {e}")
            print(f"{'─' * 60}\n")
            return {"status": "gated", "reason": str(e)}
        except Exception as e:
            print(f"  !! Order failed: {e}")
            print(f"{'─' * 60}\n")
            return {"status": "error", "reason": str(e)}
