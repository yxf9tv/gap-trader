"""Devigging, fair-value, and Kelly sizing engine (stdlib-only)."""

from __future__ import annotations
from dataclasses import dataclass, field
from collections import defaultdict, deque
from typing import Optional
import time
import math


# ---------------------------------------------------------------------------
# 1. Odds conversions
# ---------------------------------------------------------------------------

def american_to_decimal(american: float) -> float:
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_american(dec: float) -> float:
    if dec >= 2.0:
        return (dec - 1.0) * 100.0
    return -100.0 / (dec - 1.0)


def decimal_to_prob(dec: float) -> float:
    """Implied probability (still includes the vig)."""
    return 1.0 / dec


def prob_to_decimal(p: float) -> float:
    return 1.0 / p


# ---------------------------------------------------------------------------
# 2. Devigging  ->  the "no-vig" / "true" / "fair" line
# ---------------------------------------------------------------------------
# A 2-way market priced at, say, -110 / -110 has implied probs summing to ~1.048.
# That 4.8% is the "vig" / "overround" / "hold". Devigging removes it to recover
# the book's underlying probability estimate.

def devig_proportional(implied_probs: list[float]) -> list[float]:
    """Multiplicative / proportional method. Simple, fast, the industry default.
    fair_i = q_i / sum(q_j)."""
    total = sum(implied_probs)
    return [q / total for q in implied_probs]


def devig_power(implied_probs: list[float], tol: float = 1e-9) -> list[float]:
    """Power method: find k such that sum(q_i**k) == 1, then fair_i = q_i**k.
    Better models favorite-longshot bias than the proportional method.
    Solved by bisection (no scipy needed)."""
    lo, hi = 0.0001, 10.0

    def overround(k: float) -> float:
        return sum(q ** k for q in implied_probs) - 1.0

    # overround is monotincreasing in k for q in (0,1)? q**k decreases as k grows
    # for q<1, so sum decreases as k grows -> root exists between lo and hi.
    for _ in range(200):
        mid = (lo + hi) / 2.0
        val = overround(mid)
        if abs(val) < tol:
            break
        if val > 0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    return [q ** k for q in implied_probs]


# ---------------------------------------------------------------------------
# 3 & 4. EV and Kelly
# ---------------------------------------------------------------------------

def expected_value(fair_prob: float, book_decimal: float) -> float:
    """EV per 1 unit staked. Positive => +EV bet.
    EV = p*(d-1) - (1-p) = p*d - 1."""
    return fair_prob * book_decimal - 1.0


def kelly_fraction(fair_prob: float, book_decimal: float,
                   fraction: float = 0.25) -> float:
    """Fraction of bankroll to wager. `fraction` applies fractional Kelly
    (0.25 = quarter Kelly) to tame variance. Clamped at 0 (no bet if -EV)."""
    b = book_decimal - 1.0
    q = 1.0 - fair_prob
    full = (b * fair_prob - q) / b
    return max(0.0, full * fraction)


# ---------------------------------------------------------------------------
# 5. Data structures
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    """One book's price on one selection at one moment."""
    book: str
    decimal: float
    timestamp: float = field(default_factory=time.time)

    @property
    def implied(self) -> float:
        return decimal_to_prob(self.decimal)


@dataclass
class Signal:
    market_id: str
    selection: str
    fair_prob: float
    fair_decimal: float
    best_book: str
    best_decimal: float
    ev_pct: float
    kelly: float
    steam: Optional[str] = None      # "up" / "down" / None
    lagging_books: list[str] = field(default_factory=list)
    rlm: bool = False

    def __str__(self) -> str:
        parts = [
            f"{self.selection:<22} fair={self.fair_decimal:.3f} "
            f"({self.fair_prob*100:4.1f}%)  best={self.best_decimal:.3f}@{self.best_book:<10} "
            f"EV={self.ev_pct*100:+5.2f}%  kelly={self.kelly*100:4.2f}%"
        ]
        if self.steam:
            parts.append(f"  STEAM {self.steam.upper()} (lagging: {', '.join(self.lagging_books) or 'none'})")
        if self.rlm:
            parts.append("  RLM")
        return "".join(parts)


# ---------------------------------------------------------------------------
# 6. The engine
# ---------------------------------------------------------------------------

