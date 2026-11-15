# Snack Counter Automation

Design: [DESIGN.md](DESIGN.md). Code: `snackcounter/` (stdlib only, Python 3.9+).

```
python3 -m unittest discover -s tests -v    # 25 tests, no dependencies
```

- `service.py` – refill lifecycle (state machine, offers, timeouts, escalation, HR claims, report)
- `selection.py` – who buys · `budget.py` – budget check · `rates.py` – FX
- `fakes.py` – simulated chat tool and HR system (idempotent claims, outage injection)
- `tests/` – exclusivity (incl. threaded race), reassignment, fairness, visitors, budgets in NOK/INR, HR failure/retry, reports
