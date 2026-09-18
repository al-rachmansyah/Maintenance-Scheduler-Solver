"""
Validates src/output_writer.py by round-tripping the sample submission:
load its 3 CSVs, replay into a ScheduleState, write it back out, and
assert the result matches the shipped ground truth exactly.

Run: python tests/test_output_writer.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.data_model import load_instance
from src.constraints import ScheduleState
from src.self_check import load_submission_csvs, build_scheduled_accesses
from src.output_writer import write_submission

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "03_submission_sample"


def row_set(df: pd.DataFrame, columns: list[str]) -> set[tuple]:
    return set(map(tuple, df[columns].values.tolist()))


def main():
    instance = load_instance()
    access_df, occupancy_df, _ = load_submission_csvs(SAMPLE_DIR)
    accesses = build_scheduled_accesses(access_df, occupancy_df, instance)

    state = ScheduleState(instance)
    for a in accesses:
        state.add(a)

    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = write_submission(state, instance, "A", Path(tmp))

        written_access = pd.read_csv(out_dir / "SCHEDULE_ACCESS.csv")
        written_occupancy = pd.read_csv(out_dir / "SCHEDULE_OCCUPANCY.csv")
        written_results = pd.read_csv(out_dir / "RESULTS.csv")

        orig_access = pd.read_csv(SAMPLE_DIR / "SCHEDULE_ACCESS.csv")
        orig_occupancy = pd.read_csv(SAMPLE_DIR / "SCHEDULE_OCCUPANCY.csv")
        orig_results = pd.read_csv(SAMPLE_DIR / "RESULTS.csv")

        # Header-exactness (PS1_README §2.6, copied verbatim)
        if list(written_access.columns) != ["activity_id", "access_seq", "week", "eclo", "access_night"]:
            failures.append(f"SCHEDULE_ACCESS.csv header mismatch: {list(written_access.columns)}")
        if list(written_occupancy.columns) != ["activity_id", "week", "location_id", "co_share_group"]:
            failures.append(f"SCHEDULE_OCCUPANCY.csv header mismatch: {list(written_occupancy.columns)}")
        if list(written_results.columns) != ["scenario", "contract_number", "simulated_completion_date", "overrun_days"]:
            failures.append(f"RESULTS.csv header mismatch: {list(written_results.columns)}")

        # eclo must round-trip as 0/1 ints, not "True"/"False" strings
        if written_access["eclo"].dtype.kind not in "iu":
            failures.append(f"SCHEDULE_ACCESS.csv 'eclo' column is not integer dtype: {written_access['eclo'].dtype}")

        # Row-set equality (order-independent) for the two schedule files
        access_cols = ["activity_id", "access_seq", "week", "eclo", "access_night"]
        if row_set(written_access, access_cols) != row_set(orig_access, access_cols):
            failures.append("SCHEDULE_ACCESS.csv row-set does not match the sample")

        occupancy_cols = ["activity_id", "week", "location_id", "co_share_group"]
        if row_set(written_occupancy, occupancy_cols) != row_set(orig_occupancy, occupancy_cols):
            failures.append("SCHEDULE_OCCUPANCY.csv row-set does not match the sample")

        # RESULTS.csv must match EXACTLY, row for row, for all 14 contracts
        w = written_results.sort_values("contract_number").reset_index(drop=True)
        o = orig_results.sort_values("contract_number").reset_index(drop=True)
        if len(w) != len(o):
            failures.append(f"RESULTS.csv row count mismatch: written {len(w)}, sample {len(o)}")
        else:
            for i in range(len(o)):
                if (
                    w.loc[i, "contract_number"] != o.loc[i, "contract_number"]
                    or w.loc[i, "simulated_completion_date"] != o.loc[i, "simulated_completion_date"]
                    or int(w.loc[i, "overrun_days"]) != int(o.loc[i, "overrun_days"])
                ):
                    failures.append(f"RESULTS.csv mismatch for {o.loc[i, 'contract_number']}: written={dict(w.loc[i])} sample={dict(o.loc[i])}")

    print(f"Round-tripped {len(accesses)} accesses through write_submission() and compared against the sample's own 3 CSVs.")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("output_writer.py reproduces the sample submission exactly.")


if __name__ == "__main__":
    main()
