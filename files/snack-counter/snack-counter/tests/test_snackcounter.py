import threading
import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D

from snackcounter import (BudgetExhausted, InvalidReceipt, NotAuthorized, NotAvailable, Office, Policy,
                          Presence, SnackService, State, Volunteer)
from snackcounter.fakes import FakeChat, FakeHR
from snackcounter.rates import FxRates

OSLO, BLR = Office.OSLO, Office.BENGALURU
H = timedelta(hours=1)


class Clock:
    def __init__(self, t): self.t = t
    def now(self): return self.t
    def advance(self, d): self.t += d


def make(policy=None, oslo_present=("o1", "o2", "o3"), blr_present=("b1", "b2")):
    rates = FxRates({("INR", "NOK"): D("0.125")})  # 1000 INR = 125 NOK, exact arithmetic
    vols = [Volunteer("o1", "Ola", OSLO, "NOK"), Volunteer("o2", "Kari", OSLO, "NOK"),
            Volunteer("o3", "Per", OSLO, "NOK"), Volunteer("b1", "Asha", BLR, "INR"),
            Volunteer("b2", "Ravi", BLR, "INR")]
    for v in vols:
        if v.id in oslo_present + blr_present:
            v.presence = Presence.IN_OFFICE
    clock, chat = Clock(datetime(2026, 11, 16, 9, 0)), FakeChat()
    hr = FakeHR({v.id: v.payroll_currency for v in vols}, rates)
    svc = SnackService(chat, hr, clock, rates, {OSLO: "admin_oslo", BLR: "admin_blr"}, policy)
    for v in vols:
        svc.register_volunteer(v)
    return svc, clock, chat, hr


def refill(svc, office, amount, reporter="x", items=None):
    """Happy path helper: report -> assignee commits -> uploads receipt."""
    r = svc.report_empty(office, reporter)
    who = r.assignee
    svc.commit(r.id, who)
    cur = "NOK" if office == OSLO else "INR"
    svc.submit_receipt(r.id, who, amount, cur, "img.jpg", items)
    return r


class OneRefillAtATime(unittest.TestCase):
    def test_duplicate_reports_share_one_refill(self):
        svc, *_ = make()
        a = svc.report_empty(OSLO, "o2")
        b = svc.report_empty(OSLO, "o3")
        self.assertIs(a, b)
        self.assertEqual(len(svc.refills), 1)  # nobody else is asked -> no double buying

    def test_offices_are_independent(self):
        svc, *_ = make()
        self.assertNotEqual(svc.report_empty(OSLO, "o1").id, svc.report_empty(BLR, "b1").id)

    def test_new_refill_allowed_after_previous_one_is_bought(self):
        svc, *_ = make()
        a = refill(svc, OSLO, 500)
        b = svc.report_empty(OSLO, "o1")
        self.assertNotEqual(a.id, b.id)


class CommitIsExclusive(unittest.TestCase):
    def test_only_the_offered_volunteer_can_commit(self):
        svc, *_ = make()
        r = svc.report_empty(OSLO, "x")
        other = next(v for v in ("o1", "o2", "o3") if v != r.assignee)
        with self.assertRaises(NotAvailable):
            svc.commit(r.id, other)
        svc.commit(r.id, r.assignee)
        with self.assertRaises(NotAvailable):  # cannot commit twice either
            svc.commit(r.id, r.assignee)

    def test_concurrent_self_claims_exactly_one_wins(self):
        svc, *_ = make(oslo_present=())          # nobody in office -> ESCALATED
        r = svc.report_empty(OSLO, "x")
        self.assertEqual(r.state, State.ESCALATED)
        for v in ("o1", "o2", "o3"):             # three people walk in at once
            svc.set_presence(v, Presence.IN_OFFICE)
        barrier, results = threading.Barrier(3), []

        def go(vid):
            barrier.wait()
            try:
                svc.commit(r.id, vid); results.append(("ok", vid))
            except NotAvailable:
                results.append(("lost", vid))
        ts = [threading.Thread(target=go, args=(v,)) for v in ("o1", "o2", "o3")]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sum(1 for s, _ in results if s == "ok"), 1)
        winner = next(v for s, v in results if s == "ok")
        self.assertEqual(svc.refills[r.id].assignee, winner)


