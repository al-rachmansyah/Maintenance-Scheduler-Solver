"""
Validates resolve_activity_path() against ground truth: the sample
submission's own SCHEDULE_OCCUPANCY.csv, which lists exactly which
locations the organizer's reference solution booked for every activity,
every week it was active.

Logic: for each activity, its booked location SET should be identical every
week it appears (an activity doesn't change which sectors/platforms it
occupies week to week — only whether it's active that week at all). So we
can take the union of location_ids per activity across all its sample rows,
confirm it's really the same set every week, and compare that single set
against what our resolver computes from start/end alone.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.data_model import load_instance, resolve_activity_path

SAMPLE_OCCUPANCY = (
    Path(__file__).resolve().parent.parent
    / "data" / "sample_instance" / "03_submission_sample" / "SCHEDULE_OCCUPANCY.csv"
)


def main():
    instance = load_instance()
    occupancy = pd.read_csv(SAMPLE_OCCUPANCY)

    mismatches = []
    inconsistent_weeks = []
    checked = 0

    for _, activity in instance.activities.iterrows():
        activity_id = activity["activity_id"]
        rows = occupancy[occupancy["activity_id"] == activity_id]
        if rows.empty:
            # Not every activity necessarily appears in the sample submission
            # (it's Scenario A only) — skip rather than fail.
            continue

        # Confirm the "same set every week" assumption above actually holds
        # in the ground-truth data before trusting it as our oracle.
        sets_per_week = rows.groupby("week")["location_id"].apply(frozenset).unique()
        if len(sets_per_week) != 1:
            inconsistent_weeks.append(activity_id)
            continue

        expected = set(sets_per_week[0])
        actual = set(resolve_activity_path(activity["start_location_id"], activity["end_location_id"], instance))

        checked += 1
        if actual != expected:
            mismatches.append({
                "activity_id": activity_id,
                "start": activity["start_location_id"],
                "end": activity["end_location_id"],
                "missing_from_ours": expected - actual,
                "extra_in_ours": actual - expected,
            })

    print(f"Checked {checked} activities against the sample submission's ground truth.")
    if inconsistent_weeks:
        print(f"NOTE: {len(inconsistent_weeks)} activities book different locations in different "
              f"weeks in the sample ({inconsistent_weeks}) — assumption doesn't hold for these, "
              f"skipped rather than silently trusted.")

    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH(ES):")
        for m in mismatches:
            print(f"  {m}")
        sys.exit(1)
    else:
        print("All resolved paths match the sample submission's ground truth exactly.")


if __name__ == "__main__":
    main()