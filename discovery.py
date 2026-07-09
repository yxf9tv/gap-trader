"""discovery.py — PM venue market discovery via PMXT self-custody.

Fetches all available Kalshi game markets and builds
team_name → outcome_id mappings for registry.json.
"""

from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Optional

from matching import load_registry, save_registry

KALSHI_EVENT_PREFIXES = {
    "KXMLBGAME": "baseball_mlb",
    "KXNFLGAME": "football_nfl",
    "KXNCAAFGAME": "football_ncaaf",
}

SPORT_ALIAS = {
    "baseball_mlb": "MLB",
    "football_nfl": "NFL",
    "football_ncaaf": "NCAAF",
}

# ---------------------------------------------------------------------------
# MLB: ParlayAPI team names → Kalshi outcome labels
# ---------------------------------------------------------------------------

MLB_TEAM_TO_KALSHI_LABEL: dict[str, str] = {
    "Atlanta Braves":       "Atlanta",
    "Arizona Diamondbacks": "Arizona",
    "Baltimore Orioles":    "Baltimore",
    "Boston Red Sox":       "Boston",
    "Chicago Cubs":         "Chicago C",
    "Chicago White Sox":    "Chicago WS",
    "Cincinnati Reds":      "Cincinnati",
    "Cleveland Guardians":  "Cleveland",
    "Colorado Rockies":     "Colorado",
    "Detroit Tigers":       "Detroit",
    "Houston Astros":       "Houston",
    "Kansas City Royals":   "Kansas City",
    "Los Angeles Angels":   "Los Angeles A",
    "Los Angeles Dodgers":  "Los Angeles D",
    "Miami Marlins":        "Miami",
    "Milwaukee Brewers":    "Milwaukee",
    "Minnesota Twins":      "Minnesota",
    "New York Mets":        "New York M",
    "New York Yankees":     "New York Y",
    "Oakland Athletics":    "A's",
    "Philadelphia Phillies": "Philadelphia",
    "Pittsburgh Pirates":   "Pittsburgh",
    "San Diego Padres":     "San Diego",
    "San Francisco Giants": "San Francisco",
    "Seattle Mariners":     "Seattle",
    "St. Louis Cardinals":  "St. Louis",
    "Tampa Bay Rays":       "Tampa Bay",
    "Texas Rangers":        "Texas",
    "Toronto Blue Jays":    "Toronto",
    "Washington Nationals": "Washington",
}

# Reverse: partial Kalshi label → set of ParlayAPI team names
_KALSHI_TO_TEAM: dict[str, list[str]] | None = None

def team_to_kalshi_label(team_name: str) -> str | None:
    """Map a ParlayAPI team name to the Kalshi outcome label."""
    direct = MLB_TEAM_TO_KALSHI_LABEL.get(team_name)
    if direct:
        return direct
    team_lower = team_name.lower()
    for parl, kalshi in MLB_TEAM_TO_KALSHI_LABEL.items():
        if team_lower in parl.lower() or parl.lower() in team_lower:
            return kalshi
    return None

def kalshi_label_to_teams(label: str) -> list[str]:
    """Map a Kalshi outcome label back to all matching ParlayAPI team names."""
    global _KALSHI_TO_TEAM
    if _KALSHI_TO_TEAM is None:
        _KALSHI_TO_TEAM = {}
        for parl, kalshi in MLB_TEAM_TO_KALSHI_LABEL.items():
            _KALSHI_TO_TEAM.setdefault(kalshi, []).append(parl)
    return _KALSHI_TO_TEAM.get(label, [])


# ---------------------------------------------------------------------------
# Kalshi market discovery via PMXT self-custody
# ---------------------------------------------------------------------------

def discover_kalshi_markets(
    registry_path: str,
    sport_prefixes: Optional[list[str]] = None,
    venue: str = "kalshi",
) -> int:
    """Fetch game markets from Kalshi via PMXT self-custody and populate registry.

    Returns count of mappings added.

    sport_prefixes: event ID prefixes to fetch (default = MLB only)
    """
    if sport_prefixes is None:
        sport_prefixes = ["KXMLBGAME"]

    import pmxt

    api_key = os.environ.get("KALSHI_API_KEY")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    key_fallback = os.environ.get("KALSHI_PRIVATE_KEY")

    if not api_key or not (key_path or key_fallback):
        print("!! KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH required for discovery")
        return 0

    if key_path:
        private_key = open(os.path.expanduser(key_path) if "~" in key_path else key_path).read()
    else:
        private_key = key_fallback

    registry = load_registry(registry_path)
    added = 0

    k = pmxt.Kalshi(
        api_key=api_key,
        private_key=private_key,
        auto_start_server=True,
    )

    events = k.fetch_events()
    print(f"  Kalshi events loaded: {len(events)}")

    # Fetch order books in batch to get prices (optional, just for logging)
    for e in events:
        eid = getattr(e, "id", "")
        if not any(eid.startswith(p) for p in sport_prefixes):
            continue

        title = getattr(e, "title", "")
        for m in getattr(e, "markets", []):
            mtitle = getattr(m, "title", "")
            # Only process game-winner markets
            if "winner" not in mtitle.lower():
                continue
            outcomes = getattr(m, "outcomes", [])
            if len(outcomes) != 2:
                continue

            yes = outcomes[0]
            no = outcomes[1] if len(outcomes) > 1 else None
            label = getattr(yes, "label", "").strip()
            oid = getattr(yes, "outcome_id", "") or ""
            opp_oid = getattr(no, "outcome_id", "") if no else ""

            if not oid or not label:
                continue

            # Map Kalshi label → ParlayAPI team names → registry keys
            parl_teams = kalshi_label_to_teams(label)
            sport_key = "kalshi_mlb"
            for parl in parl_teams:
                key = f"{sport_key}|{parl}"
                is_new = key not in registry.get("mappings", {})
                registry.setdefault("mappings", {})[key] = {
                    venue: {
                        "outcome_id": oid,
                        "opposite_outcome_id": opp_oid if opp_oid else None,
                    }
                }
                added += 1
                print(f"    {'+' if is_new else '~'} {parl:30s} → {oid}")

    k.close()

    if added:
        save_registry(registry, registry_path)
        print(f"\n  Added {added} mappings to {registry_path}")
    else:
        print("  No new mappings (all already in registry)")

    return added


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Discover Kalshi markets and populate registry")
    p.add_argument("--registry", default=None, help="path to registry.json")
    p.add_argument("--sport", default="baseball_mlb", help="sport to discover")
    args = p.parse_args()

    registry_path = args.registry or os.path.join(
        os.path.dirname(__file__), "registry.json")

    prefix_map = {"baseball_mlb": "KXMLBGAME", "football_nfl": "KXNFLGAME"}
    prefix = prefix_map.get(args.sport, "KXMLBGAME")

    discover_kalshi_markets(registry_path, sport_prefixes=[prefix])
