import os
import sys
import time
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from fair_value import compute_fair_line, FairValueConfig
from signals import detect_break, Alert, SignalConfig
from execution import ExecutionEngine, ExecutionConfig
from matching import MarketMatcher
import parlay_api_client as api
import utils


def log_alerts(alerts: list[Alert], path: str) -> None:
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
                "gross_edge_cents": a.gross_edge_cents,
                "gap_side_label": a.gap_side_label,
            }) + "\n")


def print_alert(a: Alert, now: float) -> None:
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    event = a.market.split("|")[0][:8] if "|" in a.market else a.market[:8]
    edge = a.edge_cents if a.edge_cents is not None else a.signal_edge_cents
    conv = f"  conv=${a.conviction_liquidity:.0f}" if a.conviction_liquidity > 0 else ""
    label = f" [{a.gap_side_label}]" if a.gap_side_label else ""
    print(f"[{stamp}] ({event}) {a.live_side}{label} @ {a.signal_book}  "
          f"fair={a.fair_cents:.1f}¢  got={a.signal_price_cents:.1f}¢  "
          f"edge={edge:+.1f}¢{conv}")


def main():
    api_key = os.getenv("PARLAY_API_KEY")
    if not api_key:
        print("ERROR: PARLAY_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    sport = os.getenv("SPORT", "baseball_mlb")
    interval = int(os.getenv("POLL_INTERVAL", "300"))
    min_edge = float(os.getenv("MIN_EDGE", "1.0"))
    gate_floor = float(os.getenv("GATE_FLOOR", "0.51"))
    slippage_bps = float(os.getenv("SLIPPAGE_BPS", "5.0"))
    min_net_edge = float(os.getenv("MIN_NET_EDGE", "0.5"))
    velocity_window = int(os.getenv("VELOCITY_WINDOW", "10"))
    velocity_std_mult = float(os.getenv("VELOCITY_STD_MULT", "2.0"))
    regions = os.getenv("REGIONS", "us")
    markets = os.getenv("MARKETS", "h2h,spreads,totals")
    log_alerts_path = os.getenv("LOG_ALERTS", "alerts.jsonl")
    log_lines_path = os.getenv("LOG_LINES", "lines.jsonl")
    registry_path = os.getenv("REGISTRY_PATH", "registry.json")
    pmxt_api_key = os.getenv("PMXT_API_KEY", "")

    fair_config = FairValueConfig(
        sport=sport, gate_floor_2way=gate_floor,
        allow_single_sharp=False, devig_method="proportional",
        odds_format="decimal",
    )
    signal_config = SignalConfig(min_edge_cents=min_edge)

    engine = ExecutionEngine(ExecutionConfig(
        venue_names=["polymarket"],
        min_pm_edge_cents=min_edge,
        slippage_bps=slippage_bps,
        min_net_edge_cents=min_net_edge,
        gap_trade=True,
        gap_venues=["polymarket"],
        velocity_guard=True,
        velocity_window=velocity_window,
        velocity_std_mult=velocity_std_mult,
    ))
    engine.connect()

    matcher = MarketMatcher(
        registry_path=registry_path,
        pmxt_api_key=pmxt_api_key if pmxt_api_key else None,
    )

    seen: dict = {}

    print(f"Gap Trader — {sport} | every {interval}s | min_edge={min_edge}¢  "
          f"gate_floor={gate_floor}  (Ctrl-C to stop)\n", flush=True)

    while True:
        try:
            events, quota = api.fetch_odds(
                api_key, sport, regions, markets, "decimal")
        except api.OddsAPIError as e:
            msg = str(e)
            print(f"!! {msg}", file=sys.stderr)
            if "HTTP 429" in msg:
                print("Quota exhausted. Stopping.", file=sys.stderr)
                return
            time.sleep(interval)
            continue

        ts = time.time()
        buckets = api.normalize(events, now=ts)

        for market_id, selections in buckets:
            if len(selections) < 2:
                continue

            sharp_odds = {}
            for sel, books in selections.items():
                sharp_odds[sel] = {}
                for book, q in books.items():
                    if utils.book_class(book) in ("sharp", "exchange"):
                        sharp_odds[sel][book] = q.decimal
            if not all(sharp_odds.get(s) for s in selections):
                continue

            fair_line = compute_fair_line(sharp_odds, fair_config)
            if not fair_line.gate_pass:
                continue

            cents = {}
            for sel, books in selections.items():
                cents[sel] = {}
                for book, q in books.items():
                    cents[sel][book] = round(q.implied * 100.0, 4)

            alerts = detect_break(fair_line, cents, signal_config)
            if not alerts:
                continue

            for alert in alerts:
                alert.market = market_id

            if engine.connected and matcher:
                om, opp = matcher.resolve_alerts(alerts, ["polymarket"])
                engine.enrich_alerts(alerts, om, opp)

            log_alerts(alerts, log_alerts_path)

            for alert in alerts:
                print_alert(alert, ts)

        rem = quota.get("x-requests-remaining")
        if rem is not None and int(rem) < 50:
            print(f"   (quota low: {rem} remaining)", file=sys.stderr)

        time.sleep(interval)


if __name__ == "__main__":
    main()
