from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

from fieldproof.analysis.cohorts import (
    ARMS,
    ArmSummary,
    UnitSummary,
    by_channel,
    by_placement,
)
from fieldproof.design.contracts import Market, Policy, channel_unit_targets
from fieldproof.design.registry import Trial

RATE_METRICS = ("manual", "alarm", "failure")
METRICS = (*RATE_METRICS, "energy_wh", "duration_seconds")
POPULATION_REASONS = {"units", "enrollment_coverage"}
ENERGY_REASONS = {"energy_units", "energy_coverage"}
SCOPE_FULL = "full"
SCOPE_RESTRICTED = "restricted"
SCOPE_INSUFFICIENT = "insufficient"
SCOPES = (SCOPE_FULL, SCOPE_RESTRICTED, SCOPE_INSUFFICIENT)


def display(value: Fraction | None, multiplier: int = 1) -> str | None:
    if value is None:
        return None
    scaled = round(value * multiplier * 1_000)
    sign = "-" if scaled < 0 else ""
    whole, remainder = divmod(abs(scaled), 1_000)
    return f"{sign}{whole}.{remainder:03d}"


def weight_text(value_bp: Fraction | None) -> str | None:
    return display(value_bp, 1)


def pair(metric: str, standard: Fraction | None, assisted: Fraction | None) -> dict:
    scale = 10_000 if metric in RATE_METRICS else 1
    delta = assisted - standard if assisted is not None and standard is not None else None
    payload = {
        "standard": display(standard, scale),
        "assisted": display(assisted, scale),
        "assisted_minus_standard": display(delta, scale),
        "unit": "basis_points" if scale == 10_000 else ("Wh" if metric == "energy_wh" else "seconds"),
    }
    if metric == "energy_wh":
        payload["relative_change_bp"] = display(delta / standard, 10_000) if standard and delta is not None else None
    return payload


def coverage_gaps(arm: str, summary: ArmSummary, required_units: int, policy: Policy) -> list[dict]:
    gaps = []
    usable = len(summary.eligible)
    energy = summary.energy_coverage
    requirements = (
        ("units", usable, required_units),
        ("enrollment_coverage", summary.enrollment_coverage * 10_000, policy.minimum_unit_coverage_bp),
        ("energy_units", summary.measured_units, required_units),
        ("energy_coverage", None if energy is None else energy * 10_000, 10_000 - policy.max_missing_energy_bp),
    )
    for reason, observed, minimum in requirements:
        if observed is None or observed < minimum:
            gaps.append(
                {"arm": arm, "reason": reason, "observed": display(observed), "minimum": minimum}
            )
    return gaps


def supports_metric(gaps: list[dict], metric: str) -> bool:
    blocking = POPULATION_REASONS
    if metric == "energy_wh":
        blocking = POPULATION_REASONS | ENERGY_REASONS
    return not any(gap["reason"] in blocking for gap in gaps)


@dataclass
class ChannelEntry:
    """One pre-registered target: a cell's recruitment channel for an arm pair."""

    cell: str
    channel: str | None
    weight_bp: Fraction
    required_units: int
    arms: dict[str, ArmSummary]
    gaps: list[dict] = field(default_factory=list)

    def supports(self, metric: str) -> bool:
        return supports_metric(self.gaps, metric)

    def population_supported(self) -> bool:
        return not any(gap["reason"] in POPULATION_REASONS for gap in self.gaps)

    def arm_gaps(self, arm: str) -> list[dict]:
        return [gap for gap in self.gaps if gap["arm"] == arm]

    def mean(self, metric: str, arm: str) -> Fraction | None:
        return self.arms[arm].mean(metric)

    def stage(self, policy: Policy) -> dict:
        arms = {}
        for arm in ARMS:
            summary = self.arms[arm]
            recruited = len(summary.recruited)
            gaps = self.arm_gaps(arm)
            reasons = sorted({gap["reason"] for gap in gaps})
            if recruited < self.required_units:
                stage = "recruitment"
                reasons = ["recruited_below_target", *reasons]
            elif reasons:
                stage = "observation"
            else:
                stage = "supported"
            arms[arm] = {
                "stage": stage,
                "recruited_units": recruited,
                "eligible_units": len(summary.eligible),
                "target_units": self.required_units,
                "reasons": reasons,
            }
        if all(item["stage"] == "supported" for item in arms.values()):
            overall = "supported"
        elif any(item["stage"] == "recruitment" for item in arms.values()):
            overall = "recruitment"
        else:
            overall = "observation"
        return {"stage": overall, "arms": arms}

    def payload(self, policy: Policy) -> dict:
        return {
            "cell": self.cell,
            "channel": self.channel,
            "weight_bp": weight_text(self.weight_bp * 10_000),
            "target_units_per_arm": self.required_units,
            "supported": self.population_supported(),
            "stage": self.stage(policy),
            "arms": {
                arm: {
                    **summary.payload(),
                    "unit_coverage_bp": display(summary.enrollment_coverage, 10_000),
                    "energy_coverage_bp": display(summary.energy_coverage, 10_000),
                }
                for arm, summary in self.arms.items()
            },
            "gaps": self.gaps,
            "metrics": {
                name: pair(name, self.mean(name, "standard"), self.mean(name, "assisted"))
                if self.supports(name)
                else pair(name, None, None)
                for name in METRICS
            },
        }


