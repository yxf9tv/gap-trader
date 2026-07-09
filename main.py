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

DEFAULT_SPORTS = "baseball_mlb,baseball_kbo,baseball_npb,soccer_fifa_world_cup,tennis_atp"
GAPS_LOG = "gaps.jsonl"


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


def log_gap_event(event: str, market: str, side: str, edge: float | None,
                   duration_min: float | None = None, max_edge: float | None = None) -> None:
    rec = {
        "ts": time.time(),
        "event": event,
        "market": market,
        "side": side,
        "edge_cents": edge,
    }
    if duration_min is not None:
        rec["duration_min"] = round(duration_min, 1)
    if max_edge is not None:
        rec["max_edge_cents"] = round(max_edge, 2)
    with open(GAPS_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    api_key = os.getenv("PARLAY_API_KEY")
    if not api_key:
        print("ERROR: PARLAY_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    sports = os.getenv("SPORTS", DEFAULT_SPORTS).split(",")
    sports = [s.strip() for s in sports if s.strip()]
    interval = int(os.getenv("POLL_INTERVAL", "90"))
    min_edge = float(os.getenv("MIN_EDGE", "1.0"))
    gate_floor = float(os.getenv("GATE_FLOOR", "0.51"))
    slippage_bps = float(os.getenv("SLIPPAGE_BPS", "5.0"))
    min_net_edge = float(os.getenv("MIN_NET_EDGE", "0.5"))
    velocity_window = int(os.getenv("VELOCITY_WINDOW", "10"))
    velocity_std_mult = float(os.getenv("VELOCITY_STD_MULT", "2.0"))
    regions = os.getenv("REGIONS", "us")
    markets = os.getenv("MARKETS", "h2h,spreads,totals")
    log_alerts_path = os.getenv("LOG_ALERTS", "alerts.jsonl")
    registry_path = os.getenv("REGISTRY_PATH", "registry.json")
    pmxt_api_key = os.getenv("PMXT_API_KEY", "")

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
    connected_venues = list(engine.clients.keys())
    print(f"  venues: {connected_venues}" if connected_venues else "  !! no venues connected")

    matcher = MarketMatcher(
        registry_path=registry_path,
        pmxt_api_key=pmxt_api_key if pmxt_api_key else None,
    )

    n_sports = len(sports)
    sport_index = 0
    active_gaps: dict[tuple[str, str], dict] = {}

    print(f"Gap Trader — {n_sports} sports round-robin | every {interval}s "
          f"| ~{86400 // interval // n_sports * n_sports} calls/day  "
          f"(Ctrl-C to stop)\n", flush=True)

    while True:
        sport = sports[sport_index % n_sports]
        sport_index += 1

        fair_config = FairValueConfig(
            sport=sport, gate_floor_2way=gate_floor,
            allow_single_sharp=False, devig_method="proportional",
            odds_format="decimal",
        )

        try:
            events, quota = api.fetch_odds(
                api_key, sport, regions, markets, "decimal")
        except api.OddsAPIError as e:
            msg = str(e)
            print(f"!! {sport}: {msg}", file=sys.stderr)
            if "HTTP 429" in msg:
                print("Quota exhausted. Stopping.", file=sys.stderr)
                return
            time.sleep(interval)
            continue
        except Exception as e:
            print(f"!! {sport}: {e}", file=sys.stderr)
            time.sleep(interval)
            continue

        ts = time.time()
        buckets = api.normalize(events, now=ts)

        current_gaps: set[tuple[str, str]] = set()

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
                key = (alert.market, alert.live_side)
                edge = alert.edge_cents if alert.edge_cents is not None else alert.signal_edge_cents

                if alert.traded in ("real", "gap") and edge is not None:
                    current_gaps.add(key)
                    if key not in active_gaps:
                        active_gaps[key] = {"ts": ts, "max_edge": edge}
                        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
                        event = alert.market.split("|")[0][:8] if "|" in alert.market else alert.market[:8]
                        print(f"[{stamp}]  GAP OPEN  ({event}) {alert.live_side}  "
                              f"{alert.signal_book} fair={alert.fair_cents:.1f}¢  "
                              f"got={alert.signal_price_cents:.1f}¢  edge={edge:+.1f}¢",
                              flush=True)
                        log_gap_event("open", alert.market, alert.live_side, edge)
                    else:
                        prev = active_gaps[key]
                        if edge > prev["max_edge"]:
                            prev["max_edge"] = edge
                        prev["ts"] = ts

        # Detect closed gaps
        for key, info in list(active_gaps.items()):
            if key not in current_gaps:
                duration = (ts - info["ts"]) / 60.0
                stamp = time.strftime("%H:%M:%S", time.localtime(ts))
                market, side = key
                event = market.split("|")[0][:8] if "|" in market else market[:8]
                print(f"[{stamp}]  GAP CLOSED ({event}) {side}  "
                      f"lasted {duration:.0f}m  max_edge={info['max_edge']:+.1f}¢",
                      flush=True)
                log_gap_event("close", market, side, None, duration, info["max_edge"])
                del active_gaps[key]

        rem = quota.get("x-requests-remaining")
        seq = sport_index % n_sports
        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
        print(f"[{stamp}] [{seq}/{n_sports}] {sport} — quota: {rem or '?'}  "
              f"gaps: {len(active_gaps)}", flush=True)
        if rem is not None and int(rem) < 50:
            print(f"   (quota low: {rem} remaining)", file=sys.stderr)

        time.sleep(interval)


if __name__ == "__main__":
    main()
