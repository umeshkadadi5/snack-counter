# Snack Counter Automation — Design

## What "automate" means here
The volunteers' goodwill stays (people still shop with their own money). What I automate is the **coordination and bookkeeping** around it: detecting "empty", asking *one* person at a time, escalating when nobody delivers, filing the HR claim, and reporting spend. Nobody has to remember anything or ask in a group chat.

**Assumptions** (I could not ask, so these are explicit; each is a config value): one counter per office; "empty" is signalled by anyone pressing a button in the chat tool; volunteers set presence with a chat command (`/here`, `/remote`, `/away`) or a calendar sync; each office has one admin; purchase currency = the office's currency; FX only matters for the report and for ranking exposure.

## Lifecycle: empty → reimbursed
1. **Report** – anyone presses "box empty". If a refill is already in flight for that office, they get that one back (dedupe) → no second purchase.
2. **Offer** – the system picks *one* volunteer who is in the office *today* and DMs them (Yes/No, 2h TTL). Not a broadcast, so nobody assumes "someone else will".
3. **Commit** – "Yes" atomically moves it to COMMITTED and starts a 24h purchase deadline plus the spending cap (min of per-purchase max and remaining monthly budget). A reminder goes out at the halfway mark.
4. **Receipt** – volunteer uploads amount + receipt photo (+ optional line items). ≤ cap → the system files the HR claim immediately. > cap → admin approves/rejects first.
5. **Claim** – HR claim created with an idempotency key. A worker polls status; HR approves in 1–10 days → volunteer is told they've been reimbursed (HR pays in payroll currency). Claims older than 10 days ping the admin to chase.

Failure paths: offer expires / volunteer declines / deadline missed → next volunteer. No one eligible, 4 failed attempts, or budget exhausted → **ESCALATED** to the office admin (told once, retried each tick; a volunteer who walks in can also self-claim). HR down → CLAIM_PENDING, retried, never duplicated.

## Parts and the data each keeps
| Part | Owns | Notes |
|---|---|---|
| **Refill service** (core) | `Refill` (state, office, assignee, tried-list, deadline, cap, amount, currency, items, receipt ref, HR claim id, audit trail) | Single source of truth; state machine; one lock/DB transaction per transition |
| **Volunteer directory** | id, home office, payroll currency, offices volunteered for, *current* location, presence | Synced from company login/HR + chat commands; visitors just change `location` |
| **Selection** | nothing (pure function over refills) | Fewest refills in 90 days → longest since last → id. Skips people with ≥ 1 max-purchase unreimbursed or a claim > 10 days old (soft: falls back rather than leave the box empty) |
| **Budget** | nothing (derived: budget − counted spend this month, per office, in its own currency) | Counted from CLAIM_PENDING onwards; HR-rejected/admin-rejected don't count |
| **Chat adapter** | message templates, button callbacks | Behind an interface (`send`); fake in tests |
| **HR adapter** | claim id ↔ refill id | Behind an interface (`create_claim`, `get_claim`); idempotency key = refill id |
| **Scheduler** | nothing | Calls `tick()` every few minutes; all time rules live there and are idempotent |
| **Report** | nothing (computed) | Monthly: spend/budget/utilisation per office, one total in NOK, top items |

## How each problem is prevented
- **Empty for days** → one named person is asked (not a crowd), with TTLs, reminders, automatic reassignment and admin escalation. Presence-aware, so hybrid/holiday people aren't asked.
- **Two people buy** → max one active refill per office; commit is compare-and-set (only the current assignee, or one self-claimer, wins; tested with concurrent threads). Late buyers are told not to buy.
- **A few people do everything / slow reimbursement** → fairness rotation + exposure rule + claim filed the minute the receipt is uploaded + chasing at 10 days.
- **No visibility** → every purchase is a structured record; report gives spend per office/month, utilisation and top items (from optional line items).
- **Budgets** → cap per refill shrinks with remaining budget; exhausted ⇒ escalate (admin can override); next month resumes automatically; over-cap receipts need approval rather than being silently paid.

## Trade-offs
- **One person at a time** is slower than a broadcast "who wants it?", but removes the bystander effect and races; the 2h TTL bounds the cost.
- **Soft budget**: the volunteer has already spent the money when the receipt arrives, so we gate *before* (cap) and *route* after (approval) instead of rejecting.
- **Report FX** uses a fixed rate table (ideally one stored monthly rate) so closed months never change.
- **In-memory + lock** in the sample. In production: Postgres, partial unique index `(office) WHERE state IN (active)` and conditional `UPDATE … WHERE state='OFFERED' AND assignee=?` give the same exclusivity across processes.
- Timestamps are naive UTC in the sample; real version stores UTC and computes "month" and "office hours" in the office's timezone (Oslo and Bengaluru differ by 3.5–4.5h).

## Deliberately not built
Receipt OCR / auto-verification (admin approval covers outliers), stock sensing/IoT, snack ordering or delivery, a shopping list/voting UI, a real chat/HR integration, payroll FX handling (HR does that), working-hours-aware TTLs, a web dashboard, per-person opt-out schedules beyond presence. The first two I'd add next: working-hours-aware timers and "who's in the office" from the calendar.
