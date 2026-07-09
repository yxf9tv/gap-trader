"""
pmxt_execution.py
=================
PMXT (self-custody) wrapper — the EXECUTION + PM/Kalshi DEPTH layer.
Also includes PmxtRestClient for the PMXT hosted API (market discovery).

"Self-custody" = pass YOUR OWN venue credentials and run PMXT's local server
(auto_start_server=True); do NOT pass pmxt_api_key (that's the hosted path).
Orders and reads go direct to each venue with your keys; nothing is custodied
by PMXT's hosted service.

Market discovery uses PMXT's REST API (api.pmxt.dev/v0) — separate from
self-custody. Requires PMXT_API_KEY env var.

Verified against pmxt v2.51.x:
  Venues present : Kalshi, Polymarket, PolymarketUS  (all three of ours!)
     -> NOTE: PolymarketUS is now in PMXT, so PM US is no longer a separate
        native build. It's gated only by KYC / US-eligibility, not tooling.
  fetch_order_book(outcome_id, limit=None, params=FetchOrderBookParams)
     -> OrderBook { bids: [OrderLevel], asks: [OrderLevel], timestamp, datetime }
        OrderLevel { price: float, size: float }
  get_execution_price(order_book, side, amount) -> float   (slippage-aware fill price)
  create_order(market_id, outcome_id, *, side, order_type, amount, price=None,
               slippage_pct=None, ...) -> Order
  fetch_balance / fetch_positions / watch_order_book (WS) / fetch_ohlcv (history)

Construction (self-custody):
  Kalshi(api_key=KALSHI_API_KEY, private_key=KALSHI_PRIVATE_KEY, auto_start_server=True)
  Polymarket(private_key=POLYMARKET_PRIVATE_KEY, signature_type="gnosis-safe", auto_start_server=True)
  PolymarketUS(api_key=..., private_key=..., auto_start_server=True)   # KYC-gated

Price unit: PMXT returns probability-style prices. We convert to CENTS (x100)
to match the engine's canonical unit. *** VERIFY the unit each venue returns
against live data before trusting fills (Kalshi may already be 1-99). ***
"""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Literal, Optional


@dataclass
class LadderLevel:
    """Normalized order-book row (matches DATA_CONTRACTS.md)."""
    venue: str
    side: str            # "ask" (you buy into) | "bid" (counterparty / PM-side conviction)
    price_cents: float
    size_usd: float


def _to_cents(price: float) -> float:
    # PMXT prices are ~0..1 (prob). Convert to cents. Verify per venue.
    return round(price * 100.0, 4) if price <= 1.0 else round(price, 4)


def _get_env(*names: str) -> str | None:
    """Return the first non-empty env var from a list of names."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _read_file_or_env(path_var: str, fallback_var: str) -> str | None:
    """Read a file if path_var is set; otherwise return fallback_var's value."""
    path = os.environ.get(path_var)
    if path:
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            pass
    return os.environ.get(fallback_var)


# ----------------------------------------------------------------------------
# PMXT hosted REST API — market discovery (separate from self-custody depth)
# ----------------------------------------------------------------------------

PMXT_REST_BASE = "https://api.pmxt.dev/v0"