class NobodyCompletes(unittest.TestCase):
    def test_unanswered_offer_goes_to_someone_else(self):
        svc, clock, chat, _ = make()
        r = svc.report_empty(OSLO, "x")
        first = r.assignee
        clock.advance(3 * H)  # offer TTL is 2h
        svc.tick()
        self.assertEqual(r.state, State.OFFERED)
        self.assertNotEqual(r.assignee, first)
        self.assertIn(first, r.tried)

    def test_committed_but_no_purchase_is_reassigned_and_old_buyer_is_locked_out(self):
        svc, clock, chat, _ = make()
        r = svc.report_empty(OSLO, "x")
        first = r.assignee
        svc.commit(r.id, first)
        clock.advance(13 * H)
        svc.tick()
        self.assertTrue(any("reminder" in m for m in chat.to(first)))
        self.assertEqual(r.state, State.COMMITTED)   # still within the 24h window
        clock.advance(12 * H)
        svc.tick()
        self.assertEqual(r.state, State.OFFERED)
        self.assertNotEqual(r.assignee, first)
        with self.assertRaises(NotAvailable):        # first buyer is too late
            svc.submit_receipt(r.id, first, 500, "NOK", "late.jpg")

    def test_release_passes_it_on_immediately(self):
        svc, *_ = make()
        r = svc.report_empty(OSLO, "x")
        first = r.assignee
        svc.release(r.id, first)
        self.assertNotEqual(r.assignee, first)

    def test_escalates_after_max_attempts_and_admin_can_retry(self):
        svc, clock, chat, _ = make(Policy(max_attempts=2))
        r = svc.report_empty(OSLO, "x")
        for _ in range(2):
            clock.advance(3 * H); svc.tick()
        self.assertEqual(r.state, State.ESCALATED)
        self.assertEqual(r.escalation_reason, "MAX_ATTEMPTS")
        self.assertEqual(len([m for m in chat.to("admin_oslo")]), 1)
        svc.tick(); svc.tick()  # no spam
        self.assertEqual(len(chat.to("admin_oslo")), 1)
        svc.admin_retry(r.id, "admin_oslo")
        self.assertEqual(r.state, State.OFFERED)

    def test_nobody_in_office_escalates_then_recovers_when_someone_arrives(self):
        svc, clock, chat, _ = make(oslo_present=())
        r = svc.report_empty(OSLO, "x")
        self.assertEqual(r.escalation_reason, "NO_VOLUNTEER")
        self.assertEqual(len(chat.to("admin_oslo")), 1)
        svc.set_presence("o2", Presence.IN_OFFICE)
        svc.tick()
        self.assertEqual((r.state, r.assignee), (State.OFFERED, "o2"))


