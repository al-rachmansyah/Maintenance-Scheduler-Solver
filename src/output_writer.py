"""
Output writer — Phase 1 remainder (Role B).

Turns a committed src/constraints.py ScheduleState into the 3 exact-schema
CSV files PS1_README.md §2.6 requires (SCHEDULE_ACCESS.csv,
SCHEDULE_OCCUPANCY.csv, RESULTS.csv). Only reads `state.accesses` (the
public ScheduledAccess list) + `instance` — makes no assumption about how
the schedule was built, since Phase 2 (the greedy scheduler) doesn't exist
yet. Any future scheduler that builds a ScheduleState by calling
constraints.check_all()-gated `.add()`s can hand it straight to
`write_submission()`.

======================================================================
REVIEW POINTS:
======================================================================

1. **`overrun_days` is clamped at 0** for an on-time or early contract
   (never negative) — user's explicit decision this session. The column
   name itself ("overrun") reads as non-negative; earliness is a
   report-level diagnostic (src/self_check.py's `earliness_days_total`),
   not something this CSV represents. One-line change here
   (`max(0, ...)` -> the raw signed diff) if the organizer's reference
   validator turns out to expect otherwise.

2. **`write_results` iterates every contract in `instance.contracts`**,
   not just contracts seen in `state` — a contract with zero accesses
   scheduled produces a loud `simulated_completion_date` equal to its own
   `planned_completion_date` (0 weeks scheduled -> falls back to the
   planned date itself, overrun 0) rather than a silently-missing row.
   This is a defensive default for an incomplete/partial ScheduleState
   (e.g. mid-development); a complete, feasible submission should never
   hit it, since Rule 1 requires every activity be scheduled.
"""

from pathlib import Path

import pandas as pd

from src.constraints import ScheduleState, week_end_date
from src.data_model import Instance

OUTPUTS_ROOT = Path(__file__).resolve().parent.parent / "outputs"


def write_schedule_access(state: ScheduleState, out_dir: Path) -> None:
    rows = [
        {
            "activity_id": a.activity_id,
            "access_seq": a.access_seq,
            "week": a.week,
            "eclo": int(a.eclo),
            "access_night": a.access_night,
        }
        for a in state.accesses
    ]
    df = pd.DataFrame(rows, columns=["activity_id", "access_seq", "week", "eclo", "access_night"])
    df = df.sort_values(["activity_id", "access_seq"]).reset_index(drop=True)
    df.to_csv(out_dir / "SCHEDULE_ACCESS.csv", index=False)


def write_schedule_occupancy(state: ScheduleState, out_dir: Path) -> None:
    """
    One row per (activity, week, location) — deduplicated. Since
    2026-09-19, an activity can have more than one access in the same
    week (see docs/decisions.md); when it does, both accesses book the
    SAME locations/groups (the same possession continuing — see
    `greedy_scheduler._choose_group_for_location`), which would otherwise
    produce byte-identical duplicate rows here, one per access. The
    schema (activity_id, week, location_id, co_share_group) has no
    access_seq column to distinguish them anyway, so it's the same
    occupancy fact either way — drop_duplicates keeps the file exactly
    as informative, just without the redundant repeats.
    """
    rows = [
        {"activity_id": a.activity_id, "week": a.week, "location_id": loc, "co_share_group": a.co_share_group[loc]}
        for a in state.accesses
        for loc in a.locations
    ]
    df = pd.DataFrame(rows, columns=["activity_id", "week", "location_id", "co_share_group"])
    df = df.drop_duplicates().sort_values(["activity_id", "week", "location_id"]).reset_index(drop=True)
    df.to_csv(out_dir / "SCHEDULE_OCCUPANCY.csv", index=False)


def write_results(state: ScheduleState, instance: Instance, scenario: str, out_dir: Path) -> None:
    activities = instance.activities

    rows = []
    for contract_number, contract in instance.contracts.set_index("contract_number").iterrows():
        contract_activity_ids = set(activities[activities["contract_number"] == contract_number]["activity_id"])
        weeks = [a.week for a in state.accesses if a.activity_id in contract_activity_ids]
        planned = pd.Timestamp(contract["planned_completion_date"]).date()

        if weeks:
            simulated = week_end_date(max(weeks), instance)
        else:
            # See Review point 2 — defensive default for an incomplete ScheduleState.
            simulated = planned

        overrun_days = max(0, (simulated - planned).days)  # see Review point 1
        rows.append(
            {
                "scenario": scenario,
                "contract_number": contract_number,
                "simulated_completion_date": simulated.isoformat(),
                "overrun_days": overrun_days,
            }
        )

    df = pd.DataFrame(rows, columns=["scenario", "contract_number", "simulated_completion_date", "overrun_days"])
    df = df.sort_values("contract_number").reset_index(drop=True)
    df.to_csv(out_dir / "RESULTS.csv", index=False)


def write_submission(state: ScheduleState, instance: Instance, scenario: str, out_dir: Path | None = None) -> Path:
    if out_dir is None:
        out_dir = OUTPUTS_ROOT / f"scenario_{scenario}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    write_schedule_access(state, out_dir)
    write_schedule_occupancy(state, out_dir)
    write_results(state, instance, scenario, out_dir)
    return out_dir
