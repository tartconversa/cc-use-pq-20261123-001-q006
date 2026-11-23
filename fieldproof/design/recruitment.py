from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from sqlite3 import Connection

from fieldproof.common import conflict, encoded, invalid, missing
from fieldproof.design.contracts import (
    ARMS,
    Candidate,
    Enrollment,
    channel_unit_targets,
)
from fieldproof.design.registry import Roster, Trial, get_trial, require_phase, roster

PENDING = "pending"
ACCEPTED = "accepted"
WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Invitation:
    candidate_id: str
    arm: str
    channel_id: str | None
    status: str
    accepted_on: date | None = None

    def payload(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "arm": self.arm,
            "channel": self.channel_id,
            "status": self.status,
            "accepted_on": self.accepted_on.isoformat() if self.accepted_on else None,
        }


def cell_definition(plan, assigned: Roster, site_id: str, load: str):
    site = assigned.sites.get(site_id)
    if site is None:
        return None, None
    market = plan.market(site.market_id)
    if market is None:
        return None, None
    key = f"{site.climate}/{load}"
    return market, next((cell for cell in market.cells if cell.key == key), None)


def channels_registered(cell) -> bool:
    return cell is not None and cell.channels is not None


def candidate_map(db: Connection, trial_id: str) -> dict[str, Candidate]:
    rows = db.execute(
        "SELECT data FROM candidate WHERE trial_id=? ORDER BY candidate_id", (trial_id,)
    )
    return {
        item.candidate_id: item
        for item in (Candidate.model_validate_json(row["data"]) for row in rows)
    }


def register_candidates(db: Connection, trial: Trial, submitted: list[Candidate]) -> dict:
    require_phase(trial, "draft")
    assigned = roster(db, trial.trial_id)
    devices = {item.device: item.candidate_id for item in candidate_map(db, trial.trial_id).values()}
    additions = 0
    for candidate in submitted:
        if candidate.site_id not in assigned.sites:
            raise missing("site", candidate.site_id)
        owner = devices.get(candidate.device)
        if owner is not None and owner != candidate.candidate_id:
            raise conflict(
                "device_allocated",
                f"Device {candidate.device} already occupies a candidate slot",
                details={"candidate_id": owner},
            )
        devices[candidate.device] = candidate.candidate_id
        market, cell = cell_definition(trial.plan, assigned, candidate.site_id, candidate.load)
        if cell is None:
            raise invalid(
                "unplanned_cell",
                f"Candidate {candidate.candidate_id} has no planned cell",
            )
        if not channels_registered(cell):
            raise invalid(
                "channels_unplanned",
                "Candidates are only registered where the cell lists channels",
            )
        if not candidate.channels:
            raise invalid(
                "candidate_channels",
                f"Candidate {candidate.candidate_id} must list at least one channel",
            )
        unknown = sorted(set(candidate.channels) - set(cell.channel_ids))
        if unknown:
            raise invalid(
                "unplanned_channel",
                f"Candidate {candidate.candidate_id} names channels outside the plan",
                details={"channels": unknown},
            )
        if (
            candidate.available_from < trial.plan.starts_on
            or candidate.available_until > trial.plan.ends_before
        ):
            raise invalid(
                "candidate_window",
                f"Candidate {candidate.candidate_id} falls outside the trial",
            )
        canonical = encoded(candidate.model_dump(mode="json"))
        previous = db.execute(
            "SELECT data FROM candidate WHERE trial_id=? AND candidate_id=?",
            (trial.trial_id, candidate.candidate_id),
        ).fetchone()
        if previous is not None:
            if previous["data"] != canonical:
                raise conflict(
                    "candidate_exists",
                    f"Candidate {candidate.candidate_id} already has different details",
                )
            continue
        db.execute(
            "INSERT INTO candidate VALUES (?,?,?)",
            (trial.trial_id, candidate.candidate_id, canonical),
        )
        additions += 1
    if additions:
        _bump(db, trial.trial_id)
    return {
        "added": additions,
        "already_registered": len(submitted) - additions,
        "version": get_trial(db, trial.trial_id).version,
    }


def _bump(db: Connection, trial_id: str):
    db.execute("UPDATE trial SET version=version+1 WHERE trial_id=?", (trial_id,))


def _row(row) -> Invitation:
    data = json.loads(row["data"])
    accepted = data.get("accepted_on")
    return Invitation(
        candidate_id=data["candidate_id"],
        arm=data["arm"],
        channel_id=data.get("channel_id"),
        status=row["status"],
        accepted_on=date.fromisoformat(accepted) if accepted else None,
    )


def invitation_at(db: Connection, trial_id: str, candidate_id: str) -> Invitation | None:
    row = db.execute(
        "SELECT status, data FROM invitation WHERE trial_id=? AND candidate_id=?",
        (trial_id, candidate_id),
    ).fetchone()
    return _row(row) if row is not None else None