class Selection(unittest.TestCase):
    def test_fair_rotation(self):
        svc, *_ = make()
        order = [refill(svc, OSLO, 400).assignee for _ in range(3)]
        self.assertEqual(sorted(order), ["o1", "o2", "o3"])  # everyone once before anyone twice

    def test_skips_people_not_in_office(self):
        svc, *_ = make()
        svc.set_presence("o1", Presence.AWAY); svc.set_presence("o2", Presence.REMOTE)
        self.assertEqual(svc.report_empty(OSLO, "x").assignee, "o3")

    def test_visitor_can_refill_the_office_they_are_in_but_not_their_home_one(self):
        svc, *_ = make(oslo_present=(), blr_present=())
        svc.volunteers["o1"].volunteer_for = {OSLO, BLR}
        svc.set_presence("o1", Presence.IN_OFFICE, location=BLR)  # Oslo employee visiting Bengaluru
        self.assertEqual(svc.report_empty(BLR, "x").assignee, "o1")
        self.assertEqual(svc.report_empty(OSLO, "x").state, State.ESCALATED)  # not physically there

    def test_person_waiting_for_money_is_skipped(self):
        svc, *_ = make()
        refill(svc, OSLO, 1200)           # o1 (first by id) now has 1200 NOK outstanding
        refill(svc, OSLO, 400)            # o2
        refill(svc, OSLO, 400)            # o3
        # everybody has 1 refill; o1 would be next by rotation order but is over-exposed
        nxt = svc.report_empty(OSLO, "x").assignee
        self.assertIn(nxt, ("o2", "o3"))

    def test_overdue_claim_blocks_selection_but_falls_back_if_it_is_the_only_option(self):
        svc, clock, *_ = make(oslo_present=("o1",))
        refill(svc, OSLO, 400)
        clock.advance(11 * timedelta(days=1))
        # o1's claim is 11 days old -> over-exposed, but he is the only person in -> still asked
        self.assertEqual(svc.report_empty(OSLO, "x").assignee, "o1")


class Budget(unittest.TestCase):
    def test_cap_shrinks_with_remaining_budget_and_exhaustion_escalates(self):
        svc, clock, chat, _ = make()
        for _ in range(4):
            refill(svc, OSLO, 1200)       # 4800 of 6000 spent
        r = svc.report_empty(OSLO, "x")
        svc.commit(r.id, r.assignee)
        self.assertEqual(r.reserved_cap, D(1200))
        svc.submit_receipt(r.id, r.assignee, 1100, "NOK", "i.jpg")   # 5900 spent, 100 left
        r2 = svc.report_empty(OSLO, "x")
        self.assertEqual((r2.state, r2.escalation_reason), (State.ESCALATED, "BUDGET"))
        self.assertTrue(chat.to("admin_oslo"))

    def test_budget_resumes_next_month(self):
        svc, clock, *_ = make()
        for _ in range(5):
            refill(svc, OSLO, 1200)       # exactly 6000
        r = svc.report_empty(OSLO, "x")
        self.assertEqual(r.escalation_reason, "BUDGET")
        clock.t = datetime(2026, 12, 1, 9, 0)
        svc.tick()
        self.assertEqual(r.state, State.OFFERED)

    def test_admin_override_allows_over_budget_refill(self):
        svc, *_ = make()
        for _ in range(5):
            refill(svc, OSLO, 1200)
        r = svc.report_empty(OSLO, "x")
        with self.assertRaises(NotAuthorized):
            svc.admin_override_budget(r.id, "admin_blr")
        svc.admin_override_budget(r.id, "admin_oslo")
        self.assertEqual(r.state, State.OFFERED)

    def test_receipt_over_cap_needs_approval_and_is_not_counted_until_approved(self):
        svc, _, _, hr = make()
        r = svc.report_empty(BLR, "x")
        svc.commit(r.id, r.assignee)
        svc.submit_receipt(r.id, r.assignee, 4000, "INR", "i.jpg")   # cap is 3000
        self.assertEqual(r.state, State.PENDING_APPROVAL)
        self.assertEqual(hr.claims, {})
        self.assertEqual(svc.monthly_report(2026, 11)["offices"]["BENGALURU"]["spent"], D(0))
        svc.admin_approve(r.id, "admin_blr")
        self.assertEqual(r.state, State.CLAIM_SUBMITTED)
        self.assertEqual(svc.monthly_report(2026, 11)["offices"]["BENGALURU"]["spent"], D(4000))

    def test_receipt_currency_must_match_office(self):
        svc, *_ = make()
        r = svc.report_empty(BLR, "x")
        svc.commit(r.id, r.assignee)
        with self.assertRaises(InvalidReceipt):
            svc.submit_receipt(r.id, r.assignee, 500, "NOK", "i.jpg")


