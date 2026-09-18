"""
Validates src/self_check.py against ground truth: builds a report for the
sample submission and checks it against values hand-verified this session
directly from the sample's own RESULTS.csv + 07_PROJECT_DETAILS.csv.

Previously carved out "closure" (buffer/mirroring) violations here, same
as tests/test_constraints.py — that discrepancy is RESOLVED as of
2026-09-19 (see docs/decisions.md's "Buffer/exclusion-zone rule" entries),
so this now asserts zero hard violations of ANY kind against the sample.

Run: python tests/test_self_check.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.data_model import load_instance
from src.constraints import check_predecessor_acyclic, check_workload_conservation_all, ScheduleState
from src.self_check import build_report, scenario_from_results, load_submission_csvs, build_scheduled_accesses

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "03_submission_sample"


def main():
    instance = load_instance()
    failures = []

    report = build_report(instance, SAMPLE_DIR)

    if report["scenario"] != "A":
        failures.append(f"scenario: expected 'A', got {report['scenario']!r}")

    if report["hard_violations"]:
        failures.append(f"unexpected hard violations against the feasible sample: {report['hard_violations']}")
    print(f"hard_violations: {len(report['hard_violations'])} (expected 0)")

    soft = report["soft_scores"]
    # Hand-verified this session: C006/C010/C014 are the only overrunning
    # contracts in the sample (14/7/7 days), all contract_priority 3.
    # C004/C007/C012 finish exactly 7 days EARLY each (raw diff -7), which
    # RESULTS.csv's own clamped-at-0 column can't reveal but self_check's
    # independent recomputation does -- confirmed by hand via
    # src.self_check._contract_overrun_days before writing this assertion.
    expected = {
        "overrun_days_total": 28,
        "contracts_overrunning": 3,
        "earliness_days_total": 21,
        "eclo_nights_total": 0,
        "priority_overrun": {"1": 0, "2": 0, "3": 28},
    }
    for key, value in expected.items():
        if soft[key] != value:
            failures.append(f"soft_scores[{key!r}]: expected {value!r}, got {soft[key]!r}")

    if report["detail"].get("results_csv_mismatches"):
        failures.append(f"unexpected results_csv_mismatches: {report['detail']['results_csv_mismatches']}")

    if report["detail"]["nights_scheduled"] != 192:
        failures.append(f"detail.nights_scheduled: expected 192, got {report['detail']['nights_scheduled']}")

    # Whole-instance checks, independently, on the real instance data
    acyclic = check_predecessor_acyclic(instance)
    if not acyclic.legal:
        failures.append(f"check_predecessor_acyclic failed on real instance: {acyclic.reason}")

    access_df, occupancy_df, _ = load_submission_csvs(SAMPLE_DIR)
    accesses = build_scheduled_accesses(access_df, occupancy_df, instance)
    state = ScheduleState(instance)
    for a in accesses:
        state.add(a)
    workload_results = check_workload_conservation_all(state, instance)
    workload_failures = [r for r in workload_results if not r.legal]
    if workload_failures:
        failures.append(f"check_workload_conservation_all found violations on the feasible sample: {[r.reason for r in workload_failures]}")

    # Malformed input: a RESULTS.csv mixing two scenarios must raise
    bad_results = pd.DataFrame({"scenario": ["A", "B"], "contract_number": ["C001", "C002"], "simulated_completion_date": ["2027-01-01", "2027-01-01"], "overrun_days": [0, 0]})
    raised = False
    try:
        scenario_from_results(bad_results)
    except ValueError:
        raised = True
    if not raised:
        failures.append("scenario_from_results did not raise on a mixed-scenario RESULTS.csv")

    print(f"soft_scores: {soft}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("\nself_check.py matches every hand-verified value on the real sample.")


if __name__ == "__main__":
    main()