def invitations(db: Connection, trial_id: str) -> list[Invitation]:
    rows = db.execute(
        "SELECT status, data FROM invitation WHERE trial_id=? ORDER BY candidate_id",
        (trial_id,),
    )
    return [_row(row) for row in rows]


def _channel_for(candidate: Candidate, requested: str | None, cell) -> str | None:
    if not channels_registered(cell):
        return None
    planned = cell.channel_ids
    if requested is not None and requested not in planned:
        raise invalid("unplanned_channel", f"Channel {requested} is outside the plan")
    if requested is not None:
        if requested not in candidate.channels:
            raise invalid(
                "channel_unavailable",
                f"Candidate {candidate.candidate_id} did not arrive through {requested}",
                details={"available": sorted(candidate.channels)},
            )
        return requested
    reachable = [channel for channel in planned if channel in candidate.channels]
    if len(reachable) == 1:
        return reachable[0]
    if not reachable:
        raise invalid(
            "channel_unavailable",
            f"Candidate {candidate.candidate_id} has no planned channel",
        )
    raise invalid(
        "channel_required",
        f"Candidate {candidate.candidate_id} arrived through several channels",
        details={"available": sorted(reachable)},
    )


def invite(db: Connection, trial: Trial, submitted, expected_version: int | None = None) -> dict:
    require_phase(trial, "draft")
    if expected_version is not None and trial.version != expected_version:
        raise conflict(
            "trial_changed",
            "Read the current trial before changing invitations",
            details={"current_version": trial.version},
        )
    assigned = roster(db, trial.trial_id)
    candidates = candidate_map(db, trial.trial_id)
    changed = 0
    for request in submitted:
        candidate = candidates.get(request.candidate_id)
        if candidate is None:
            raise missing("candidate", request.candidate_id)
        _, cell = cell_definition(trial.plan, assigned, candidate.site_id, candidate.load)
        if not channels_registered(cell):
            raise invalid("channels_unplanned", "The candidate's cell lists no channels")
        channel = _channel_for(candidate, request.channel_id, cell)
        current = invitation_at(db, trial.trial_id, candidate.candidate_id)
        if current is not None and current.status == ACCEPTED:
            if current.arm == request.arm and current.channel_id == channel:
                continue
            raise conflict(
                "invitation_accepted",
                f"Candidate {candidate.candidate_id} already accepted a fixed invitation",
            )
        if (
            current is not None
            and current.status == PENDING
            and current.arm == request.arm
            and current.channel_id == channel
        ):
            continue
        data = encoded(
            {
                "candidate_id": candidate.candidate_id,
                "arm": request.arm,
                "channel_id": channel,
            }
        )
        db.execute(
            """INSERT INTO invitation VALUES (?,?,?,?)
               ON CONFLICT(trial_id,candidate_id) DO UPDATE
               SET status='pending', data=excluded.data""",
            (trial.trial_id, candidate.candidate_id, PENDING, data),
        )
        changed += 1
    if changed:
        _bump(db, trial.trial_id)
    return {"changed": changed, "version": get_trial(db, trial.trial_id).version}


def _accepted(invitation: Invitation, unit: Enrollment) -> dict:
    return {
        **invitation.payload(),
        "active_from": unit.active_from.isoformat(),
        "active_until": unit.active_until.isoformat(),
    }


def accept(db: Connection, trial: Trial, candidate_id: str, accepted_on: date) -> dict:
    require_phase(trial, "draft")
    assigned = roster(db, trial.trial_id)
    candidate = candidate_map(db, trial.trial_id).get(candidate_id)
    if candidate is None:
        raise missing("candidate", candidate_id)
    current = invitation_at(db, trial.trial_id, candidate_id)
    if current is None:
        raise missing("invitation", candidate_id)
    if current.status == ACCEPTED:
        unit = assigned.units.get(candidate_id)
        if unit is not None:
            return _accepted(current, unit)
        raise conflict("invitation_accepted", "This invitation is already accepted")
    if current.status == WITHDRAWN:
        raise conflict("invitation_withdrawn", "A withdrawn invitation cannot be accepted")
    if not candidate.available_from <= accepted_on < candidate.available_until:
        raise invalid(
            "acceptance_window",
            "Acceptance must fall inside the candidate's availability",
        )
    if candidate_id in assigned.units:
        raise conflict("unit_exists", f"Unit {candidate_id} is already enrolled")
    for other in candidate_map(db, trial.trial_id).values():
        if other.candidate_id == candidate_id or other.device != candidate.device:
            continue
        if other.candidate_id in assigned.units:
            raise conflict(
                "device_allocated",
                f"Device {candidate.device} already occupies another slot",
                details={"candidate_id": other.candidate_id},
            )
    unit = Enrollment(
        unit_id=candidate.candidate_id,
        site_id=candidate.site_id,
        arm=current.arm,
        load=candidate.load,
        active_from=max(candidate.available_from, trial.plan.starts_on),
        active_until=min(candidate.available_until, trial.plan.ends_before),
        channel_id=current.channel_id,
    )
    db.execute(
        "INSERT INTO enrollment VALUES (?,?,?,?,?)",
        (
            trial.trial_id,
            unit.unit_id,
            unit.site_id,
            unit.arm,
            encoded(unit.model_dump(mode="json")),
        ),
    )
    db.execute(
        """UPDATE invitation SET status=?, data=json_set(data, '$.accepted_on', ?)
           WHERE trial_id=? AND candidate_id=?""",
        (ACCEPTED, accepted_on.isoformat(), trial.trial_id, candidate_id),
    )
    _bump(db, trial.trial_id)
    return _accepted(
        Invitation(candidate_id, current.arm, current.channel_id, ACCEPTED, accepted_on),
        unit,
    )


