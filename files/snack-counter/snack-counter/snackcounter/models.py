"""Domain types. Money is always Decimal, never float."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional


class Office(str, Enum):
    OSLO = "OSLO"
    BENGALURU = "BENGALURU"


class Presence(str, Enum):
    IN_OFFICE = "IN_OFFICE"
    REMOTE = "REMOTE"
    AWAY = "AWAY"  # holiday / sick


class State(str, Enum):
    OPEN = "OPEN"                          # just reported, not yet assigned
    OFFERED = "OFFERED"                    # asked ONE volunteer, offer has a TTL
    COMMITTED = "COMMITTED"                # volunteer said yes, purchase deadline running
    ESCALATED = "ESCALATED"                # nobody available / budget exhausted -> office admin
    PENDING_APPROVAL = "PENDING_APPROVAL"  # receipt above the cap, admin must approve
    CLAIM_PENDING = "CLAIM_PENDING"        # receipt accepted, HR call not (yet) successful
    CLAIM_SUBMITTED = "CLAIM_SUBMITTED"    # HR has the claim, waiting for approval
    REIMBURSED = "REIMBURSED"              # terminal, happy path
    HR_REJECTED = "HR_REJECTED"            # terminal, needs a human
    REJECTED = "REJECTED"                  # admin rejected the receipt, terminal


# The box is still empty / unresolved in these states -> at most ONE per office.
ACTIVE_STATES = frozenset({State.OPEN, State.OFFERED, State.COMMITTED, State.ESCALATED})
# Counts against the monthly budget.
COUNTED_STATES = frozenset({State.CLAIM_PENDING, State.CLAIM_SUBMITTED, State.REIMBURSED})
# Volunteer is out of pocket.
UNREIMBURSED_STATES = frozenset({State.PENDING_APPROVAL, State.CLAIM_PENDING, State.CLAIM_SUBMITTED})


@dataclass(frozen=True)
class OfficeConfig:
    currency: str
    monthly_budget: Decimal
    min_purchase: Decimal   # below this remaining budget a refill is pointless
    max_purchase: Decimal   # per-refill cap (top of the "typical purchase" range)


OFFICES = {
    Office.OSLO: OfficeConfig("NOK", Decimal(6000), Decimal(300), Decimal(1200)),
    Office.BENGALURU: OfficeConfig("INR", Decimal(15000), Decimal(500), Decimal(3000)),
}


@dataclass(frozen=True)
class Policy:
    offer_ttl: timedelta = timedelta(hours=2)        # time to answer an offer
    commit_ttl: timedelta = timedelta(hours=24)      # time to buy after committing
    max_attempts: int = 4                            # volunteers tried before escalating
    fairness_window: timedelta = timedelta(days=90)  # "how many refills lately"
    hr_chase_after: timedelta = timedelta(days=10)   # HR SLA is 1-10 days


@dataclass
class Volunteer:
    id: str
    name: str
    home_office: Office
    payroll_currency: str
    volunteer_for: set = field(default_factory=set)  # offices they are willing to refill
    location: Optional[Office] = None                # where they physically are (visitors differ from home)
    presence: Presence = Presence.REMOTE

    def __post_init__(self):
        if self.location is None:
            self.location = self.home_office
        if not self.volunteer_for:
            self.volunteer_for = {self.home_office}

    def eligible(self, office: Office) -> bool:
        return (self.presence is Presence.IN_OFFICE
                and self.location == office
                and office in self.volunteer_for)


@dataclass
class Refill:
    id: str
    office: Office
    reported_by: str
    created_at: datetime
    state: State = State.OPEN
    assignee: Optional[str] = None
    offer_expires: Optional[datetime] = None
    deadline: Optional[datetime] = None
    reserved_cap: Optional[Decimal] = None
    budget_override: bool = False
    escalation_reason: Optional[str] = None
    tried: list = field(default_factory=list)       # volunteers who timed out / declined
    reminded: bool = False
    chased: bool = False
    # filled when the receipt arrives
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    items: list = field(default_factory=list)       # optional [(name, qty, cost)]
    receipt_image: Optional[str] = None
    purchased_at: Optional[datetime] = None
    claim_id: Optional[str] = None
    claim_submitted_at: Optional[datetime] = None
    history: list = field(default_factory=list)     # audit trail [(time, text)]


class SnackError(Exception): pass
class NotAvailable(SnackError): pass     # someone else got it / wrong state
class NotAuthorized(SnackError): pass
class BudgetExhausted(SnackError): pass
class InvalidReceipt(SnackError): pass
