from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from fieldproof.analysis.cohorts import ARMS, ArmSummary
from fieldproof.analysis.contrast import MarketEstimate, display
from fieldproof.design.contracts import Policy

RATES = ("manual", "alarm", "failure")
RATE_SCALE = 10_000


@dataclass(frozen=True)
class Interval:
    low: Fraction
    high: Fraction

    def weighted(self, weight: Fraction):
        return Interval(self.low * weight, self.high * weight)

    def add(self, other: Interval):
        return Interval(self.low + other.low, self.high + other.high)

    def subtract(self, other: Interval):
        return Interval(self.low - other.high, self.high - other.low)

    def payload(self):
        return {
            "low_bp": display(self.low, RATE_SCALE),
            "high_bp": display(self.high, RATE_SCALE),
        }


def population_interval(arm: ArmSummary, name: str) -> Interval:
    """保持合格设备的观测率，计算全体入组设备均值的界限。"""
    total = len(arm.recruited)
    included = len(arm.eligible)
    if total == 0:
        return Interval(Fraction(0), Fraction(1))
    measured = sum((unit.metric(name) for unit in arm.eligible), Fraction(0))
    return Interval(measured / total, (measured + total - included) / total)


def range_verdict(bound: Interval, limit_bp: int, direction: str) -> str:
    boundary = Fraction(limit_bp, RATE_SCALE)
    if direction == "at_least":
        if bound.low >= boundary:
            return "robust"
        if bound.high < boundary:
            return "cannot_pass"
    else:
        if bound.high <= boundary:
            return "robust"
        if bound.low > boundary:
            return "cannot_pass"
    return "depends_on_unqualified_units"


def _channel_unknown(estimate: MarketEstimate, scopes) -> list[dict]:
    grouped: dict[str, dict] = {}
    for cell_key, channel, weight, arms, _ in scopes:
        bucket = grouped.setdefault(cell_key, {"cell": cell_key, "channels": {}})
        unknown = {}
        for arm, summary in arms.items():
            missing = len(summary.recruited) - len(summary.eligible)
            unknown[arm] = {
                "unqualified_units": missing,
                "has_no_enrollment": not summary.recruited,
                "market_weight_exposed_bp": display(
                    weight * (Fraction(missing, len(summary.recruited))
                              if summary.recruited else 1),
                    RATE_SCALE,
                ),
            }
        bucket["channels"][channel or ""] = unknown
    return [grouped[key] for key in sorted(grouped)]


def range_readout(estimate: MarketEstimate, policy: Policy) -> dict:
    scores = {
        "manual": (policy.min_manual_saving_bp, "at_least"),
        "alarm": (policy.max_alarm_increase_bp, "at_most"),
        "failure": (policy.max_failure_increase_bp, "at_most"),
    }
    market_ranges = {
        name: {arm: Interval(Fraction(0), Fraction(0)) for arm in ARMS}
        for name in RATES
    }
    missing_by_cell = []
    channelled = any(entry.channel is not None for entry in estimate.entries)
    if channelled:
        scopes = [
            (entry.cell, entry.channel, entry.weight_bp, entry.arms, entry.required_units)
            for entry in estimate.entries
        ]
        missing_by_cell = _channel_unknown(estimate, scopes)
    else:
        scopes = [
            (cell.key, None, Fraction(cell.weight_bp, RATE_SCALE), cell.arms,
             policy.required_units_per_arm)
            for cell in estimate.cells
        ]
    for cell_key, channel, weight, arms, _ in scopes:
        if not channelled:
            unknown = {}
            for arm, summary in arms.items():
                missing = len(summary.recruited) - len(summary.eligible)
                unknown[arm] = {
                    "unqualified_units": missing,
                    "has_no_enrollment": not summary.recruited,
                    "market_weight_exposed_bp": display(
                        weight * (Fraction(missing, len(summary.recruited))
                                  if summary.recruited else 1),
                        RATE_SCALE,
                    ),
                }
            missing_by_cell.append({"cell": cell_key, "arms": unknown})
        for arm, summary in arms.items():
            for name in RATES:
                contribution = population_interval(summary, name).weighted(weight)
                market_ranges[name][arm] = market_ranges[name][arm].add(contribution)
    metrics = {}
    for name, arms in market_ranges.items():
        left, right = (
            ("standard", "assisted") if name == "manual" else ("assisted", "standard")
        )
        delta = arms[left].subtract(arms[right])
        limit, direction = scores[name]
        metrics[name] = {
            **{arm: interval.payload() for arm, interval in arms.items()},
            "effect": {
                "definition": f"{left}_minus_{right}",
                **delta.payload(),
                "boundary_bp": limit,
                "direction": direction,
                "verdict": range_verdict(delta, limit, direction),
            },
        }
    return {
        "market_id": estimate.market.market_id,
        "interpretation": "Unqualified devices can have any rate between zero and one",
        "unknown_by_cell": missing_by_cell,
        "metrics": metrics,
    }
