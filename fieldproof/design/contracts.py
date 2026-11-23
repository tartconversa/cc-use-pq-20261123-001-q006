from __future__ import annotations

from datetime import date, datetime, timedelta
from fractions import Fraction
import math
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)
]
Arm = Literal["standard", "assisted"]
ARMS: tuple[str, str] = ("standard", "assisted")
Climate = Literal["cold", "temperate", "warm"]
Load = Literal["light", "heavy"]
Rate = Annotated[StrictInt, Field(ge=0, le=10_000)]
Weight = Annotated[StrictInt, Field(gt=0, le=10_000)]
WholeSecond = Annotated[AwareDatetime, Field()]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def whole_seconds(cls, value):
        if isinstance(value, datetime) and value.microsecond:
            raise ValueError("Only whole-second timestamps are accepted")
        return value


class ChannelWeight(Input):
    channel_id: Key
    weight_bp: Weight


class Cell(Input):
    climate: Climate
    load: Load
    weight_bp: Weight
    channels: list[ChannelWeight] | None = Field(default=None, min_length=1, max_length=8)

    @model_validator(mode="after")
    def channel_composition(self):
        if self.channels is None:
            return self
        if len({item.channel_id for item in self.channels}) != len(self.channels):
            raise ValueError("A cell cannot list the same recruitment channel twice")
        if sum(item.weight_bp for item in self.channels) != 10_000:
            raise ValueError("Cell channel weights must add up to 10,000 basis points")
        return self

    @property
    def key(self) -> str:
        return f"{self.climate}/{self.load}"

    @property
    def channel_ids(self) -> tuple[str, ...]:
        return tuple(item.channel_id for item in self.channels or ())


class Market(Input):
    market_id: Key
    label: ShortText
    cells: list[Cell] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def composition(self):
        if len({cell.key for cell in self.cells}) != len(self.cells):
            raise ValueError("A market cannot have duplicate comparison cells")
        if sum(cell.weight_bp for cell in self.cells) != 10_000:
            raise ValueError("Market weights must add up to 10,000 basis points")
        registered = [cell.channels is not None for cell in self.cells]
        if any(registered) and not all(registered):
            raise ValueError("Every cell in a market must register recruitment channels")
        return self

    @property
    def has_channels(self) -> bool:
        return all(cell.channels is not None for cell in self.cells)


class Policy(Input):
    required_units_per_arm: Annotated[StrictInt, Field(ge=2, le=100)]
    required_cycles_per_unit: Annotated[StrictInt, Field(ge=1, le=100)]
    minimum_unit_coverage_bp: Rate
    max_alarm_increase_bp: Rate
    max_failure_increase_bp: Rate
    min_manual_saving_bp: Rate
    max_energy_increase_bp: Rate
    max_missing_energy_bp: Rate
    max_cycle_seconds: Annotated[StrictInt, Field(ge=60, le=172_800)]
    max_cycle_wh: Annotated[StrictInt, Field(ge=1, le=50_000)]
    # Pre-registered target weight that must be supported by both arms before a
    # market-level conclusion may be published. 10,000 means "full target only".
    minimum_target_coverage_bp: Rate = 10_000
    # The largest share of the supported target weight a single recruitment
    # channel may contribute. 10,000 disables the concentration guard.
    max_channel_contribution_bp: Rate = 10_000


class TrialPlan(Input):
    title: ShortText
    program: Key
    starts_on: date
    ends_before: date
    policy: Policy
    markets: list[Market] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def trial_window(self):
        days = (self.ends_before - self.starts_on).days
        if not 1 <= days <= 180:
            raise ValueError("Trial must span between 1 and 180 local dates")
        if len({market.market_id for market in self.markets}) != len(self.markets):
            raise ValueError("Market identifiers must be unique")
        return self

    def market(self, market_id: str) -> Market | None:
        return next((item for item in self.markets if item.market_id == market_id), None)


class NewTrial(Input):
    trial_id: Key
    plan: TrialPlan


class ReviseTrial(Input):
    expected_version: Annotated[StrictInt, Field(ge=1)]
    plan: TrialPlan


class AdvancePhase(Input):
    expected_version: Annotated[StrictInt, Field(ge=1)]


class Site(Input):
    site_id: Key
    market_id: Key
    climate: Climate
    timezone: ShortText

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Expected an IANA time zone") from exc
        return value


class Enrollment(Input):
    unit_id: Key
    site_id: Key
    arm: Arm
    load: Load
    active_from: date
    active_until: date
    channel_id: Key | None = None

    @model_validator(mode="after")
    def interval(self):
        if self.active_from >= self.active_until:
            raise ValueError("Enrollment end must follow enrollment start")
        return self


class Enrollments(Input):
    units: list[Enrollment] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_units(self):
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("Duplicate units within a single enrollment request")
        return self


class Candidate(Input):
    candidate_id: Key
    device_id: Key | None = None
    site_id: Key
    load: Load
    channels: list[Key] = Field(default_factory=list, max_length=8)
    available_from: date
    available_until: date

    @property
    def device(self) -> str:
        return self.device_id or self.candidate_id

    @model_validator(mode="after")
    def availability(self):
        if self.available_from >= self.available_until:
            raise ValueError("Candidate availability end must follow its start")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("A candidate cannot list the same channel twice")
        return self


class Candidates(Input):
    candidates: list[Candidate] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_candidates(self):
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate candidates within a single registration request")
        return self


class Invitation(Input):
    candidate_id: Key
    arm: Arm
    channel_id: Key | None = None


class Invitations(Input):
    invitations: list[Invitation] = Field(min_length=1, max_length=500)
    expected_version: Annotated[StrictInt, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def unique_candidates(self):
        if len({item.candidate_id for item in self.invitations}) != len(self.invitations):
            raise ValueError("A candidate can receive only one invitation per request")
        return self


class AcceptInvitation(Input):
    accepted_on: date


class WithdrawInvitation(Input):
    note: ShortText


def local_day(moment: datetime, site: Site) -> date:
    return moment.astimezone(ZoneInfo(site.timezone)).date()


def channel_unit_targets(
    required: int, channels: list[tuple[str, int]]
) -> dict[str, int]:
    """Split a cell's per-arm unit target across channels by pre-registered weight.

    Targets are apportioned proportionally with the largest-remainder method.
    Every registered channel keeps at least one unit per arm, so a channel with
    a small share is still recruited rather than silently skipped. When there
    are more channels than ``required``, the effective per-arm target rises to
    the channel count so each channel has a real target to fill.
    """
    if not channels:
        return {}
    total = sum(weight for _, weight in channels) or 1
    exact = {channel: Fraction(required * weight, total) for channel, weight in channels}
    targets = {channel: max(1, math.floor(value)) for channel, value in exact.items()}
    difference = required - sum(targets.values())
    if difference > 0:
        rank = sorted(
            channels,
            key=lambda item: (-(exact[item[0]] - math.floor(exact[item[0]])), item[0]),
        )
        for offset in range(difference):
            targets[rank[offset % len(rank)][0]] += 1
    while difference < 0:
        reducible = sorted(
            (channel for channel, value in targets.items() if value > 1),
            key=lambda channel: (exact[channel] - math.floor(exact[channel]), channel),
        )
        if not reducible:
            break
        targets[reducible[0]] -= 1
        difference += 1
    return targets


def enrolled_at(unit: Enrollment, day: date) -> bool:
    return unit.active_from <= day < unit.active_until


def enrolled_days(unit: Enrollment, start: date, stop: date) -> int:
    overlap = min(unit.active_until, stop) - max(unit.active_from, start)
    return max(overlap, timedelta()).days
