from decimal import Decimal


class FxRates:
    """Fixed table, e.g. {("INR","NOK"): Decimal("0.125")}. Inverse is derived.
    In production: one rate per month (e.g. central-bank average) stored with the
    report, so a closed month never changes when markets move."""

    def __init__(self, table):
        self._t = {}
        for (a, b), r in table.items():
            self._t[(a, b)] = Decimal(r)
            self._t[(b, a)] = Decimal(1) / Decimal(r)

    def convert(self, amount, src, dst):
        if src == dst:
            return Decimal(amount)
        try:
            return Decimal(amount) * self._t[(src, dst)]
        except KeyError:
            raise ValueError(f"no rate {src}->{dst}")
