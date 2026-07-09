"""
matching.py — M5: Market matching + settlement verification.

Maps a signal alert's (market_id, live_side) to per-venue PMXT outcome IDs
via a manually-curated whitelist (registry). Settlement equivalence is verified
before any outcome_id is returned.

The registry is a JSON file (registry.json) that users populate with known-good
mappings after manual verification of contract terms.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import os
from typing import Optional

from signals import Alert

DEFAULT_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "registry.json")

# Lazy import for PmxtRestClient (avoid circular dep at module level)
_PMXT_REST_CLIENT: object | None = None

def _get_pmxt_rest(api_key: str | None = None):
    from pmxt_execution import PmxtRestClient
    if api_key:
        return PmxtRestClient(api_key)
    global _PMXT_REST_CLIENT
    key = os.environ.get("PMXT_API_KEY")
    if key and _PMXT_REST_CLIENT is None:
        _PMXT_REST_CLIENT = PmxtRestClient(key)
    return _PMXT_REST_CLIENT if key else None


# ---------------------------------------------------------------------------
# Market Key — canonical market identity
# ---------------------------------------------------------------------------

@dataclass
class MarketKey:
    sport: str = ""
    event_id: str = ""
    market_type: str = ""       # "h2h" | "spread" | "total" | "player_prop"
    line: float | None = None
    player: str | None = None
    side: str = ""


# ---------------------------------------------------------------------------
# Settlement Key — hash of resolution rules
# ---------------------------------------------------------------------------

def settlement_id_from_market_type(
    market_type: str,
    line: float | None = None,
) -> str:
    """Compute a deterministic settlement identifier from market metadata.

    Two markets must have the same settlement_id to be comparable.
    This is a simplified v1 — real settlement hashing would include
    push/void rules, OT/ET inclusion, settlement source, etc.
    """
    h = hashlib.sha256()
    h.update(market_type.encode())
    if line is not None:
        h.update(str(line).encode())
    # Future: add push/void rules, OT/ET flags, settlement source
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Parse a ParlayAPI market_id into structured components
# ---------------------------------------------------------------------------

_KNOWN_MARKET_TYPES = {"h2h", "spreads", "totals"}


def parse_market_id(market_id: str) -> tuple[str, str, str, float | None, str | None]:
    """Parse market_id into (event_id, market_type, player, line, raw).

    Game lines:  "evt_lal_bos|h2h" → "evt_lal_bos", "h2h", None, None
    Spreads:     "evt_lal_bos|spreads" → "evt_lal_bos", "spread", None, None (TODO: line)
    Totals:      "evt_lal_bos|totals" → "evt_lal_bos", "total", None, None
    Props:       "ee78|Chase Burns|player_strikeouts|6.5"
                 → "ee78", "player_prop", "Chase Burns", 6.5
    """
    parts = market_id.split("|")

    # Check for prop format: event|player|market_key|line
    if len(parts) >= 4:
        event_id = parts[0]
        player = parts[1]
        market_type = "player_prop"
        try:
            line = float(parts[3])
        except (ValueError, IndexError):
            line = None
        return event_id, market_type, player, line, parts[2]

    # Game line format: event|market_key
    event_id = parts[0]
    raw = parts[1] if len(parts) > 1 else "h2h"
    market_type = raw

    # Normalize market_type
    if market_type == "spreads":
        market_type = "spread"
    elif market_type == "totals":
        market_type = "total"

    return event_id, market_type, None, None, raw


# ---------------------------------------------------------------------------
# Mapping registry — loads/stores whitelist of known venue mappings
# ---------------------------------------------------------------------------

def _default_registry() -> dict:
    return {
        "version": 1,
        "mappings": {},
    }


def load_registry(path: str = DEFAULT_REGISTRY_PATH) -> dict:
    """Load the mapping registry from a JSON file. Returns empty if missing."""
    if not os.path.exists(path):
        return _default_registry()
    with open(path) as f:
        return json.load(f)


def save_registry(registry: dict, path: str = DEFAULT_REGISTRY_PATH) -> None:
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Market Matcher — resolves alerts to per-venue outcome IDs
# ---------------------------------------------------------------------------

@dataclass
class VenueMapping:
    outcome_id: str
    opposite_outcome_id: str


@dataclass
class MatchResult:
    market_key: MarketKey
    settlement_id: str
    outcome_id: str
    opposite_outcome_id: str | None = None


class MarketMatcher:
    """Resolves signal alerts to PMXT outcome IDs.

    Three-layer resolution:
      1. Registry lookup (manually curated whitelist)
      2. PMXT REST API auto-discovery (fallback, requires PMXT_API_KEY)
      3. Kalshi team name alias table (for sports with known naming conventions)
    """

    def __init__(self, registry_path: str = DEFAULT_REGISTRY_PATH,
                 pmxt_api_key: str | None = None):
        self.registry_path = registry_path
        self._registry = load_registry(registry_path)
        self._dirty = False
        self._pmxt = _get_pmxt_rest(pmxt_api_key) if pmxt_api_key or os.environ.get("PMXT_API_KEY") else None
        # Kalshi team name alias table (lazy-loaded)
        self._kalshi_aliases: dict | None = None

    def reload(self) -> None:
        self._registry = load_registry(self.registry_path)

    def save(self) -> None:
        if self._dirty:
            save_registry(self._registry, self.registry_path)
            self._dirty = False

    # ------------------------------------------------------------------
    # Public API — used by the execution pipeline
    # ------------------------------------------------------------------

    def resolve_alert(
        self,
        alert: Alert,
        venues: list[str],
    ) -> tuple[MatchResult | None, str, Optional[str]]:
        """Resolve an Alert to venue-specific outcome IDs.

        Returns (match_result_or_None, primary_outcome_id, opposite_outcome_id_or_None).

        If no mapping exists in the registry, returns (None, placeholder_id, None)
        where placeholder_id is derived from market+side (for development).
        """
        event_id, market_type, player, line, raw = parse_market_id(alert.market)
        key = f"{alert.market}|{alert.live_side}"

        # Build MarketKey for reference
        market_key = MarketKey(
            event_id=event_id,
            market_type=market_type,
            line=line,
            player=player,
            side=alert.live_side,
        )
        sid = settlement_id_from_market_type(market_type, line)

        # Look up registry
        mapping = self._registry.get("mappings", {}).get(key)
        if mapping is not None:
            # Use the first venue's mapping (or venue-specific)
            for venue in venues:
                vm = mapping.get(venue)
                if vm:
                    return (
                        MatchResult(market_key, sid, vm["outcome_id"], vm.get("opposite_outcome_id")),
                        vm["outcome_id"],
                        vm.get("opposite_outcome_id"),
                    )
            # Fallback: if no venue-specific, use "default"
            vm = mapping.get("default")
            if vm:
                return (
                    MatchResult(market_key, sid, vm["outcome_id"], vm.get("opposite_outcome_id")),
                    vm["outcome_id"],
                    vm.get("opposite_outcome_id"),
                )

        # No mapping found — try PMXT auto-discovery (best-effort)
        if self._pmxt:
            try:
                result = self._pmxt.find_outcome_for_team(alert.live_side)
                if result:
                    oid = result["outcome_id"]
                    opp = result.get("opposite_outcome_id")
                    venue = result.get("venue", "kalshi")
                    self.add_mapping(alert.market, alert.live_side, venue, oid, opp)
                    self.save()
                    return (
                        MatchResult(market_key, sid, oid, opp),
                        oid, opp,
                    )
            except Exception:
                pass

        # No match — try sport-specific team name lookup (kalshi_mlb, etc.)
        for sport_prefix in ("kalshi_mlb", "kalshi_nfl", "kalshi_soccer"):
            team_key = f"{sport_prefix}|{alert.live_side}"
            mapping = self._registry.get("mappings", {}).get(team_key)
            if mapping is not None:
                for venue_name, vm in mapping.items():
                    oid = vm.get("outcome_id")
                    opp = vm.get("opposite_outcome_id")
                    if oid:
                        # Cache with exact key too (optional)
                        return (
                            MatchResult(market_key, sid, oid, opp),
                            oid, opp,
                        )

        # No mapping or auto-discovery — return placeholder for development
        placeholder = f"{alert.market}:{alert.live_side}"
        return None, placeholder, None

    def resolve_alerts(
        self,
        alerts: list[Alert],
        venues: list[str],
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
        """Resolve multiple alerts into outcome_map + opposite_map.

        Returns (outcome_map, opposite_map) suitable for
        ExecutionEngine.enrich_alerts().
        """
        outcome_map: dict[str, dict[str, str]] = {}
        opposite_map: dict[str, dict[str, str]] = {}

        for alert in alerts:
            result, oid, opp_oid = self.resolve_alert(alert, venues)
            market = alert.market
            side = alert.live_side

            if market not in outcome_map:
                outcome_map[market] = {}
                opposite_map[market] = {}
            outcome_map[market][side] = oid
            if opp_oid:
                opposite_map[market][side] = opp_oid

        return outcome_map, opposite_map

    # ------------------------------------------------------------------
    # Registry management — for manual whitelist curation
    # ------------------------------------------------------------------

    def add_mapping(
        self,
        market_id: str,
        side: str,
        venue: str,
        outcome_id: str,
        opposite_outcome_id: Optional[str] = None,
    ) -> None:
        """Add or update a mapping entry.

        market_id: e.g., "evt_lal_bos|h2h"
        side:      e.g., "Lakers"
        """
        key = f"{market_id}|{side}"
        if "mappings" not in self._registry:
            self._registry["mappings"] = {}
        if key not in self._registry["mappings"]:
            self._registry["mappings"][key] = {}
        self._registry["mappings"][key][venue] = {
            "outcome_id": outcome_id,
        }
        if opposite_outcome_id:
            self._registry["mappings"][key][venue]["opposite_outcome_id"] = opposite_outcome_id
        self._dirty = True

    def remove_mapping(self, market_id: str, side: str, venue: str) -> bool:
        key = f"{market_id}|{side}"
        mappings = self._registry.get("mappings", {})
        if key in mappings and venue in mappings[key]:
            del mappings[key][venue]
            if not mappings[key]:
                del mappings[key]
            self._dirty = True
            return True
        return False

    def get_mapping(self, market_id: str, side: str) -> dict | None:
        key = f"{market_id}|{side}"
        return self._registry.get("mappings", {}).get(key)