class HRIntegration(unittest.TestCase):
    def test_hr_outage_is_retried_without_duplicate_claims(self):
        svc, clock, _, hr = make()
        hr.fail_next = 2
        r = refill(svc, OSLO, 600)
        self.assertEqual(r.state, State.CLAIM_PENDING)
        self.assertEqual(svc.monthly_report(2026, 11)["offices"]["OSLO"]["spent"], D(600))  # already counted
        svc.tick(); svc.tick(); svc.tick()
        self.assertEqual(r.state, State.CLAIM_SUBMITTED)
        self.assertEqual(len(hr.claims), 1)
        self.assertEqual(hr.claims[r.claim_id]["employee_id"], r.assignee)

    def test_reimbursed_in_payroll_currency_and_overdue_claims_are_chased(self):
        svc, clock, chat, hr = make()
        svc.volunteers["b1"].payroll_currency = "NOK"; hr.payroll_currency_of["b1"] = "NOK"
        r = refill(svc, BLR, 2000)
        self.assertEqual(r.assignee, "b1")
        clock.advance(11 * timedelta(days=1)); svc.tick()
        self.assertTrue(r.chased)
        self.assertTrue(any("chase" in m for m in chat.to("admin_blr")))
        hr.approve(r.claim_id); svc.tick()
        self.assertEqual(r.state, State.REIMBURSED)
        self.assertEqual(hr.get_claim(r.claim_id)["reimbursed_amount"], D(250))  # 2000 INR -> 250 NOK
        self.assertIn("250.00 NOK", chat.to("b1")[-1])

    def test_hr_rejection_goes_to_a_human(self):
        svc, clock, chat, hr = make()
        r = refill(svc, OSLO, 500)
        hr.reject(r.claim_id); svc.tick()
        self.assertEqual(r.state, State.HR_REJECTED)
        self.assertTrue(any("rejected" in m for m in chat.to("admin_oslo")))
        self.assertEqual(svc.monthly_report(2026, 11)["offices"]["OSLO"]["spent"], D(0))


class Reporting(unittest.TestCase):
    def test_totals_across_both_currencies(self):
        svc, clock, *_ = make()
        refill(svc, OSLO, 1000, items=[("chips", 10, D(400)), ("fruit", 5, D(600))])
        refill(svc, BLR, 2000, items=[("chips", 20, D(800)), ("nuts", 4, D(1200))])
        rep = svc.monthly_report(2026, 11, "NOK")
        o, b = rep["offices"]["OSLO"], rep["offices"]["BENGALURU"]
        self.assertEqual((o["spent"], o["remaining"], o["utilisation"]), (D(1000), D(5000), D("0.1667")))
        self.assertEqual((b["spent"], b["remaining"]), (D(2000), D(13000)))
        self.assertEqual(b["spent_in_reporting_currency"], D(250))
        self.assertEqual(rep["total"], D(1250))            # 1000 NOK + 2000 INR*0.125
        self.assertEqual(svc.monthly_report(2026, 11, "INR")["total"], D(10000))  # 1000 NOK = 8000 INR + 2000
        self.assertEqual(rep["top_items"][0], ("fruit", 5, D(600)))
        chips = next(t for t in rep["top_items"] if t[0] == "chips")
        self.assertEqual((chips[1], chips[2]), (30, D(400) + D(100)))  # 20 chips cost 800 INR = 100 NOK

    def test_other_months_and_unfinished_refills_are_excluded(self):
        svc, clock, *_ = make()
        refill(svc, OSLO, 700)
        clock.t = datetime(2026, 12, 2, 9, 0)
        refill(svc, OSLO, 300)
        svc.report_empty(OSLO, "x")                         # in flight, no spend yet
        self.assertEqual(svc.monthly_report(2026, 11)["total"], D(700))
        self.assertEqual(svc.monthly_report(2026, 12)["total"], D(300))


if __name__ == "__main__":
    unittest.main()
