"""Economic-model arithmetic (plan §2 #15, agent-modules.md §7): code computes every number.

Every value is a closed interval ``[low, high]`` of non-negative numbers. All formulas are
products of their inputs, so interval multiplication is the product of the lows and of the highs.
Money is in the pack's currency until :func:`to_usd` converts it at a dated rate.
"""

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class Interval:
    low: float
    high: float

    def __post_init__(self) -> None:
        if not 0 <= self.low <= self.high:
            raise ValueError(f"not a non-negative interval: [{self.low}, {self.high}]")

    def __mul__(self, other: "Interval | float") -> "Interval":
        if isinstance(other, Interval):
            return Interval(self.low * other.low, self.high * other.high)
        return Interval(self.low * other, self.high * other)

    def __truediv__(self, k: float) -> "Interval":
        return Interval(self.low / k, self.high / k)

    def rounded(self, digits: int = 2) -> list[float]:
        return [round(self.low, digits), round(self.high, digits)]


@dataclass(frozen=True)
class Input:
    name: str
    per: str | None  # money input: the unit after the currency ("hour" → "TRY/hour")
    unit: str | None = None  # non-money input: the exact unit

    def expected_unit(self, currency: str) -> str:
        return f"{currency}/{self.per}" if self.per else str(self.unit)


FORMULAS: dict[str, tuple[Input, ...]] = {
    "labor_savings": (
        Input("hours_saved_per_month", None, "hour/month"),
        Input("loaded_hourly_cost", "hour"),
    ),
    "error_cost_avoided": (
        Input("errors_per_month", None, "errors/month"),
        Input("cost_per_error", "error"),
        Input("share_avoidable", None, "fraction"),
    ),
    "revenue_recovered": (
        Input("lost_revenue_per_month", "month"),
        Input("share_recoverable", None, "fraction"),
    ),
    "compliance_cost": (
        Input("penalty_or_outsourcing_cost_per_month", "month"),
        Input("share_replaceable", None, "fraction"),
    ),
}


def value_local(formula: str, values: dict[str, Interval]) -> Interval:
    """Monthly value per customer in local currency: the product of the formula's inputs."""
    out = Interval(1.0, 1.0)
    for i in FORMULAS[formula]:
        out = out * values[i.name]
    return out


def to_usd(value: Interval, usd_rate: float) -> Interval:
    """Local currency → USD at ``usd_rate`` local units per USD."""
    return value / usd_rate


def loaded_hourly_cost(
    net_monthly_wage: float, multiplier: Interval, hours_per_month: float
) -> Interval:
    """Employer cost per working hour from a net monthly wage."""
    return Interval(net_monthly_wage, net_monthly_wage) * multiplier / hours_per_month


# Competitor price periods that convert to a monthly price (per_user_month is per seat).
MONTHLY = {"month": 1.0, "per_user_month": 1.0, "year": 1 / 12}


def monthly_usd(
    amount: float, currency: str, period: str | None, local_currency: str, usd_rate: float
) -> float | None:
    """A competitor price as USD per month, or ``None`` when its period or currency cannot be
    converted (one-time, per document, unknown period; a currency other than local or USD)."""
    factor = MONTHLY.get(period or "unknown")
    if factor is None or amount <= 0:
        return None
    if currency == "USD":
        return amount * factor
    if currency == local_currency:
        return amount * factor / usd_rate
    return None


def anchor(prices_usd: list[float]) -> dict[str, float] | None:
    """Min and median of observed monthly competitor prices (USD), or ``None`` without any."""
    if not prices_usd:
        return None
    return {"min": round(min(prices_usd), 2), "median": round(median(prices_usd), 2)}