class SmartMoneyEngine:
    def __init__(self,
                 sharp_books: tuple[str, ...] = ("pinnacle", "circa"),
                 steam_window_s: float = 90.0,
                 steam_move_threshold: float = 0.015,   # 1.5% implied-prob shift
                 steam_min_books: int = 3,
                 ev_threshold: float = 0.0,
                 devig: str = "proportional"):
        self.sharp_books = sharp_books
        self.steam_window_s = steam_window_s
        self.steam_move_threshold = steam_move_threshold
        self.steam_min_books = steam_min_books
        self.ev_threshold = ev_threshold
        self.devig_fn = devig_power if devig == "power" else devig_proportional

        # history[(market, selection)][book] = deque[(ts, implied_prob)]
        self._hist: dict[tuple[str, str], dict[str, deque]] = \
            defaultdict(lambda: defaultdict(lambda: deque(maxlen=500)))

    def ingest(self, market_id: str, selection: str, quote: Quote) -> None:
        self._hist[(market_id, selection)][quote.book].append(
            (quote.timestamp, quote.implied))

    def _fair_prob(self, market_id: str, selections: dict[str, dict[str, Quote]],
                   target_selection: str) -> Optional[float]:
        """Devig the sharpest available book across the full market to get the
        fair probability of `target_selection`."""
        sel_names = list(selections.keys())
        for sharp in self.sharp_books:
            if all(sharp in selections[s] for s in sel_names):
                implied = [selections[s][sharp].implied for s in sel_names]
                fair = self.devig_fn(implied)
                return dict(zip(sel_names, fair))[target_selection]
        # Fallback: consensus devig across the median price per selection.
        implied = []
        for s in sel_names:
            qs = [q.implied for q in selections[s].values()]
            qs.sort()
            implied.append(qs[len(qs) // 2])
        fair = self.devig_fn(implied)
        return dict(zip(sel_names, fair))[target_selection]

    def _detect_steam(self, market_id: str, selection: str) -> tuple[Optional[str], list[str]]:
        """Steam = many books moved implied prob in the same direction within the
        window. Returns (direction, lagging_books) where lagging books are the ones
        that have NOT yet moved -- i.e. where soft value may still sit."""
        now = time.time()
        books = self._hist[(market_id, selection)]
        moved_up, moved_down, lagging = 0, 0, []
        for book, dq in books.items():
            window = [(t, p) for (t, p) in dq if now - t <= self.steam_window_s]
            if len(window) < 2:
                lagging.append(book)
                continue
            delta = window[-1][1] - window[0][1]
            if delta >= self.steam_move_threshold:
                moved_up += 1
            elif delta <= -self.steam_move_threshold:
                moved_down += 1
            else:
                lagging.append(book)
        if moved_up >= self.steam_min_books and moved_up > moved_down:
            return "up", lagging
        if moved_down >= self.steam_min_books and moved_down > moved_up:
            return "down", lagging
        return None, []

    def evaluate(self, market_id: str,
                 selections: dict[str, dict[str, Quote]],
                 bet_pct: Optional[dict[str, float]] = None) -> list[Signal]:
        """Score every selection in a market.

        selections: {selection_name: {book_name: Quote}}
        bet_pct:    optional {selection_name: public_ticket_fraction} for RLM.
        """
        signals: list[Signal] = []
        for selection, book_quotes in selections.items():
            fair = self._fair_prob(market_id, selections, selection)
            if fair is None or fair <= 0:
                continue
            # Best (highest) available decimal price = best payout for the bettor.
            best_book, best_q = max(book_quotes.items(), key=lambda kv: kv[1].decimal)
            ev = expected_value(fair, best_q.decimal)
            if ev < self.ev_threshold:
                continue

            steam_dir, lagging = self._detect_steam(market_id, selection)

            rlm = False
            if bet_pct is not None and selection in bet_pct:
                # RLM: public mostly on this side, yet its price drifted longer
                # (implied prob fell) -> sharp money on the other side.
                dq = next(iter(self._hist[(market_id, selection)].values()), None)
                if dq and len(dq) >= 2 and bet_pct[selection] >= 0.60:
                    if dq[-1][1] < dq[0][1]:
                        rlm = True

            signals.append(Signal(
                market_id=market_id,
                selection=selection,
                fair_prob=fair,
                fair_decimal=prob_to_decimal(fair),
                best_book=best_book,
                best_decimal=best_q.decimal,
                ev_pct=ev,
                kelly=kelly_fraction(fair, best_q.decimal),
                steam=steam_dir,
                lagging_books=lagging,
                rlm=rlm,
            ))
        signals.sort(key=lambda s: s.ev_pct, reverse=True)
        return signals



