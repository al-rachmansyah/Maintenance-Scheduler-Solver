"""
Validates src/constraints.py against ground truth: replays the sample
submission (a known-FEASIBLE Scenario A answer) through every check_*
function and asserts zero hard violations, full stop. If this ever fails,
the bug is almost certainly in constraints.py, not in the sample data.

Previously carved out "closure" (buffer/mirroring) violations as a known,
accepted discrepancy (34 false violations against this exact sample) —
that discrepancy is RESOLVED as of 2026-09-19, not just tolerated: the
buffer-sector-extension reading was found to be actively wrong (not
merely stricter-than-necessary) after it turned out to be the dominant
cause of badly-degraded scheduling quality on the real instance. See
docs/decisions.md's "Buffer/exclusion-zone rule" entries (both the
original discrepancy and the reversal) for the full evidence chain. No
carve-out needed anymore — every rule, including "closure", must now
report zero violations against this sample.

Submission-loading (CSV -> ScheduledAccess) lives in src/self_check.py
(load_submission_csvs/build_scheduled_accesses) — this test imports it
rather than keeping its own copy, so production code and tests can't
silently drift apart on how a submission is parsed.

Run: python tests/test_constraints.py
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_model import load_instance
from src.constraints import (
    ScheduleState,
    SCENARIO_CONFIGS,
    check_all,
    check_predecessor_acyclic,
    buffer_proximity_pairs,
    check_one_access_per_activity_week,
)
from src.self_check import load_submission_csvs, build_scheduled_accesses

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "03_submission_sample"


def main():
    instance = load_instance()

    acyclic = check_predecessor_acyclic(instance)
    print(f"Predecessor acyclicity check on the real instance: {'ok' if acyclic.legal else 'FAILED - ' + acyclic.reason}")
    if not acyclic.legal:
        sys.exit(1)

    access_df, occupancy_df, _ = load_submission_csvs(SAMPLE_DIR)
    accesses = build_scheduled_accesses(access_df, occupancy_df, instance)
    scenario = SCENARIO_CONFIGS["A"]

    state = ScheduleState(instance)
    failures = []

    for candidate in accesses:
        results = check_all(candidate, state, instance, scenario)
        for r in results:
            if not r.legal:
                failures.append((candidate.activity_id, candidate.week, r))
        state.add(candidate)

    print(f"Replayed {len(accesses)} accesses from the sample submission through every check_* function.")

    if failures:
        print(f"\n{len(failures)} HARD VIOLATION(S) — sample should be feasible, so this is a bug in constraints.py:")
        for activity_id, week, r in failures:
            print(f"  [{r.rule}] {activity_id} wk{week}: {r.reason}")
        sys.exit(1)
    else:
        print("\nZero hard violations. constraints.py agrees the sample submission is feasible.")

    # Soft buffer-proximity hedge (constraints.py "Rule 4, SOFT companion") —
    # never a hard check, but the sample gives it a concrete, independently
    # verifiable baseline: exactly the 14 (pair, week) rows the user's
    # row-by-row review (docs/buffer_rule_violations_reviewed.csv) classified
    # as "possible_conflict_night_unknown" (overlapping buffer footprints, no
    # co-share connection at all). If this number moves, either the footprint
    # logic or the co-share-connectivity logic changed — investigate.
    pairs = buffer_proximity_pairs(state, instance)
    expected_unknown_rows = 14
    if len(pairs) != expected_unknown_rows:
        print(f"\nFAILED: expected {expected_unknown_rows} buffer-proximity pairs in the sample (the user's reviewed 'unknown' rows), got {len(pairs)}: {pairs}")
        sys.exit(1)
    print(f"Sample has {len(pairs)} buffer-proximity pairs (soft) — matches the 14 'possible_conflict_night_unknown' rows from the manual review.")

    # One access per activity per week (PS1_README §2.4 rule 10 — see constraints.py's
    # ScenarioConfig docstring for why this is OFF by default as of 2026-09-20):
    # the opt-in strict config must reject a second same-week access; the
    # default (permissive) config must not.
    first = accesses[0]
    second = replace(first, access_night=first.access_night % 3 + 1, access_seq=first.access_seq + 100)
    probe_state = ScheduleState(instance)
    probe_state.add(first)
    permissive = SCENARIO_CONFIGS["A"]
    strict = replace(permissive, one_access_per_activity_week=True)
    if check_one_access_per_activity_week(second, probe_state, strict).legal:
        print("\nFAILED: strict config accepted a second access for the same activity in the same week")
        sys.exit(1)
    if not check_one_access_per_activity_week(second, probe_state, permissive).legal:
        print("\nFAILED: permissive config rejected a same-week repeat")
        sys.exit(1)
    print("One-access-per-activity-week: strict config rejects a same-week repeat, permissive config allows it.")


if __name__ == "__main__":
    main()
