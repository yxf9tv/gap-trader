"""
parlay_api_client.py
====================
ParlayAPI adapter — the SIGNAL feed (sharp + soft odds, player props, and
Novig/ProphetX order-book DEPTH for conviction). Replaces the old The-Odds-API
client (no Pinnacle => disqualified as a sharp anchor).

Built against the verified ParlayAPI schema (https://parlay-api.com/docs):
  Base : https://parlay-api.com/v1
  Auth : header  X-API-Key: <key>     (preferred)
         or query ?apiKey=<key>       (TOA-compatible)
  Env  : export PARLAY_API_KEY=xxxx

Coverage notes that matter for THIS project (verified from the bookmaker list):
  - SHARP anchor available : pinnacle (sharp), novig + prophetx (exchanges).
    *** Circa is NOT carried by ParlayAPI. *** The anchor therefore uses the
    available subset {pinnacle, novig, prophetx} and Circa falls silently.
  - SOFT signal books available : draftkings, fanduel, caesars, bovada, betmgm,
    fanatics, fliff, bet365, betrivers, hardrock, tipico, pointsbet, parx, stake,
    sugarhouse  (+ DFS: prizepicks, underdog, betr, sleeper, pick6, parlayplay).
    *** SportZino and theScore (the books in the original screenshots) are NOT
    carried. *** The break signal must come from one of the books above.
  - Exchange DEPTH (conviction) : /v1/exchange/{novig|prophetx}/markets.
  - Kalshi / Polymarket DEPTH is NOT full-ladder here (event-markets/search is
    top-of-book yes_bid/yes_ask only) -> get PM/Kalshi depth from PMXT instead.
  - Historical closing-odds + line-movement endpoints -> CLV backtest + the
    line-movement time series (plan §10, §4.7).

This module is a thin, swappable implementation of the SignalFeed interface so
the feed can be replaced without touching the engine.
"""
from __future__ import annotations
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Optional

from smart_money_engine import Quote, american_to_decimal

BASE = "https://parlay-api.com/v1"

# book -> class, used to route into the pipeline (plan §3)
SHARP_BOOKS    = {"pinnacle"}                       # sportsbook sharps (Circa not on ParlayAPI)
EXCHANGE_BOOKS = {"novig", "prophetx"}              # near-zero-margin; also fair anchors
PM_BOOKS       = {"kalshi", "polymarket"}           # execution venues (depth via PMXT)
# everything else carried by ParlayAPI is treated as a soft signal source.

def book_class(book: str) -> str:
    if book in SHARP_BOOKS:    return "sharp"
    if book in EXCHANGE_BOOKS: return "exchange"
    if book in PM_BOOKS:       return "pm"
    return "soft"


class ParlayAPIError(Exception):
    pass


# ----------------------------------------------------------------------------
# Swappable interface — the engine depends on THIS, not on ParlayAPI directly.
# ----------------------------------------------------------------------------
class SignalFeed(ABC):
    @abstractmethod
    def game_odds(self, sport: str, markets, bookmakers=None, regions: str = "us"): ...
    @abstractmethod
    def props(self, sport: str, markets=None, bookmakers=None): ...
    @abstractmethod
    def exchange_depth(self, exchange: str): ...
    @abstractmethod
    def closing_odds(self, sport: str, date: str): ...


