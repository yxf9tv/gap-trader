"""
fair_value.py — M1: Fair-Value Service + Anchor-Trust Gate

Takes sharp-book odds for a market, outputs the devigged consensus fair
probability per side (in cents) plus an anchor-trust gate decision.

Builds on smart_money_engine.py devig primitives. No order books, no
prediction markets, no execution, no network.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from smart_money_engine import devig_power, devig_proportional
import utils


@dataclass
class FairValueConfig:
    sport: str = "unknown"
    gate_floor_2way: float = 0.51
    gate_floor_3way: float = 0.0
    anchor_mode: str = "conservative"
    allow_single_sharp: bool = False
    odds_format: str = "american"
    devig_method: str = "proportional"
    sportsbooks: set[str] = field(default_factory=lambda: {"pinnacle"})
    exchanges: set[str] = field(default_factory=lambda: {"novig", "prophetx"})


@dataclass
class FairLine:
    per_side: dict[str, float]
    gate_pass: bool
    reason: str
    sharps_used: list[str]
    favorite: Optional[str] = None


def _to_cents(price: float, fmt: str) -> float:
    if fmt == "cents":
        return price
    if fmt == "decimal":
        return utils.decimal_to_cents(price)
    return utils.american_to_cents(price)


def _is_3way(selections: list[str]) -> bool:
    return len(selections) >= 3


def compute_fair_line(
    quotes: dict[str, dict[str, float]],
    config: Optional[FairValueConfig] = None,
) -> FairLine:
    if config is None:
        config = FairValueConfig()

    selections = list(quotes.keys())
    if not selections:
        return FairLine(
            per_side={}, gate_pass=False, reason="no selections", sharps_used=[]
        )

    all_books: set[str] = set()
    for sel in selections:
        all_books.update(quotes[sel].keys())
    if not all_books:
        return FairLine(
            per_side={s: 0.0 for s in selections},
            gate_pass=False,
            reason="no books",
            sharps_used=[],
        )

    sharps_used = sorted(all_books)

    cents: dict[str, dict[str, float]] = {}
    for sel in selections:
        cents[sel] = {}
        for book, price in quotes[sel].items():
            cents[sel][book] = _to_cents(price, config.odds_format)

    if len(sharps_used) == 1 and not config.allow_single_sharp:
        only_book = sharps_used[0]
        per_side = {s: cents[s][only_book] for s in selections}
        return FairLine(
            per_side=per_side,
            gate_pass=False,
            reason="single-source",
            sharps_used=sharps_used,
        )

    book_fairs: dict[str, dict[str, float]] = {}
    for book in sharps_used:
        implied = []
        for sel in selections:
            if book not in cents[sel]:
                break
            implied.append(cents[sel][book] / 100.0)
        else:
            devig_fn = (
                devig_proportional
                if config.devig_method == "proportional"
                else devig_power
            )
            fair_probs = devig_fn(implied)
            book_fairs[book] = {
                s: p * 100.0 for s, p in zip(selections, fair_probs)
            }

    used_books = sorted(book_fairs.keys())
    if not used_books:
        return FairLine(
            per_side={s: 0.0 for s in selections},
            gate_pass=False,
            reason="no book covers all selections",
            sharps_used=sharps_used,
        )

    if len(used_books) < 2 and not config.allow_single_sharp:
        per_side = book_fairs[used_books[0]]
        return FairLine(
            per_side=per_side,
            gate_pass=False,
            reason="single-source",
            sharps_used=used_books,
        )

    favorites: dict[str, tuple[str, float]] = {}
    for book, probs in book_fairs.items():
        fav = max(probs, key=probs.get)
        favorites[book] = (fav, probs[fav])

    fav_set = {f[0] for f in favorites.values()}
    if len(fav_set) > 1:
        per_side = {s: min(bf[s] for bf in book_fairs.values()) for s in selections}
        return FairLine(
            per_side=per_side,
            gate_pass=False,
            reason=f"directional disagreement: {favorites}",
            sharps_used=used_books,
        )

    favorite = fav_set.pop()
    fav_prob = max(f[1] for f in favorites.values())

    three_way = _is_3way(selections)
    floor = config.gate_floor_3way if three_way else config.gate_floor_2way
    if floor > 0 and (fav_prob / 100.0) < floor:
        per_side = {s: min(bf[s] for bf in book_fairs.values()) for s in selections}
        return FairLine(
            per_side=per_side,
            gate_pass=False,
            reason=f"favorite {favorite} at {fav_prob:.1f}¢ below floor {floor*100:.0f}%",
            sharps_used=used_books,
            favorite=favorite,
        )

    if config.anchor_mode == "median":
        per_side = {}
        for s in selections:
            vals = sorted(bf[s] for bf in book_fairs.values())
            n = len(vals)
            per_side[s] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    else:
        per_side = {s: min(bf[s] for bf in book_fairs.values()) for s in selections}

    return FairLine(
        per_side=per_side,
        gate_pass=True,
        reason="ok",
        sharps_used=used_books,
        favorite=favorite,
    )
