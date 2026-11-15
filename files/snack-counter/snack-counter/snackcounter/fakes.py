"""Simulated stand-ins for the chat tool and the external HR system."""
from decimal import Decimal


class FakeChat:
    def __init__(self):
        self.sent = []  # (user_id, text)

    def send(self, user_id, text):
        self.sent.append((user_id, text))

    def to(self, user_id):
        return [t for u, t in self.sent if u == user_id]


class HRUnavailable(Exception):
    pass


class FakeHR:
    """Mimics the external HR API: create claim (amount, currency, description,
    receipt, employee id), approval later, reimbursement in payroll currency.
    Honours an idempotency key because real networks retry."""

    def __init__(self, payroll_currency_of, rates):
        self.payroll_currency_of = payroll_currency_of  # employee_id -> currency
        self.rates = rates
        self.claims = {}
        self._by_key = {}
        self.fail_next = 0

    def create_claim(self, *, idempotency_key, amount, currency, description, receipt_image, employee_id):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise HRUnavailable("HR system down")
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        cid = f"HR-{len(self.claims) + 1}"
        self.claims[cid] = dict(amount=Decimal(amount), currency=currency, description=description,
                                receipt_image=receipt_image, employee_id=employee_id, status="PENDING")
        self._by_key[idempotency_key] = cid
        return cid

    def get_claim(self, claim_id):
        c = self.claims[claim_id]
        out = dict(status=c["status"])
        if c["status"] == "APPROVED":
            pay = self.payroll_currency_of[c["employee_id"]]
            out["reimbursed_amount"] = self.rates.convert(c["amount"], c["currency"], pay)
            out["reimbursed_currency"] = pay
        return out

    # test/sim controls (HR's side)
    def approve(self, claim_id):
        self.claims[claim_id]["status"] = "APPROVED"

    def reject(self, claim_id):
        self.claims[claim_id]["status"] = "REJECTED"
