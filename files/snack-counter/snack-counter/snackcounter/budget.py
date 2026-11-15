"""Budget check. Pure functions over the refill list -> trivially testable.

Budget is per office, in the office's own currency, attributed to the month the
receipt arrived. Because only ONE refill per office can be active, there are no
concurrent reservations to track: remaining budget = budget - counted spend."""
from decimal import Decimal

from .models import COUNTED_STATES


def spent(refills, office, year, month) -> Decimal:
    return sum((r.amount for r in refills
                if r.office == office and r.state in COUNTED_STATES
                and r.purchased_at.year == year and r.purchased_at.month == month),
               Decimal(0))


def remaining(cfg, refills, office, year, month) -> Decimal:
    return cfg.monthly_budget - spent(refills, office, year, month)
