"""Refill lifecycle orchestration.

OPEN -> OFFERED -> COMMITTED -> (receipt) -> CLAIM_PENDING -> CLAIM_SUBMITTED -> REIMBURSED
            |           |            \\-> PENDING_APPROVAL (over cap) -> admin
            |           \\-> deadline missed -> next volunteer
            \\-> TTL expired / declined -> next volunteer
        any assign failure -> ESCALATED (admin told once; re-tried every tick)

All mutating methods take one lock. In production the same guarantee comes from a
DB: a partial unique index (office) WHERE state in ACTIVE, and conditional UPDATEs
("... WHERE id=? AND state='OFFERED' AND assignee=?") -- see DESIGN.md."""
import itertools
import threading
from collections import defaultdict
from decimal import Decimal

from . import budget
from .fakes import HRUnavailable
from .models import (ACTIVE_STATES, COUNTED_STATES, OFFICES, BudgetExhausted, InvalidReceipt,
                     NotAuthorized, NotAvailable, Policy, Presence, Refill, State, Volunteer)
from .selection import choose_volunteer


class SnackService:
    def __init__(self, chat, hr, clock, rates, admins, policy=None, configs=None):
        self.chat, self.hr, self.clock, self.rates = chat, hr, clock, rates
        self.admins = admins                      # {Office: admin_user_id}
        self.policy = policy or Policy()
        self.configs = configs or OFFICES
        self.volunteers = {}
        self.refills = {}
        self._seq = itertools.count(1)
        self._lock = threading.RLock()

    # ---------- people ----------
    def register_volunteer(self, v: Volunteer):
        with self._lock:
            self.volunteers[v.id] = v

    def set_presence(self, user_id, presence: Presence, location=None):
        """Fed by a chat command (/here, /remote, /away) or a calendar sync."""
        with self._lock:
            v = self.volunteers[user_id]
            v.presence = presence
            if location is not None:
                v.location = location

    # ---------- lifecycle ----------
    def report_empty(self, office, reporter_id) -> Refill:
        """Anyone can press 'box is empty'. Idempotent per office: if a refill is
        already in flight, return it instead of starting a second one."""
        with self._lock:
            existing = self._active(office)
            if existing:
                self._log(existing, f"duplicate empty-report by {reporter_id} ignored")
                return existing
            r = Refill(id=f"R{next(self._seq)}", office=office, reported_by=reporter_id,
                       created_at=self.clock.now())
            self.refills[r.id] = r
            self._log(r, f"reported empty by {reporter_id}")
            self._assign(r)
            return r

    def commit(self, refill_id, person_id) -> Refill:
        """Compare-and-set: only the person the refill is currently offered to --
        or, if it is ESCALATED for lack of volunteers, any eligible volunteer
        (self-service) -- can take it, and only once."""
        with self._lock:
            r = self.refills[refill_id]
            v = self.volunteers[person_id]
            offered_to_me = r.state is State.OFFERED and r.assignee == person_id
            self_claim = (r.state is State.ESCALATED
                          and r.escalation_reason in ("NO_VOLUNTEER", "MAX_ATTEMPTS")
                          and v.eligible(r.office))
            if not (offered_to_me or self_claim):
                raise NotAvailable(f"{refill_id} is {r.state.value}; not available to {person_id}")
            cap = self._cap(r)  # may raise BudgetExhausted (e.g. month rolled over)
            r.state, r.assignee = State.COMMITTED, person_id
            r.offer_expires = None
            r.deadline = self.clock.now() + self.policy.commit_ttl
            r.reserved_cap, r.escalation_reason, r.reminded = cap, None, False
            cur = self.configs[r.office].currency
            self._log(r, f"{person_id} committed (cap {cap} {cur})")
            self.chat.send(person_id, f"Thanks! Buy snacks for up to {cap} {cur} and upload the receipt "
                                      f"before {r.deadline:%Y-%m-%d %H:%M}.")
            return r

    def release(self, refill_id, person_id) -> Refill:
        """Decline an offer, or back out after committing ('I can't make it')."""
        with self._lock:
            r = self.refills[refill_id]
            if r.state not in (State.OFFERED, State.COMMITTED) or r.assignee != person_id:
                raise NotAvailable(f"{person_id} does not hold {refill_id}")
            r.tried.append(person_id)
            self._log(r, f"{person_id} released")
            self._assign(r)
            return r

    def submit_receipt(self, refill_id, person_id, amount, currency, receipt_image, items=None) -> Refill:
        with self._lock:
            r = self.refills[refill_id]
            if r.state is not State.COMMITTED or r.assignee != person_id:
                raise NotAvailable(f"{person_id} is not the committed buyer of {refill_id} "
                                   f"(state {r.state.value})")
            cfg = self.configs[r.office]
            amount = Decimal(amount)
            if amount <= 0:
                raise InvalidReceipt("amount must be positive")
            if currency != cfg.currency:
                raise InvalidReceipt(f"{r.office.value} receipts are in {cfg.currency}, got {currency}")
            r.amount, r.currency, r.receipt_image = amount, currency, receipt_image
            r.items, r.purchased_at = list(items or []), self.clock.now()
            if amount > r.reserved_cap:
                r.state = State.PENDING_APPROVAL
                self._log(r, f"receipt {amount} exceeds cap {r.reserved_cap}; needs approval")
                self.chat.send(self.admins[r.office], f"{r.id}: {person_id} spent {amount} {currency} "
                               f"(cap {r.reserved_cap}). Approve or reject?")
                self.chat.send(person_id, "Thanks - this is above the usual cap, so your admin must approve it first.")
            else:
                self._submit_claim(r)
            return r

    def admin_approve(self, refill_id, admin_id) -> Refill:
        with self._lock:
            r = self._admin_target(refill_id, admin_id, State.PENDING_APPROVAL)
            self._log(r, f"approved by {admin_id}")
            self._submit_claim(r)
            return r

    def admin_reject(self, refill_id, admin_id) -> Refill:
        with self._lock:
            r = self._admin_target(refill_id, admin_id, State.PENDING_APPROVAL)
            r.state = State.REJECTED
            self._log(r, f"rejected by {admin_id}")
            self.chat.send(r.assignee, f"{r.id}: your receipt was rejected by {admin_id}; please talk to them.")
            return r

    def admin_override_budget(self, refill_id, admin_id) -> Refill:
        with self._lock:
            r = self.refills[refill_id]
            self._require_admin(r.office, admin_id)
            r.budget_override = True
            self._log(r, f"budget override by {admin_id}")
            if r.state is State.ESCALATED:
                self._assign(r)
            return r

    def admin_retry(self, refill_id, admin_id) -> Refill:
        """Admin asks the system to start over with everyone (clears 'tried')."""
        with self._lock:
            r = self.refills[refill_id]
            self._require_admin(r.office, admin_id)
            r.tried.clear()
            if r.state is State.ESCALATED:
                self._assign(r)
            return r

    def tick(self):
        """Run every few minutes (cron/worker). All time-based rules live here, and it is
        idempotent, so running it twice or late is harmless."""
        with self._lock:
            now = self.clock.now()
            for r in list(self.refills.values()):
                if r.state is State.OFFERED and now >= r.offer_expires:
                    self._log(r, f"offer to {r.assignee} expired")
                    self.chat.send(r.assignee, f"{r.id}: offer expired, passing it on.")
                    r.tried.append(r.assignee)
                    self._assign(r)
                elif r.state is State.COMMITTED:
                    if now >= r.deadline:
                        self._log(r, f"{r.assignee} missed the deadline")
                        self.chat.send(r.assignee, f"{r.id}: deadline passed, handing the refill to someone else. "
                                                   f"Don't buy - it may be done already.")
                        r.tried.append(r.assignee)
                        self._assign(r)
                    elif not r.reminded and now >= r.deadline - self.policy.commit_ttl / 2:
                        r.reminded = True
                        self.chat.send(r.assignee, f"{r.id}: reminder - refill due {r.deadline:%Y-%m-%d %H:%M}. "
                                                   f"Can't make it? Release it.")
                elif r.state is State.ESCALATED and r.escalation_reason in ("NO_VOLUNTEER", "BUDGET"):
                    self._assign(r)  # someone may have arrived / a new month may have started
                elif r.state is State.CLAIM_PENDING:
                    self._submit_claim(r)
                elif r.state is State.CLAIM_SUBMITTED:
                    self._sync_claim(r, now)

    # ---------- reporting ----------
    def monthly_report(self, year, month, currency="NOK"):
        with self._lock:
            offices, total = {}, Decimal(0)
            item_spend = defaultdict(lambda: [0, Decimal(0)])
            for office, cfg in self.configs.items():
                rs = [r for r in self.refills.values()
                      if r.office == office and r.state in COUNTED_STATES
                      and r.purchased_at.year == year and r.purchased_at.month == month]
                spent = sum((r.amount for r in rs), Decimal(0))
                converted = self.rates.convert(spent, cfg.currency, currency)
                total += converted
                offices[office.value] = dict(currency=cfg.currency, budget=cfg.monthly_budget, spent=spent,
                                             remaining=cfg.monthly_budget - spent,
                                             utilisation=(spent / cfg.monthly_budget).quantize(Decimal("0.0001")),
                                             refills=len(rs), spent_in_reporting_currency=converted)
                for r in rs:
                    for name, qty, cost in r.items:
                        item_spend[name][0] += qty
                        item_spend[name][1] += self.rates.convert(cost, cfg.currency, currency)
            top = sorted(((n, q, c) for n, (q, c) in item_spend.items()), key=lambda t: -t[2])
            return dict(month=f"{year}-{month:02d}", reporting_currency=currency, offices=offices,
                        total=total, top_items=top)

    # ---------- internals ----------
    def _active(self, office):
        return next((r for r in self.refills.values() if r.office == office and r.state in ACTIVE_STATES), None)

    def _log(self, r, text):
        r.history.append((self.clock.now(), text))

    def _require_admin(self, office, admin_id):
        if self.admins.get(office) != admin_id:
            raise NotAuthorized(f"{admin_id} is not the admin of {office.value}")

    def _admin_target(self, refill_id, admin_id, expected):
        r = self.refills[refill_id]
        self._require_admin(r.office, admin_id)
        if r.state is not expected:
            raise NotAvailable(f"{refill_id} is {r.state.value}, expected {expected.value}")
        return r

    def _cap(self, r) -> Decimal:
        """Max the buyer may spend. Raises if the month's budget cannot fund a refill."""
        cfg, now = self.configs[r.office], self.clock.now()
        if r.budget_override:
            return cfg.max_purchase
        rem = budget.remaining(cfg, self.refills.values(), r.office, now.year, now.month)
        if rem < cfg.min_purchase:
            raise BudgetExhausted(f"{r.office.value}: only {rem} {cfg.currency} left this month")
        return min(cfg.max_purchase, rem)

    def _assign(self, r):
        """(Re)offer the refill to the best volunteer, or escalate."""
        r.assignee = r.offer_expires = r.deadline = r.reserved_cap = None
        now, cfg = self.clock.now(), self.configs[r.office]
        try:
            self._cap(r)
        except BudgetExhausted:
            return self._escalate(r, "BUDGET", "monthly budget is (nearly) used up - approve an override?")
        if len(r.tried) >= self.policy.max_attempts:
            return self._escalate(r, "MAX_ATTEMPTS", f"{len(r.tried)} volunteers did not deliver")
        cands = [v for v in self.volunteers.values() if v.eligible(r.office) and v.id not in r.tried]
        pick = choose_volunteer(cands, list(self.refills.values()), now, self.policy, cfg, self.rates)
        if pick is None:
            return self._escalate(r, "NO_VOLUNTEER", "no volunteer is in the office right now")
        r.state, r.assignee = State.OFFERED, pick.id
        r.offer_expires, r.escalation_reason = now + self.policy.offer_ttl, None
        self._log(r, f"offered to {pick.id}")
        self.chat.send(pick.id, f"The {r.office.value} snack box is empty. Can you refill it? "
                                f"Up to {cfg.max_purchase} {cfg.currency}. [Yes] [No] "
                                f"(offer expires {r.offer_expires:%H:%M})")

    def _escalate(self, r, reason, text):
        r.state = State.ESCALATED
        if r.escalation_reason != reason:  # tell the admin once per reason, not every tick
            self._log(r, f"escalated: {reason}")
            self.chat.send(self.admins[r.office], f"{r.id} ({r.office.value}) needs attention: {text}")
        r.escalation_reason = reason

    def _submit_claim(self, r):
        r.state = State.CLAIM_PENDING  # counted against budget from here on
        try:
            r.claim_id = self.hr.create_claim(
                idempotency_key=r.id, amount=r.amount, currency=r.currency,
                description=f"Snack counter refill {r.office.value} {r.purchased_at:%Y-%m-%d} ({r.id})",
                receipt_image=r.receipt_image, employee_id=r.assignee)
        except HRUnavailable:
            self._log(r, "HR unavailable; will retry")
            return
        r.state, r.claim_submitted_at = State.CLAIM_SUBMITTED, self.clock.now()
        self._log(r, f"claim {r.claim_id} submitted to HR")
        self.chat.send(r.assignee, f"Thanks for refilling! Claim {r.claim_id} for {r.amount} {r.currency} is with HR.")

    def _sync_claim(self, r, now):
        info = self.hr.get_claim(r.claim_id)
        if info["status"] == "APPROVED":
            r.state = State.REIMBURSED
            self._log(r, "reimbursed")
            self.chat.send(r.assignee, f"You've been reimbursed {info['reimbursed_amount'].quantize(Decimal('0.01'))} "
                                       f"{info['reimbursed_currency']} for {r.id}.")
        elif info["status"] == "REJECTED":
            r.state = State.HR_REJECTED
            self._log(r, "HR rejected the claim")
            self.chat.send(self.admins[r.office], f"{r.id}: HR rejected claim {r.claim_id} - needs a human.")
            self.chat.send(r.assignee, f"HR rejected claim {r.claim_id}; {self.admins[r.office]} will follow up.")
        elif not r.chased and now - r.claim_submitted_at > self.policy.hr_chase_after:
            r.chased = True
            self._log(r, "claim overdue at HR; admin chased")
            self.chat.send(self.admins[r.office], f"{r.id}: claim {r.claim_id} pending > "
                                                  f"{self.policy.hr_chase_after.days} days at HR - please chase.")