def withdraw(db: Connection, trial: Trial, candidate_id: str, note: str) -> dict:
    require_phase(trial, "draft")
    current = invitation_at(db, trial.trial_id, candidate_id)
    if current is None:
        raise missing("invitation", candidate_id)
    if current.status == ACCEPTED:
        raise conflict(
            "invitation_accepted",
            "An accepted invitation is fixed and cannot be withdrawn",
        )
    if current.status != WITHDRAWN:
        db.execute(
            """UPDATE invitation SET status=?, data=json_set(data, '$.note', ?)
               WHERE trial_id=? AND candidate_id=?""",
            (WITHDRAWN, note, trial.trial_id, candidate_id),
        )
        _bump(db, trial.trial_id)
    return {
        **Invitation(candidate_id, current.arm, current.channel_id, WITHDRAWN).payload(),
        "note": note,
        "version": get_trial(db, trial.trial_id).version,
    }


def pending_invitations(db: Connection, trial_id: str) -> list[Invitation]:
    return [item for item in invitations(db, trial_id) if item.status == PENDING]


def recruited_by_target(db: Connection, trial: Trial) -> dict:
    assigned = roster(db, trial.trial_id)
    policy = trial.plan.policy
    counts: dict[tuple[str, str, str | None, str], dict] = {}
    for market in trial.plan.markets:
        for cell in market.cells:
            targets = (
                channel_unit_targets(
                    policy.required_units_per_arm,
                    [(item.channel_id, item.weight_bp) for item in cell.channels],
                )
                if cell.channels is not None
                else {None: policy.required_units_per_arm}
            )
            for channel, required in targets.items():
                for arm in ARMS:
                    counts[(market.market_id, cell.key, channel, arm)] = {
                        "market": market.market_id,
                        "cell": cell.key,
                        "channel": channel,
                        "arm": arm,
                        "weight_bp": weight_bp(cell, channel),
                        "target_units": required,
                        "recruited": 0,
                        "pending": 0,
                    }
    for unit in assigned.units.values():
        market, cell = assigned.placement(unit)
        key = (market, cell, unit.channel_id, unit.arm)
        if key in counts:
            counts[key]["recruited"] += 1
    candidates = candidate_map(db, trial.trial_id)
    for item in pending_invitations(db, trial.trial_id):
        candidate = candidates.get(item.candidate_id)
        if candidate is None:
            continue
        market, cell = cell_definition(
            trial.plan, assigned, candidate.site_id, candidate.load
        )
        if market is None or cell is None:
            continue
        key = (market.market_id, cell.key, item.channel_id, item.arm)
        if key in counts:
            counts[key]["pending"] += 1
    return counts


def weight_bp(cell, channel: str | None) -> int:
    if channel is None or cell.channels is None:
        return 10_000
    return next(
        (item.weight_bp for item in cell.channels if item.channel_id == channel), 0
    )


def recruitment_readout(db: Connection, trial: Trial) -> dict:
    counts = recruited_by_target(db, trial)
    markets: dict[str, dict[str, dict]] = {}
    gaps = []
    for key, value in sorted(
        counts.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "", item[0][3])
    ):
        shortfall = max(0, value["target_units"] - value["recruited"])
        cell = markets.setdefault(value["market"], {}).setdefault(
            value["cell"], {"cell": value["cell"], "channels": {}}
        )
        channel = cell["channels"].setdefault(
            value["channel"] or "",
            {
                "weight_bp": value["weight_bp"],
                "arms": {arm: {"target_units": 0, "recruited": 0, "pending": 0, "shortfall": 0}
                         for arm in ARMS},
            },
        )
        channel["arms"][value["arm"]] = {
            "target_units": value["target_units"],
            "recruited": value["recruited"],
            "pending": value["pending"],
            "shortfall": shortfall,
        }
        if shortfall:
            gaps.append(
                {
                    "market": value["market"],
                    "cell": value["cell"],
                    "channel": value["channel"],
                    "arm": value["arm"],
                    "enrolled": value["recruited"],
                    "pending": value["pending"],
                    "required": value["target_units"],
                }
            )
    return {
        "markets": [
            {"market_id": market, "cells": [cells[key] for key in sorted(cells)]}
            for market, cells in sorted(markets.items())
        ],
        "gaps": gaps,
    }