@dataclass
class CellEstimate:
    key: str
    weight_bp: int
    arms: dict[str, ArmSummary]
    gaps: list[dict]
    entries: list[ChannelEntry]

    def supports(self, name: str) -> bool:
        return not any(gap["reason"] in POPULATION_REASONS | (
            ENERGY_REASONS if name == "energy_wh" else set()
        ) for gap in self.gaps)

    def cell_metric(self, name: str, arm: str) -> Fraction | None:
        if all(entry.channel is None for entry in self.entries):
            # Legacy climate/load-only cells keep the original pooled meaning.
            return self.arms[arm].mean(name)
        # A channel cell has no pooled meaning: every registered channel must
        # support the metric before a cell-wide number exists. Per-channel
        # results stay available in each entry.
        if not all(entry.supports(name) for entry in self.entries):
            return None
        total = sum((entry.weight_bp for entry in self.entries), Fraction(0))
        if not total:
            return None
        values = []
        for entry in self.entries:
            value = entry.mean(name, arm)
            if value is None:
                return None
            values.append(value * entry.weight_bp)
        return sum(values, Fraction(0)) / total

    def payload(self, policy: Policy) -> dict:
        return {
            "cell": self.key,
            "weight_bp": self.weight_bp,
            "arms": {
                arm: {
                    **summary.payload(),
                    "unit_coverage_bp": display(summary.enrollment_coverage, 10_000),
                    "energy_coverage_bp": display(summary.energy_coverage, 10_000),
                }
                for arm, summary in self.arms.items()
            },
            "gaps": self.gaps,
            "channels": [entry.payload(policy) for entry in self.entries],
            "metrics": {
                name: pair(name, self.cell_metric(name, "standard"), self.cell_metric(name, "assisted"))
                for name in METRICS
            },
        }


def assess_cells(
    market: Market, units: list[UnitSummary], policy: Policy
) -> list[CellEstimate]:
    placed_all = by_placement(units)
    placed_channel = by_channel(units)
    base = policy.required_units_per_arm
    estimates = []
    for cell in market.cells:
        pooled = {
            arm: ArmSummary.assess(
                placed_all.get((market.market_id, cell.key, arm), []), policy
            )
            for arm in ARMS
        }
        gaps = [
            gap
            for arm, summary in pooled.items()
            for gap in coverage_gaps(arm, summary, base, policy)
        ]
        entries = []
        if cell.channels is None:
            entries.append(
                ChannelEntry(cell.key, None, Fraction(cell.weight_bp, 10_000), base, pooled, gaps)
            )
        else:
            targets = channel_unit_targets(
                base, [(item.channel_id, item.weight_bp) for item in cell.channels]
            )
            for channel in cell.channels:
                arms = {
                    arm: ArmSummary.assess(
                        placed_channel.get(
                            (market.market_id, cell.key, channel.channel_id, arm), []
                        ),
                        policy,
                    )
                    for arm in ARMS
                }
                required = targets[channel.channel_id]
                channel_gaps = [
                    gap
                    for arm, summary in arms.items()
                    for gap in coverage_gaps(arm, summary, required, policy)
                ]
                entries.append(
                    ChannelEntry(
                        cell.key,
                        channel.channel_id,
                        Fraction(cell.weight_bp * channel.weight_bp, 100_000_000),
                        required,
                        arms,
                        channel_gaps,
                    )
                )
        estimates.append(CellEstimate(cell.key, cell.weight_bp, pooled, gaps, entries))
    return estimates


