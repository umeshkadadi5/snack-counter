"""Who buys? Fair, available, and not already out of pocket."""
from datetime import datetime
from decimal import Decimal

from .models import UNREIMBURSED_STATES, State


def _stats(vid, refills, now, policy, office_cfg, rates):
    bought = [r for r in refills
              if r.assignee == vid and r.purchased_at and r.state != State.REJECTED]
    recent = [r for r in bought if now - r.purchased_at <= policy.fairness_window]
    last = max((r.purchased_at for r in bought), default=datetime.min)
    pending = [r for r in bought if r.state in UNREIMBURSED_STATES]
    outstanding = sum((rates.convert(r.amount, r.currency, office_cfg.currency) for r in pending),
                      Decimal(0))
    oldest = max((now - r.purchased_at for r in pending), default=None)
    return len(recent), last, outstanding, oldest


def choose_volunteer(candidates, refills, now, policy, office_cfg, rates):
    """candidates are already filtered for presence/location/not-yet-tried.

    Hard-ish rule: skip anyone whose unreimbursed money >= one max purchase, or whose
    oldest claim is overdue (solves "a few people fund everything and wait weeks").
    Soft fallback: if that excludes everybody, still pick the least-exposed person --
    an empty box is worse than a slightly unfair pick.
    Order: fewest refills in the window, then longest since last refill, then id."""
    if not candidates:
        return None
    scored = []
    for v in candidates:
        n, last, out, oldest = _stats(v.id, refills, now, policy, office_cfg, rates)
        overexposed = out >= office_cfg.max_purchase or (oldest is not None and oldest > policy.hr_chase_after)
        scored.append((overexposed, n, last, v.id, out, v))
    healthy = [s for s in scored if not s[0]]
    if healthy:
        return min(healthy, key=lambda s: (s[1], s[2], s[3]))[5]
    return min(scored, key=lambda s: (s[4], s[1], s[2], s[3]))[5]