class ParlayAPIClient(SignalFeed):
    def __init__(self, api_key: Optional[str] = None, base: str = BASE, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("PARLAY_API_KEY")
        if not self.api_key:
            raise ParlayAPIError("Set PARLAY_API_KEY (or pass api_key=).")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.last_quota = {}

    def _get(self, path: str, params: Optional[dict] = None):
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "X-API-Key": self.api_key,
            "User-Agent": "smart-money-scanner/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for h in ("x-requests-used", "x-requests-remaining", "x-requests-last"):
                    if resp.headers.get(h) is not None:
                        self.last_quota[h] = resp.headers.get(h)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            raise ParlayAPIError(f"HTTP {ex.code} on {path}: {ex.read().decode('utf-8', 'ignore')[:200]}")
        except urllib.error.URLError as ex:
            raise ParlayAPIError(f"network error on {path}: {ex}")

    # --- raw endpoints (verified) -------------------------------------------
    def game_odds(self, sport, markets, bookmakers=None, regions="us", odds_format="american"):
        params = {"regions": regions, "markets": ",".join(markets), "oddsFormat": odds_format}
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        return self._get(f"/sports/{sport}/odds", params)             # markets x regions credits

    def props(self, sport, markets=None, bookmakers=None):
        params = {}
        if markets:    params["markets"] = ",".join(markets)
        if bookmakers: params["bookmakers"] = ",".join(bookmakers)
        return self._get(f"/sports/{sport}/props", params)            # 3 credits, ALL books

    def exchange_depth(self, exchange):                                # novig | prophetx
        return self._get(f"/exchange/{exchange}/markets")             # 1 credit, order-book depth

    def closing_odds(self, sport, date):
        return self._get(f"/historical/sports/{sport}/closing-odds", {"date": date})  # 5 credits


# ----------------------------------------------------------------------------
# Normalizers -> engine's Quote / cents.  Keep American odds OUT of the engine.
# ----------------------------------------------------------------------------
def american_to_cents(american: float) -> float:
    """Implied probability in cents (0-100), the canonical unit."""
    return 100.0 / american_to_decimal(american)

def normalize_prop_rows(rows):
    """ParlayAPI /props rows -> {market_id: {side: {book: Quote}}}.

    Each row: bookmaker, player, market_key, line, over_price, under_price,
    canonical_event_id, commence_time, last_update (ms).
    """
    out = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        mid = f"{r['canonical_event_id']}|{r['player']}|{r['market_key']}|{r['line']}"
        ts = (r.get("last_update", 0) or 0) / 1000.0 or time.time()
        for side, price_key in (("Over", "over_price"), ("Under", "under_price")):
            price = r.get(price_key)
            if price is None:
                continue
            out[mid][side][r["bookmaker"]] = Quote(
                book=r["bookmaker"], decimal=american_to_decimal(price), timestamp=ts)
    return out  # {market_id: {side: {book: Quote}}}

# (game-line odds use the TOA shape; reuse a similar normalizer in M2.)


# ----------------------------------------------------------------------------
# Offline selftest — no network, no key. Verifies parse + cents + class routing.
# ----------------------------------------------------------------------------
_FIXTURE_PROPS = [
    {"bookmaker": "pinnacle", "player": "Chase Burns", "market_key": "player_strikeouts",
     "line": 6.5, "over_price": -120, "under_price": 100,
     "canonical_event_id": "ee78", "commence_time": "2026-05-01T19:35:00Z", "last_update": 1746130000000},
    {"bookmaker": "novig", "player": "Chase Burns", "market_key": "player_strikeouts",
     "line": 6.5, "over_price": -118, "under_price": 102,
     "canonical_event_id": "ee78", "commence_time": "2026-05-01T19:35:00Z", "last_update": 1746130000000},
    {"bookmaker": "draftkings", "player": "Chase Burns", "market_key": "player_strikeouts",
     "line": 6.5, "over_price": 110, "under_price": -134,   # soft book selling the Over cheap = the break
     "canonical_event_id": "ee78", "commence_time": "2026-05-01T19:35:00Z", "last_update": 1746130000000},
]

def run_selftest():
    print("parlay_api_client selftest (offline)\n" + "-" * 44)
    norm = normalize_prop_rows(_FIXTURE_PROPS)
    mid = next(iter(norm))
    over = norm[mid]["Over"]
    assert {"pinnacle", "novig", "draftkings"} <= set(over.keys())
    cents = {b: round(100.0 / q.decimal, 1) for b, q in over.items()}
    assert book_class("pinnacle") == "sharp"
    assert book_class("novig") == "exchange"
    assert book_class("draftkings") == "soft"
    assert book_class("kalshi") == "pm"
    # draftkings Over +110 (~47.6c) is CHEAPER than pinnacle -120 (~54.5c) => the live side
    assert cents["draftkings"] < cents["pinnacle"], cents
    print("  Over prices (cents):", cents)
    print("  book classes route correctly; soft DK Over is the cheap 'break'.")
    print("\n  PASS: parse + cents conversion + book-class routing OK.")
    print("  NOTE: Circa, SportZino, theScore are NOT on ParlayAPI (see header).")

if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        print("Use --selftest for offline check, or import ParlayAPIClient (needs PARLAY_API_KEY).")


# ----------------------------------------------------------------------------
# TOA-compatible game-odds layer.
# ParlayAPI is a documented drop-in for the-odds-api on /odds, so the old
# poller (run_alerts.py) keeps working through these until its M2 rework into
# the signal/execution split. Game-line events use the TOA shape:
#   [{id, commence_time, home_team, away_team,
#     bookmakers:[{key,title,markets:[{key,outcomes:[{name,price}]}]}]}]
# ----------------------------------------------------------------------------
OddsAPIError = ParlayAPIError  # back-compat alias for run_alerts

def get_key(cli_key=None):
    return cli_key or os.environ.get("PARLAY_API_KEY")

def fetch_odds(key, sport, regions="us", markets="h2h", odds_format="american"):
    """Hit ParlayAPI /odds (TOA-shape). Returns (events, quota_dict)."""
    cli = ParlayAPIClient(api_key=key)
    mkts = markets.split(",") if isinstance(markets, str) else list(markets)
    events = cli.game_odds(sport, markets=mkts, regions=regions, odds_format=odds_format)
    return events, dict(cli.last_quota)

def normalize(events, now=None):
    """TOA event JSON -> {market_id: {selection: {book: Quote}}} for the engine.
    Prices are taken as decimal (call with oddsFormat=decimal)."""
    now = now if now is not None else time.time()
    out = defaultdict(lambda: defaultdict(dict))
    for ev in events:
        for bm in ev.get("bookmakers", []):
            bk = bm["key"]
            for mk in bm.get("markets", []):
                mid = f"{ev['id']}|{mk['key']}"
                for oc in mk.get("outcomes", []):
                    price = oc.get("price")
                    if price is None:
                        continue
                    out[mid][oc["name"]][bk] = Quote(
                        book=bk, decimal=float(price), timestamp=now)
    return list(out.items())  # list of (market_id, selections) — evaluate_all iterates tuples