class PmxtRestClient:
    """Client for PMXT's hosted REST API (api.pmxt.dev/v0).

    Used for market discovery (finding outcome IDs by team name / sport).
    Requires PMXT_API_KEY env var. Separate from self-custody venue clients.
    """

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("PMXT_API_KEY")
        if not key:
            raise ValueError("PMXT_API_KEY required for PmxtRestClient")
        self.api_key = key
        self.base = PMXT_REST_BASE

    def _get(self, path: str, params: Optional[dict] = None, retries: int = 3) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "smart-money-scanner/1.0",
        })
        last_ex: Exception | None = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as ex:
                body = ex.read().decode("utf-8", "ignore")[:200]
                if ex.code == 429 and attempt < retries - 1:
                    import time
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"PMXT HTTP {ex.code} on {path}: {body}") from ex
            except urllib.error.URLError as ex:
                last_ex = ex
                if attempt < retries - 1:
                    import time
                    time.sleep(2 ** attempt)
                    continue
                break
        raise RuntimeError(f"PMXT network error on {path}: {last_ex}") from last_ex

    def search_markets(
        self,
        query: str,
        limit: int = 10,
        exchange: Optional[str] = None,
    ) -> list[dict]:
        """Search markets by text query. Returns list of market dicts.

        Note: PMXT's exchange query param is a hint, not a strict filter.
        Always post-filter by exchange in calling code.
        """
        params: dict = {"query": query, "limit": limit}
        return self._get("/markets", params).get("data", [])

    def search_events(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        """Search events by text query. Returns list of event dicts."""
        return self._get("/events", {"query": query, "limit": limit}).get("data", [])

    def find_outcome_for_team(
        self,
        team_name: str,
        sport_hint: Optional[str] = None,
        exchange: str = "kalshi",
    ) -> dict | None:
        """Find a prediction-market outcome matching a team name.

        Searches PMXT markets for {team_name}, looks for game-winner markets
        from the specified exchange where one outcome label matches the team.

        Returns:
            { "market_id": str, "outcome_id": str,
              "opposite_outcome_id": str, "venue": str,
              "title": str, "price": float } or None
        """
        query = team_name
        markets = self.search_markets(query, limit=30)
        if not markets:
            return None

        # Post-filter by exchange (PMXT's exchange param is unreliable)
        markets = [m for m in markets if m.get("sourceExchange", "").lower() == exchange.lower()]

        seen = set()
        for m in markets:
            mid = m.get("marketId") or m.get("id", "")
            if mid in seen:
                continue
            seen.add(mid)
            outcomes = m.get("outcomes", [])
            if not outcomes:
                continue

            title_lower = m.get("title", "").lower()
            # Skip markets that don't look like game winners
            if "winner" not in title_lower:
                continue

            yes_oc = None
            no_oc = None
            team_lower = team_name.lower()

            for oc in outcomes:
                label = oc.get("label", "").strip()
                oid = oc.get("outcomeId", "")
                if not oid:
                    continue
                label_lower = label.lower()

                if label_lower == team_lower:
                    yes_oc = oc
                elif label_lower.startswith("not ") and team_lower in label_lower:
                    no_oc = oc

            # Fuzzy fallback: if no exact match, try contains
            if not yes_oc:
                for oc in outcomes:
                    label = oc.get("label", "").strip()
                    label_lower = label.lower()
                    if not label_lower.startswith("not ") and team_lower in label_lower:
                        yes_oc = oc
                    elif label_lower.startswith("not ") and team_lower in label_lower:
                        no_oc = oc
                    if yes_oc:
                        break

            if yes_oc:
                opp_id = no_oc.get("outcomeId", "") if no_oc else ""
                return {
                    "market_id": mid,
                    "outcome_id": yes_oc["outcomeId"],
                    "opposite_outcome_id": opp_id or None,
                    "venue": m.get("sourceExchange", exchange),
                    "title": m.get("title", ""),
                    "price": yes_oc.get("price"),
                }

        return None

    def find_outcome_for_sides(
        self,
        home_team: str,
        away_team: str,
        sport_hint: Optional[str] = None,
        exchange: str = "kalshi",
    ) -> dict | None:
        """Find a market matching home_team vs away_team.

        Tries "home vs away Winner?" then individual team names.
        Post-filters by exchange (PMXT's filter is unreliable).
        """
        for query in (f"{home_team} vs {away_team}", f"{home_team} vs", home_team):
            markets = self.search_markets(query, limit=15)
            if not markets:
                continue
            markets = [m for m in markets if m.get("sourceExchange", "").lower() == exchange.lower()]
            for m in markets:
                outcomes = m.get("outcomes", [])
                title = m.get("title", "")
                mid = m.get("marketId") or m.get("id", "")
                if not outcomes or not mid:
                    continue

                home_oc = None
                away_oc = None
                for oc in outcomes:
                    label = oc.get("label", "").strip()
                    oid = oc.get("outcomeId", "")
                    if not oid:
                        continue
                    cl = label.lower()
                    if cl == home_team.lower() or cl == f"not {home_team.lower()}":
                        home_oc = oc
                    elif cl == away_team.lower() or cl == f"not {away_team.lower()}":
                        away_oc = oc

                if home_oc or away_oc:
                    result = {
                        "market_id": mid,
                        "venue": m.get("sourceExchange", exchange),
                        "title": title,
                        "home": None,
                        "away": None,
                    }
                    if home_oc:
                        result["home"] = {
                            "outcome_id": home_oc["outcomeId"],
                            "label": home_oc.get("label", ""),
                            "price": home_oc.get("price"),
                        }
                    if away_oc:
                        result["away"] = {
                            "outcome_id": away_oc["outcomeId"],
                            "label": away_oc.get("label", ""),
                            "price": away_oc.get("price"),
                        }
                    return result
        return None


def make_venue(name: str, *, auto_start_server: bool = True):
    """Construct a venue in SELF-CUSTODY mode from env creds. No pmxt_api_key."""
    import pmxt
    if name == "kalshi":
        # PMXT auto-picks up PMXT_API_KEY env var → hosted mode → EthAccountSigner
        # crashes on non-Ethereum keys. Hide it to force self-custody mode.
        _saved = os.environ.pop("PMXT_API_KEY", None)
        try:
            key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
            if key_path and os.path.exists(key_path):
                with open(key_path) as _f:
                    _pem = _f.read()
            else:
                _pem = os.environ.get("KALSHI_PRIVATE_KEY")
            return pmxt.Kalshi(
                api_key=os.environ.get("KALSHI_API_KEY"),
                private_key=_pem,
                auto_start_server=auto_start_server)
        finally:
            if _saved is not None:
                os.environ["PMXT_API_KEY"] = _saved
    if name == "polymarket":
        _saved = os.environ.pop("PMXT_API_KEY", None)
        try:
            return pmxt.Polymarket(
                private_key=_get_env("POLYMARKET_PRIVATE_KEY", "POLY_PK"),
                proxy_address=_get_env("POLYMARKET_PROXY_ADDRESS", "POLY_FUNDER"),
                signature_type="gnosis-safe",
                auto_start_server=auto_start_server)
        finally:
            if _saved is not None:
                os.environ["PMXT_API_KEY"] = _saved
    if name == "polymarket_us":
        return pmxt.PolymarketUS(
            api_key=os.environ.get("POLYMARKET_US_API_KEY"),
            private_key=os.environ.get("POLYMARKET_US_PRIVATE_KEY"),
            auto_start_server=auto_start_server)
    raise ValueError(f"unknown venue {name}")


def ladder_from_order_book(venue: str, ob) -> List[LadderLevel]:
    """OrderBook -> normalized LadderLevel list (asks + bids), prices in cents."""
    levels: List[LadderLevel] = []
    for lvl in getattr(ob, "asks", []) or []:
        levels.append(LadderLevel(venue, "ask", _to_cents(lvl.price), lvl.size))
    for lvl in getattr(ob, "bids", []) or []:
        levels.append(LadderLevel(venue, "bid", _to_cents(lvl.price), lvl.size))
    return levels


def get_ladder(client, venue: str, outcome_id: str) -> List[LadderLevel]:
    """Fetch + normalize one venue's book for an outcome."""
    ob = client.fetch_order_book(outcome_id)
    if isinstance(ob, list):
        ob = ob[0]
    return ladder_from_order_book(venue, ob)


# ----------------------------------------------------------------------------
# Utility functions — work on normalized ladder data, no PMXT dependency.
# ----------------------------------------------------------------------------

def best_ask_cents(ladder: List[LadderLevel]) -> float | None:
    """Lowest ask price across all ask-side levels in a per-outcome ladder."""
    asks = [l.price_cents for l in ladder if l.side == "ask"]
    return min(asks) if asks else None


def best_bid_cents(ladder: List[LadderLevel]) -> float | None:
    """Highest bid price across all bid-side levels."""
    bids = [l.price_cents for l in ladder if l.side == "bid"]
    return max(bids) if bids else None


def executable_depth(ladder: List[LadderLevel], fair_cents: float) -> float:
    """Sum size of all asks below fair_cents."""
    return sum(l.size_usd for l in ladder if l.side == "ask"
               and l.price_cents < fair_cents)


def merged_asks(ladders: List[List[LadderLevel]]) -> List[LadderLevel]:
    """Merge ask-side levels from multiple venues, sorted by price ascending."""
    merged: List[LadderLevel] = []
    for ladder in ladders:
        for l in ladder:
            if l.side == "ask":
                merged.append(l)
    merged.sort(key=lambda x: x.price_cents)
    return merged


def conviction_liquidity(
    ladders: List[List[LadderLevel]],
    side_price_cents: float,
) -> float:
    """Sum opposite-side bid sizes across venue ladders.

    For the live side (e.g. 'Over') at price P:
      - Complementary price = 100 - P
      - Walk each ladder's opposite-side bids at price >= complement
      - Sum their sizes

    Each ladder represents the opposing outcome (e.g. 'Under'), whose
    bids are people buying the opposite side = people selling the live side.
    """
    complement = 100.0 - side_price_cents
    total = 0.0
    for ladder in ladders:
        for level in ladder:
            if level.side == "bid" and level.price_cents >= complement:
                total += level.size_usd
    return total


def fetch_balance(client) -> float:
    """Fetch available balance from a venue client."""
    bal = client.fetch_balance()
    if isinstance(bal, dict):
        return float(bal.get("available", 0))
    return float(getattr(bal, "available", 0))


# --- EXECUTION (Phase 7) — disabled by default behind a hard gate -----------
EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED") == "1"

def place_order(client, *, market_id, outcome_id, side: Literal["buy", "sell"],
                amount: float, price: float, order_type="limit"):
    """Real order placement. GATED: only fires past GATE B (see CLAUDE.md).
    Kalshi writes require self-host (auto_start_server=True)."""
    if not EXECUTION_ENABLED:
        raise RuntimeError(
            "Execution is gated. Do not place real orders before GATE B "
            "(positive CLV on real fills). Set EXECUTION_ENABLED=1 only when ready.")
    return client.create_order(market_id=market_id, outcome_id=outcome_id,
                               side=side, order_type=order_type, amount=amount, price=price)


# ----------------------------------------------------------------------------
# Offline selftest — mock OrderBook, no creds, no network, no server.
# ----------------------------------------------------------------------------
def run_selftest():
    print("pmxt_execution selftest (offline, mocked)\n" + "-" * 44)

    class _L:
        def __init__(self, p, s): self.price, self.size = p, s
    class _OB:
        asks = [_L(0.55, 200), _L(0.57, 150)]
        bids = [_L(0.53, 300), _L(0.51, 80)]

    ladder = ladder_from_order_book("kalshi", _OB())
    asks = [l for l in ladder if l.side == "ask"]
    bids = [l for l in ladder if l.side == "bid"]
    assert asks[0].price_cents == 55.0 and asks[0].size_usd == 200, asks
    assert bids[0].price_cents == 53.0, bids
    print("  asks (you buy):", [(l.price_cents, l.size_usd) for l in asks])
    print("  bids (counterparty / PM-side conviction):", [(l.price_cents, l.size_usd) for l in bids])

    # ---- utility function tests ----
    best_ask = best_ask_cents(ladder)
    assert best_ask == 55.0, f"expected 55, got {best_ask}"
    print(f"  best_ask_cents: {best_ask}")

    best_bid = best_bid_cents(ladder)
    assert best_bid == 53.0, f"expected 53, got {best_bid}"
    print(f"  best_bid_cents: {best_bid}")

    depth = executable_depth(ladder, 60.0)
    assert depth == 350.0, f"expected 350, got {depth}"
    print(f"  executable_depth (below 60¢): ${depth}")

    # merged_asks — combine two venue ladders
    class _OB2:
        asks = [_L(0.54, 100)]
        bids = []
    ladder2 = ladder_from_order_book("polymarket", _OB2())
    merged = merged_asks([ladder, ladder2])
    assert len(merged) == 3
    assert merged[0].price_cents == 54.0  # polymarket cheapest
    assert merged[1].price_cents == 55.0  # then kalshi
    assert merged[2].price_cents == 57.0
    print(f"  merged_asks (kalshi + polymarket): {[(l.venue, l.price_cents, l.size_usd) for l in merged]}")

    # execution must stay gated
    gated = False
    try:
        place_order(None, market_id="m", outcome_id="o", side="buy", amount=10, price=0.55)
    except RuntimeError:
        gated = True
    assert gated, "execution should be gated by default!"
    print("  execution correctly GATED (EXECUTION_ENABLED != 1).")

    try:
        import pmxt  # noqa
        print("  pmxt import OK — venues:",
              [v for v in ("Kalshi", "Polymarket", "PolymarketUS") if hasattr(pmxt, v)])
    except Exception:
        print("  (pmxt not installed here — wrapper is import-light by design.)")

    print("\n  PASS: ladder normalization + cents + utility functions + execution gate OK.")

if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        print("Use --selftest for offline check, or import get_ladder / make_venue (needs venue creds).")
