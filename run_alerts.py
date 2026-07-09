#!/usr/bin/env python3
"""
run_alerts.py — M2 rework: Signal/Execution split runner.

Polls ParlayAPI for odds, feeds sharp books into M1 (fair-value service),
runs M2 break detection on all books, and logs structured alerts +
line-movement time series. Places NO bets (execution layer is gated).

  export PARLAY_API_KEY=xxxx
  python run_alerts.py --sport basketball_nba --regions us --markets h2h,spreads,totals \\
                       --interval 300 --min-edge 1.0

Signal/exec split:
  - SIGNAL layer = fair line (M1) + break detection (M2) → Alert objects
  - EXECUTION layer = PMXT integration (M3+), gated behind EXECUTION_ENABLED
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Auto-load .env if present (simple key=value loader, no deps needed)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from fair_value import compute_fair_line, FairValueConfig
from signals import detect_break, Alert, SignalConfig
from execution import ExecutionEngine, ExecutionConfig
from matching import MarketMatcher
import parlay_api_client as api
import utils


def _quotes_to_cents(selections: dict) -> dict[str, dict[str, float]]:
    """Convert Quote dicts to {selection: {book: cents}}."""
    out = {}
    for sel, books in selections.items():
        out[sel] = {}
        for book, q in books.items():
            out[sel][book] = round(q.implied * 100.0, 4)
    return out


def extract_sharp_odds(selections: dict) -> dict[str, dict[str, float]]:
    """Extract only sharp/exchange book odds in decimal format for M1."""
    out = {}
    for sel, books in selections.items():
        out[sel] = {}
        for book, q in books.items():
            if utils.book_class(book) in ("sharp", "exchange"):
                out[sel][book] = q.decimal
    return out


def process_market(
    market_id: str,
    selections: dict,
    fair_config: FairValueConfig,
    signal_config: SignalConfig,
) -> tuple:
    """Run M1 + M2 on one market.

    Returns (fair_line, alerts) or (None, []) on gate failure.
    """
    if len(selections) < 2:
        return None, []

    sharp_odds = extract_sharp_odds(selections)
    if not all(sharp_odds.get(s) for s in selections):
        return None, []

    fair_line = compute_fair_line(sharp_odds, fair_config)
    if not fair_line.gate_pass:
        return fair_line, []

    cents = _quotes_to_cents(selections)
    alerts = detect_break(fair_line, cents, signal_config)
    for alert in alerts:
        alert.market = market_id

    return fair_line, alerts


def log_alerts(alerts: list[Alert], path: str | None) -> None:
    """Append alerts to JSONL log."""
    if not path or not alerts:
        return
    with open(path, "a") as f:
        for a in alerts:
            f.write(json.dumps({
                "market": a.market,
                "live_side": a.live_side,
                "fair_cents": a.fair_cents,
                "signal_book": a.signal_book,
                "signal_price_cents": a.signal_price_cents,
                "signal_edge_cents": a.signal_edge_cents,
                "best_pm": a.best_pm,
                "edge_cents": a.edge_cents,
                "conviction_liquidity": a.conviction_liquidity,
                "executable_depth": a.executable_depth,
                "kelly_pct": a.kelly_pct,
                "max_stake": a.max_stake,
                "execution_mode": a.execution_mode,
                "traded": a.traded,
                "timestamp": a.timestamp,
                "closing_sharp_fair_cents": a.closing_sharp_fair_cents,
                "clv_cents": a.clv_cents,
            }) + "\n")


def log_lines(
    market_id: str,
    fair_line,
    cents: dict[str, dict[str, float]],
    ts: float,
    path: str | None,
) -> None:
    """Append line-movement snapshot to JSONL log."""
    if not path:
        return
    record: dict = {
        "market": market_id,
        "ts": ts,
        "fair_cents": {},
        "sharps": {},
    }
    if fair_line:
        record["fair_cents"] = fair_line.per_side
    if cents:
        for side, books in cents.items():
            for book, price in books.items():
                if utils.book_class(book) in ("sharp", "exchange"):
                    if book not in record["sharps"]:
                        record["sharps"][book] = {}
                    record["sharps"][book][side] = price
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def print_alerts(alerts: list[Alert], seen: dict, now: float) -> None:
    """Dedupe and print new alerts to console. Ranked by conviction when available."""
    # Sort by conviction_liquidity descending if any alert has it
    has_conviction = any(a.conviction_liquidity > 0 for a in alerts)
    sorted_alerts = sorted(
        alerts,
        key=lambda a: -a.conviction_liquidity if has_conviction else -a.signal_edge_cents,
    )
    for a in sorted_alerts:
        key = (a.market, a.live_side, a.signal_book)
        prev = seen.get(key)
        display_edge = a.edge_cents if a.edge_cents is not None else a.signal_edge_cents
        if prev is not None and abs(prev - display_edge) < 0.5:
            continue
        stamp = time.strftime("%H:%M:%S", time.localtime(now))
        event = a.market.split("|")[0][:8] if "|" in a.market else a.market[:8]
        conv = f"  conv=${a.conviction_liquidity:.0f}" if a.conviction_liquidity > 0 else ""
        print(f"[{stamp}] ({event}) {a.live_side} @ {a.signal_book} "
              f"fair={a.fair_cents:.1f}¢  got={a.signal_price_cents:.1f}¢  "
              f"edge={display_edge:+.1f}¢{conv}")
        seen[key] = display_edge


def _log_fair(market_id: str, fair_line, ts: float, path: str) -> None:
    """Append a fair-value snapshot to the time-series log."""
    record = {
        "ts": ts,
        "market": market_id,
        "favorite": fair_line.favorite,
        "gate_pass": fair_line.gate_pass,
        "sides": {side: round(fc, 4) for side, fc in fair_line.per_side.items()},
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def run_live(args) -> None:
    key = api.get_key(args.api_key)
    if not key:
        print("ERROR: PARLAY_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # Optional Kalshi market discovery at startup
    if args.discover:
        from discovery import discover_kalshi_markets
        _PREFIX_MAP = {
            "baseball_mlb": ["KXMLBGAME"],
            "football_nfl": ["KXNFLGAME"],
            "soccer": ["KXSOCCER"],  # placeholder — not validated
        }
        prefixes = _PREFIX_MAP.get(args.discover, ["KXMLBGAME"])
        reg_path = args.registry or os.path.join(os.path.dirname(__file__), "registry.json")
        print(f"Discovering Kalshi markets for {args.discover}...")
        discover_kalshi_markets(reg_path, sport_prefixes=prefixes)
        return

    fair_config = FairValueConfig(
        sport=args.sport,
        gate_floor_2way=args.gate_floor,
        allow_single_sharp=args.allow_single,
        devig_method=args.devig,
        odds_format="decimal",
    )
    signal_config = SignalConfig(min_edge_cents=args.min_edge)
    engine: ExecutionEngine | None = None
    matcher: MarketMatcher | None = None
    pm_venues = args.pm_venues.split(",")
    gap_venues = args.gap_venues.split(",")
    if args.execution:
        engine = ExecutionEngine(ExecutionConfig(
            venue_names=pm_venues,
            min_pm_edge_cents=args.min_edge,
            slippage_bps=args.slippage_bps,
            min_net_edge_cents=args.min_net_edge,
            gap_trade=args.gap_trade,
            gap_venues=gap_venues,
            velocity_guard=args.velocity_guard,
            velocity_window=args.velocity_window,
            velocity_std_mult=args.velocity_std_mult,
        ))
        engine.connect()
        pmxt_key = args.pmxt_api_key or os.environ.get("PMXT_API_KEY")
        matcher = MarketMatcher(
            registry_path=args.registry or os.path.join(os.path.dirname(__file__), "registry.json"),
            pmxt_api_key=pmxt_key,
        )
    seen: dict = {}

    print(f"Polling {args.sport} | regions={args.regions} | markets={args.markets} "
          f"| every {args.interval}s | min_edge={args.min_edge}¢  "
          f"gate_floor={args.gate_floor}  (Ctrl-C to stop)\n")

    while True:
        try:
            events, quota = api.fetch_odds(key, args.sport, args.regions,
                                           args.markets, "decimal")
        except api.OddsAPIError as e:
            msg = str(e)
            print(f"!! {msg}", file=sys.stderr)
            if "HTTP 429" in msg:
                print("Quota exhausted. Stopping.", file=sys.stderr)
                return
            time.sleep(args.interval)
            continue

        ts = time.time()
        buckets = api.normalize(events, now=ts)

        for market_id, selections in buckets:
            fair_line, alerts = process_market(
                market_id, selections, fair_config, signal_config)

            if alerts:
                if engine and engine.connected and matcher:
                    om, opp = matcher.resolve_alerts(alerts, pm_venues)
                    engine.enrich_alerts(alerts, om, opp)
                    if args.place:
                        for alert in alerts:
                            oid = om.get(alert.market, {}).get(alert.live_side)
                            if oid:
                                engine.place_bet(alert, oid, confirm=True)
                log_alerts(alerts, args.log_alerts)
                print_alerts(alerts, seen, ts)

            cents = _quotes_to_cents(selections)
            log_lines(market_id, fair_line, cents, ts, args.log_lines)

            # Log fair value time series (polled per cycle)
            if fair_line.gate_pass and args.log_fair:
                _log_fair(market_id, fair_line, ts, args.log_fair)

        rem = quota.get("x-requests-remaining")
        if rem is not None and int(rem) < 50:
            print(f"   (quota low: {rem} requests remaining)", file=sys.stderr)

        time.sleep(args.interval)


# ---------------------------------------------------------------------------
# Offline self-test: proves M1 + M2 pipeline end-to-end with no network.
# ---------------------------------------------------------------------------

def _mock_event(scenario: str):
    """Build a ParlayAPI-TOA-shape event."""
    from smart_money_engine import american_to_decimal

    def bm(key, home_dec, away_dec):
        return {"key": key, "title": key, "last_update": "now",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Lakers", "price": home_dec},
                    {"name": "Celtics", "price": away_dec},
                ]}]}

    if scenario == "open":
        # Efficient market: all books roughly agree
        h, a = -110, -110
        return [{"id": "evt_lal_bos", "sport_key": "basketball_nba",
                 "commence_time": "2026-01-15T00:10:00Z",
                 "bookmakers": [
                     bm("pinnacle", american_to_decimal(h), american_to_decimal(a)),
                     bm("novig", american_to_decimal(h), american_to_decimal(a)),
                     bm("draftkings", american_to_decimal(h), american_to_decimal(a)),
                 ]}]
    else:
        # Break: DraftKings sells Lakers cheap while sharps have them as favorite
        # Sharps: Lakers -200 (66.7¢) / -190 (65.5¢), Celtics +170/+160 → Lakers ~63¢
        # DK: Lakers -110 (52.4¢), Celtics -110 (52.4¢) → Lakers gap = ~10¢
        return [{"id": "evt_lal_bos", "sport_key": "basketball_nba",
                 "commence_time": "2026-01-15T00:10:00Z",
                 "bookmakers": [
                     bm("pinnacle",
                        american_to_decimal(-200), american_to_decimal(170)),
                     bm("novig",
                        american_to_decimal(-190), american_to_decimal(160)),
                     bm("draftkings",
                        american_to_decimal(-110), american_to_decimal(-110)),
                 ]}]


def run_selftest():
    print("=== M2 SELFTEST (offline, no network) ===\n")

    fair_config = FairValueConfig(odds_format="decimal")
    signal_config = SignalConfig(min_edge_cents=1.0)
    t0 = time.time()

    print("t0  efficient market (no break):")
    b0 = api.normalize(_mock_event("open"), now=t0)
    for mid, selections in b0:
        _, alerts = process_market(mid, selections, fair_config, signal_config)
        if alerts:
            for a in alerts:
                print(f"   ALERT: {a}")
        else:
            print("   (no alerts — good, no break in efficient market)")
    print()

    print("t1  DraftKings breaks from sharp consensus on Lakers:")
    b1 = api.normalize(_mock_event("break"), now=t0 + 120)
    for mid, selections in b1:
        fair_line, alerts = process_market(mid, selections, fair_config, signal_config)
        assert fair_line.gate_pass, f"gate failed: {fair_line.reason}"
        assert fair_line.favorite == "Lakers"
        print(f"   Fair line: {fair_line.per_side}")
        print(f"   Sharps used: {fair_line.sharps_used}")

        if alerts:
            for a in alerts:
                print(f"   ALERT: {a.live_side} at {a.signal_book} "
                      f"(fair={a.fair_cents:.1f}¢, got={a.signal_price_cents:.1f}¢, "
                      f"edge={a.signal_edge_cents:.1f}¢)")
        else:
            print("   (no alerts)")

    # Assertions
    assert any(
        a.signal_book == "draftkings" and a.live_side == "Lakers" and a.signal_edge_cents > 5
        for _, selections in b1
        for fair_line, alerts in [process_market(mid, selections, fair_config, signal_config)]
        for a in alerts
    ), "expected a Lakers break on DraftKings with edge > 5¢"

    print("\n   PASS: M1 fair line + M2 break detection working end-to-end.")
    print("   Sharp fair computed, break detected on DK Lakers, alert emitted.")


def build_parser():
    p = argparse.ArgumentParser(
        description="Smart-money signal runner (M1+M2, alerts only)")
    p.add_argument("--selftest", action="store_true",
                   help="run offline pipeline test")
    p.add_argument("--api-key", default=None)
    p.add_argument("--sport", default="basketball_nba")
    p.add_argument("--regions", default="us")
    p.add_argument("--markets", default="h2h,spreads,totals")
    p.add_argument("--interval", type=int, default=90,
                   help="seconds between polls")
    p.add_argument("--min-edge", type=float, default=1.0,
                   help="minimum edge in cents to fire alert")
    p.add_argument("--gate-floor", type=float, default=0.51,
                   help="favorite consensus probability floor (0.51 = 51%%)")
    p.add_argument("--allow-single", action="store_true",
                   help="allow fair line from a single sharp book")
    p.add_argument("--devig", default="proportional",
                   choices=["proportional", "power"])
    p.add_argument("--log-alerts", default="alerts.jsonl",
                   help="JSONL alert log path")
    p.add_argument("--log-lines", default="lines.jsonl",
                   help="JSONL line-movement time-series path")
    p.add_argument("--execution", action="store_true",
                   help="enable PMXT execution layer (EXECUTION_ENABLED)")
    p.add_argument("--pm-venues", default="kalshi,polymarket",
                   help="comma-separated PM venues for execution")
    p.add_argument("--gap-venues", default="polymarket",
                   help="comma-separated PM venues for gap-side check (default: polymarket)")
    p.add_argument("--registry", default=None,
                   help="path to registry.json for market matching")
    p.add_argument("--pmxt-api-key", default=None,
                   help="PMXT API key for auto-market-discovery (or PMXT_API_KEY env)")
    p.add_argument("--discover", default=None,
                   help="discover Kalshi markets and save to registry.json (sport: baseball_mlb, etc)")
    p.add_argument("--place", action="store_true",
                   help="prompt to place bets on real alerts (requires --execution)")
    p.add_argument("--log-fair", default="",
                   help="path to fair-value time-series JSONL (empty = skip)")
    p.add_argument("--gap-trade", action="store_true",
                   help="enable gap-trade mode (limit orders at fair, both sides)")
    p.add_argument("--slippage-bps", type=float, default=5.0,
                   help="slippage buffer in basis points (default 5 = 0.05%%)")
    p.add_argument("--min-net-edge", type=float, default=0.5,
                   help="minimum edge in cents AFTER fees + slippage")
    p.add_argument("--velocity-guard", action="store_true", default=True,
                   help="enable velocity guard (rolling std filter on fair value)")
    p.add_argument("--no-velocity-guard", action="store_false", dest="velocity_guard",
                   help="disable velocity guard")
    p.add_argument("--velocity-window", type=int, default=10,
                   help="rolling window for velocity guard (polls)")
    p.add_argument("--velocity-std-mult", type=float, default=2.0,
                   help="std multiplier for velocity guard")
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    if args.selftest:
        run_selftest()
    else:
        try:
            run_live(args)
        except KeyboardInterrupt:
            print("\nstopped.")
