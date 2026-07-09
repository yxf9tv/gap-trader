"""
utils.py — Shared helpers for the smart-money scanner.

Canonical location for:
  - odds-to-cents conversion (canonical unit)
  - book classification (sharp / exchange / soft / pm)

Circa is deliberately excluded from the sharp book list — ParlayAPI doesn't carry it.
"""

from smart_money_engine import american_to_decimal


SHARP_BOOKS = {"pinnacle"}
EXCHANGE_BOOKS = {"novig", "prophetx"}
PM_BOOKS = {"kalshi", "polymarket", "polymarket_us"}
# everything else is treated as a soft signal source


def book_class(book: str) -> str:
    if book in SHARP_BOOKS:
        return "sharp"
    if book in EXCHANGE_BOOKS:
        return "exchange"
    if book in PM_BOOKS:
        return "pm"
    return "soft"


def american_to_cents(american: float) -> float:
    """American odds → implied probability in cents (0–100)."""
    return 100.0 / american_to_decimal(american)


def decimal_to_cents(decimal: float) -> float:
    """Decimal odds → implied probability in cents (0–100)."""
    return 100.0 / decimal
