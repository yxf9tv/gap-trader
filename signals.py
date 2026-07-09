"""
signals.py — M2: Signal layer (break detection + conviction).

After M1 computes the devigged sharp fair, this module detects when a book
prices a side below that fair (the "break"), computes conviction liquidity,
and emits structured Alert objects.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import time
from typing import Optional

from fair_value import FairLine


@dataclass
class SignalConfig:
    min_edge_cents: float = 1.0
    sharp_books: set[str] = field(default_factory=lambda: {"pinnacle"})
    exchange_books: set[str] = field(default_factory=lambda: {"novig", "prophetx"})


@dataclass
class Alert:
    market: str = ""
    live_side: str = ""
    fair_cents: float = 0.0
    signal_book: str = ""
    signal_price_cents: float = 0.0
    signal_edge_cents: float = 0.0     # fair - signal_book price (break magnitude)
    best_pm: dict = field(default_factory=dict)  # venue -> best_ask_cents
    edge_cents: float | None = None     # NET edge after fees + slippage (or None)
    gross_edge_cents: float | None = None  # edge before fees/slippage
    conviction_liquidity: float = 0.0
    executable_depth: float = 0.0
    kelly_pct: float | None = None      # depth-capped Kelly fraction (execution layer)
    max_stake: float | None = None      # dollar cap = min(kelly_stake, executable_depth)
    execution_mode: str | None = None   # "take" | "post" | None (shadow)
    timestamp: float = 0.0
    traded: str = "shadow"              # "real" | "gap" | "shadow"
    gap_side_label: str = ""            # "Yes" or "No" for gap trades
    # CLV — backfilled after event close
    closing_sharp_fair_cents: float | None = None
    clv_cents: float | None = None      # closing_sharp_fair - entry_price; positive = beat close


def detect_break(
    fair_line: FairLine,
    quotes_cents: dict[str, dict[str, float]],
    config: Optional[SignalConfig] = None,
) -> list[Alert]:
    """Scan all books for any pricing a side below the devigged sharp fair.

    Returns alerts sorted by edge (largest first). Only returns alerts with
    edge >= config.min_edge_cents. Excludes sharp/exchange books from break
    detection — they define the fair line, they don't break from it.
    """
    if config is None:
        config = SignalConfig()

    signal_sources = config.sharp_books | config.exchange_books
    alerts: list[Alert] = []
    now = time.time()

    for side, fair_cents in fair_line.per_side.items():
        if side not in quotes_cents:
            continue

        for book, price_cents in quotes_cents[side].items():
            if book in signal_sources:
                continue
            edge = fair_cents - price_cents
            if edge >= config.min_edge_cents:
                alerts.append(Alert(
                    live_side=side,
                    fair_cents=round(fair_cents, 4),
                    signal_book=book,
                    signal_price_cents=round(price_cents, 4),
                    signal_edge_cents=round(edge, 4),
                    timestamp=now,
                ))

    alerts.sort(key=lambda a: a.signal_edge_cents, reverse=True)
    return alerts


def compute_conviction(
    ladder_levels: list[dict],
    side: str,
    price_cents: float,
) -> float:
    """Sum opposite-side resting liquidity at or beyond the price.

    For the live side (e.g. 'Over') at price P:
      - Walk the opposite side bids/asks at price >= 100-P
      - Sum their sizes in dollars

    Returns 0 if no depth data is available (M3+ fills this with real PMXT data).
    """
    if not ladder_levels:
        return 0.0

    complement = 100.0 - price_cents
    total = 0.0

    for level in ladder_levels:
        if level.get("side") in ("bid", "buy") and level.get("price_cents", 0) >= complement:
            total += level.get("size_usd", 0)

    return total
