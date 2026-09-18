"""
Smoke test — Phase 0/1 groundwork.

Goal: prove the environment + the 8 instance CSVs are loadable and internally
consistent BEFORE we write a single line of scheduling logic. This is not the
real self-check validator (that comes in Phase 2/3, once we have output to
check) — it only checks that the *input* data means what we think it means.

Run: source .venv/bin/activate && python3 tests/smoke_test.py
"""

import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "01_data"

FILES = {
    "lines": "01_LINES.csv",
    "stations": "02_STATIONS.csv",
    "sectors": "03_SECTORS.csv",
    "location_supply": "04_LOCATION_SUPPLY.csv",
    "buffer_location": "05_BUFFER_LOCATION.csv",
    "parameters": "06_PARAMETERS.csv",
    "contracts": "07_PROJECT_DETAILS.csv",
    "activities": "08_ACTIVITY_DETAILS.csv",
}

failures = []


def check(condition, message):
    """Record and print the result of a smoke-test condition."""
    if not condition:
        failures.append(message)
        print(f"  [FAIL] {message}")
    else:
        print(f"  [ok]   {message}")


def main():
    print(f"Loading instance from: {DATA_DIR}")
    dfs = {}
    for key, filename in FILES.items():
        path = DATA_DIR / filename
        check(path.exists(), f"{filename} exists")
        dfs[key] = pd.read_csv(path)

    lines, stations, sectors, supply, buffers, params, contracts, activities = (
        dfs["lines"], dfs["stations"], dfs["sectors"], dfs["location_supply"],
        dfs["buffer_location"], dfs["parameters"], dfs["contracts"], dfs["activities"],
    )

    print("\n--- Row counts ---")
    for key, df in dfs.items():
        print(f"  {key:16s} {len(df):4d} rows")

    print("\n--- Referential integrity ---")

    # Every activity's start/end location must be a real, known location.
    known_locations = set(supply["location_id"])
    bad_starts = set(activities["start_location_id"]) - known_locations
    bad_ends = set(activities["end_location_id"]) - known_locations
    check(not bad_starts, f"all start_location_id values exist in LOCATION_SUPPLY (bad: {bad_starts})")
    check(not bad_ends, f"all end_location_id values exist in LOCATION_SUPPLY (bad: {bad_ends})")

    # start/end location_ids in ACTIVITY_DETAILS are SECTOR ids, not station ids —
    # confirm every one of them is a "tunnel sector" kind, never a platform.
    sector_kind = supply.set_index("location_id")["location_kind"]
    non_sector_starts = [s for s in activities["start_location_id"] if sector_kind.get(s) != "tunnel sector"]
    non_sector_ends = [s for s in activities["end_location_id"] if sector_kind.get(s) != "tunnel sector"]
    check(not non_sector_starts, f"every activity start_location_id is a tunnel sector (bad: {non_sector_starts})")
    check(not non_sector_ends, f"every activity end_location_id is a tunnel sector (bad: {non_sector_ends})")

    # Every activity's contract_number must exist in PROJECT_DETAILS.
    known_contracts = set(contracts["contract_number"])
    bad_contracts = set(activities["contract_number"]) - known_contracts
    check(not bad_contracts, f"all activity contract_number values exist in PROJECT_DETAILS (bad: {bad_contracts})")

    # predecessor_activity_id (undocumented in PS1_README's rules list, but present
    # in the real data) must always point at another real activity_id.
    preds = activities["predecessor_activity_id"].dropna()
    known_activities = set(activities["activity_id"])
    bad_preds = set(preds) - known_activities
    check(not bad_preds, f"all predecessor_activity_id values point at real activities (bad: {bad_preds})")
    print(f"  [info] {len(preds)}/{len(activities)} activities declare a predecessor — "
          f"this is a real hard-ish rule not listed in PS1_README §2.4, we must still honor it.")

    # nature_of_activity must be one of the three BUFFER_LOCATION categories.
    known_natures = set(buffers["nature_of_works"])
    bad_natures = set(contracts["nature_of_activity"]) - known_natures
    check(not bad_natures, f"all contract nature_of_activity values are known buffer categories (bad: {bad_natures})")

    # Live contracts should carry weekly cap 2, everyone else 3 (per PS1_README §2.3) —
    # but we READ the actual column rather than hardcoding this, and just confirm
    # our understanding matches this instance.
    live_caps = contracts.loc[contracts["nature_of_activity"] == "Live", "number_of_maximum_access_per_week"]
    other_caps = contracts.loc[contracts["nature_of_activity"] != "Live", "number_of_maximum_access_per_week"]
    check((live_caps == 2).all(), f"Live contracts all have weekly cap 2 (found: {sorted(live_caps.unique())})")
    check((other_caps == 3).all(), f"non-Live contracts all have weekly cap 3 (found: {sorted(other_caps.unique())})")

    # access_type must be one of PM/PC/C.
    check(set(contracts["access_type"]) <= {"PM", "PC", "C"},
          f"all access_type values are PM/PC/C (found: {set(contracts['access_type'])})")

    print("\n--- Horizon ---")
    param_map = dict(zip(params["key"], params["value"]))
    print(f"  horizon_start={param_map.get('horizon_start')}  horizon_weeks={param_map.get('horizon_weeks')}")

    print("\n--- Network shape ---")
    check(len(stations) == 20, f"20 station rows (8+2 per line x 2 lines) — found {len(stations)}")
    check(len(sectors) == 18, f"18 sector rows (9 edges per line x 2 lines) — found {len(sectors)}")
    n_tunnel = (supply["location_kind"] == "tunnel sector").sum()
    n_platform = (supply["location_kind"] == "platform sector").sum()
    check(n_tunnel == 36, f"36 tunnel-sector locations (18 sectors x 2 bounds) — found {n_tunnel}")
    check(n_platform == 40, f"40 platform-sector locations (20 stations x 2 bounds) — found {n_platform}")

    print("\n--- Summary ---")
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All checks passed. Environment + data understanding confirmed.")


if __name__ == "__main__":
    main()