def entry_weighted(
    entries: list[ChannelEntry], metric: str, arm: str
) -> Fraction | None:
    if not entries:
        return None
    total = sum((entry.weight_bp for entry in entries), Fraction(0))
    if not total:
        return None
    values = []
    for entry in entries:
        if not entry.supports(metric):
            return None
        value = entry.mean(metric, arm)
        if value is None:
            return None
        values.append(value * entry.weight_bp)
    return sum(values, Fraction(0)) / total


def concentration_shares(
    supported: list[ChannelEntry], policy: Policy
) -> dict:
    groups: dict[str, Fraction] = defaultdict(Fraction)
    for entry in supported:
        groups[entry.channel or ""] += entry.weight_bp
    total = sum(groups.values(), Fraction(0))
    shares = {
        channel: (weight / total * 10_000 if total else Fraction(0))
        for channel, weight in groups.items()
    }
    breach = any(value > policy.max_channel_contribution_bp for value in shares.values())
    return {
        "shares": {key: weight_text(value) for key, value in shares.items()},
        "max_channel_contribution_bp": policy.max_channel_contribution_bp,
        "breach": breach,
    }


def target_scope(
    entries: list[ChannelEntry], policy: Policy
) -> tuple[str, Fraction, list[ChannelEntry], dict]:
    channels_registered = any(entry.channel is not None for entry in entries)
    supported = [entry for entry in entries if entry.population_supported()]
    total = sum((entry.weight_bp for entry in entries), Fraction(0))
    supported_weight = sum((entry.weight_bp for entry in supported), Fraction(0))
    supported_bp = supported_weight * 10_000
    concentration = concentration_shares(supported, policy)
    if not channels_registered:
        # A climate/load-only pilot has no alternative scope: it is compared and
        # published whole, and its gates decide whether it is publishable.
        return SCOPE_FULL, supported_bp, supported, concentration
    if supported_weight == total and total > 0:
        scope = SCOPE_FULL
    elif supported_bp < policy.minimum_target_coverage_bp:
        scope = SCOPE_INSUFFICIENT
    else:
        scope = SCOPE_RESTRICTED
    if concentration["breach"]:
        scope = SCOPE_INSUFFICIENT
    return scope, supported_bp, supported, concentration


@dataclass
class MarketEstimate:
    market: Market
    cells: list[CellEstimate]
    entries: list[ChannelEntry]
    standard: dict[str, Fraction | None]
    assisted: dict[str, Fraction | None]
    supported_standard: dict[str, Fraction | None]
    supported_assisted: dict[str, Fraction | None]
    scope: str
    supported_weight_bp: Fraction
    supported_entries: list[ChannelEntry]
    concentration: dict
    policy: Policy

    @classmethod
    def build(cls, market: Market, cells: list[CellEstimate], policy: Policy):
        entries = [entry for cell in cells for entry in cell.entries]
        scope, supported_weight, supported, concentration = target_scope(entries, policy)
        channels_registered = any(entry.channel is not None for entry in entries)
        standard = {metric: entry_weighted(entries, metric, "standard") for metric in METRICS}
        assisted = {metric: entry_weighted(entries, metric, "assisted") for metric in METRICS}
        if channels_registered:
            supported_standard = {
                metric: entry_weighted(supported, metric, "standard") for metric in METRICS
            }
            supported_assisted = {
                metric: entry_weighted(supported, metric, "assisted") for metric in METRICS
            }
        else:
            # Restricting a legacy climate/load-only pilot would change the
            # comparison meaning, so its supported view is all-or-nothing.
            supported_standard = standard
            supported_assisted = assisted
        return cls(
            market,
            cells,
            entries,
            standard,
            assisted,
            supported_standard,
            supported_assisted,
            scope,
            supported_weight,
            supported,
            concentration,
            policy,
        )

    def vectors(self, scope: str) -> tuple[dict, dict]:
        if scope == SCOPE_RESTRICTED:
            return self.supported_standard, self.supported_assisted
        return self.standard, self.assisted

    def delta(self, name: str, scope: str = SCOPE_FULL) -> Fraction | None:
        standard, assisted = self.vectors(scope)
        left = standard[name]
        right = assisted[name]
        return right - left if left is not None and right is not None else None

    def evidence_entries(self, scope: str) -> list[ChannelEntry]:
        if scope == SCOPE_RESTRICTED:
            return self.supported_entries
        return self.entries

    def coverage_ok(self, scope: str) -> bool:
        entries = self.supported_entries if scope == SCOPE_RESTRICTED else self.entries
        return bool(entries) and all(not entry.gaps for entry in entries)

    def integrity(self, scope: str = SCOPE_FULL) -> dict:
        units = [
            unit
            for entry in self.evidence_entries(scope)
            for arm in entry.arms.values()
            for unit in arm.recruited
            if unit.integrity_issues
        ]
        return {
            "unresolved_units": len(units),
            "unresolved_with_alarm": sum(unit.unsafe_unresolved for unit in units),
            "by_site": integrity_by_site(units),
        }

    def coverage(self) -> dict:
        channels: dict[str, dict] = {}
        for entry in self.entries:
            bucket = channels.setdefault(
                entry.channel or "",
                {
                    "target_weight": Fraction(0),
                    "supported_weight": Fraction(0),
                    "entries": 0,
                    "supported_entries": 0,
                },
            )
            bucket["target_weight"] += entry.weight_bp
            bucket["entries"] += 1
            if entry.population_supported():
                bucket["supported_weight"] += entry.weight_bp
                bucket["supported_entries"] += 1
        total = sum((entry.weight_bp for entry in self.entries), Fraction(0))
        total_bp = total * 10_000
        unsupported = [entry for entry in self.entries if not entry.population_supported()]
        reasons: dict[str, Fraction] = defaultdict(Fraction)
        for entry in unsupported:
            reasons[entry.stage(self.policy)["stage"]] += entry.weight_bp
        return {
            "scope": self.scope,
            "total_weight_bp": weight_text(total_bp),
            "supported_weight_bp": weight_text(self.supported_weight_bp),
            "unsupported_weight_bp": weight_text(total_bp - self.supported_weight_bp),
            "minimum_weight_bp": self.policy.minimum_target_coverage_bp,
            "unsupported_by_reason": {
                stage: weight_text(weight * 10_000)
                for stage, weight in sorted(reasons.items())
            },
            "concentration": self.concentration,
            "channels": {
                key: {
                    "target_weight_bp": weight_text(value["target_weight"] * 10_000),
                    "supported_weight_bp": weight_text(value["supported_weight"] * 10_000),
                    "entries": value["entries"],
                    "supported_entries": value["supported_entries"],
                }
                for key, value in sorted(channels.items())
            },
            "unsupported": [
                {
                    "cell": entry.cell,
                    "channel": entry.channel,
                    "weight_bp": weight_text(entry.weight_bp * 10_000),
                    **entry.stage(self.policy),
                }
                for entry in unsupported
            ],
            "supported": [
                {
                    "cell": entry.cell,
                    "channel": entry.channel,
                    "weight_bp": weight_text(entry.weight_bp * 10_000),
                }
                for entry in self.supported_entries
            ],
        }

    def payload(self) -> dict:
        return {
            "market_id": self.market.market_id,
            "label": self.market.label,
            "cells": [cell.payload(self.policy) for cell in self.cells],
            "metrics": {
                name: pair(name, self.standard[name], self.assisted[name]) for name in METRICS
            },
            "supported_metrics": {
                name: pair(name, self.supported_standard[name], self.supported_assisted[name])
                for name in METRICS
            },
            "scope": self.scope,
            "target_coverage": self.coverage(),
            "integrity": self.integrity(),
        }


def integrity_by_site(units: list[UnitSummary]) -> dict[str, dict[str, int]]:
    from collections import Counter

    totals = defaultdict(Counter)
    for unit in units:
        totals[unit.site_id].update(unit.integrity_issues)
    return {site: dict(sorted(counts.items())) for site, counts in sorted(totals.items())}


def estimate(trial: Trial, units: dict[str, UnitSummary]) -> list[MarketEstimate]:
    values = list(units.values())
    return [
        MarketEstimate.build(
            market, assess_cells(market, values, trial.plan.policy), trial.plan.policy
        )
        for market in trial.plan.markets
    ]
