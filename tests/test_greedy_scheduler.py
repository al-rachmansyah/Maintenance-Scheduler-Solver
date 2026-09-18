"""
Validates src/greedy_scheduler.py against the real instance for all three
scenarios. Unlike the other tests, there's no ground-truth answer key to
diff against here — the greedy scheduler's own output is not expected to
match the sample submission's schedule (different algorithm, different
choices). What's checked is the safety-net CONTRACT itself:

  - 100% of workload is always placed, in every scenario (PS1_README §1
    non-negotiable #1) — total access-nights scheduled must equal the sum
    of total_accesses across all 54 activities, regardless of scenario.
  - Every hard rule holds, zero exceptions, on all 3 scenarios. Earlier
    versions of this test allowed Scenario B to carry residual
    `planned_date` violations (its last-resort relaxation firing on a
    couple of tight contracts) — as of 2026-09-19's "one access per
    activity per week" reversal (see docs/decisions.md), that no longer
    happens on the real instance: all three scenarios come back fully
    feasible. Tightened this test to expect exactly that, so a future
    regression back to needing the relaxation is caught, not silently
    allowed.
  - An activity CAN have more than one access in the same week (the
    default, permissive reading of PS1_README §2.4 rule 10 as of
    2026-09-20 — see `ScenarioConfig.one_access_per_activity_week`'s
    docstring in constraints.py) — what's actually still invalid is a
    repeat landing on DIFFERENT locations or a different co_share_group
    than its earlier visit that week (the same possession must stay
    self-consistent); this test checks for that, not for the mere
    existence of a repeat.

Run: python tests/test_greedy_scheduler.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_model import load_instance
from src.constraints import SCENARIO_CONFIGS
from src.greedy_scheduler import schedule
from src.output_writer import write_submission
from src.self_check import build_report

SAMPLE_PROXIMITY_PAIRS = 14  # what the organizer's own sample scores on constraints.buffer_proximity_pairs


def main():
    instance = load_instance()
    total_needed = float(instance.activities["total_accesses"].sum())
    failures = []

    for scenario_name, allowed_tags in (("A", set()), ("B", set()), ("C", set())):
        scenario = SCENARIO_CONFIGS[scenario_name]
        state = schedule(instance, scenario)

        total_yield = sum(1.5 if a.eclo else 1.0 for a in state.accesses)
        if total_yield < total_needed:
            failures.append(f"Scenario {scenario_name}: only {total_yield}/{total_needed} total yield placed — Rule 1 violated")

        # Repeats within a week are fine now — inconsistency isn't.
        seen: dict[tuple[str, int], tuple] = {}
        for a in state.accesses:
            key = (a.activity_id, a.week)
            fingerprint = (a.locations, tuple(sorted(a.co_share_group.items())))
            if key in seen and seen[key] != fingerprint:
                failures.append(f"Scenario {scenario_name}: {a.activity_id} wk{a.week} has inconsistent locations/co_share_group across its own repeat accesses that week")
            seen[key] = fingerprint

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = write_submission(state, instance, scenario_name, Path(tmp))
            report = build_report(instance, out_dir)

        bad_tags = sorted(set(v["rule"] for v in report["hard_violations"]) - allowed_tags)
        if bad_tags:
            failures.append(f"Scenario {scenario_name}: unexpected hard violation tags {bad_tags} (allowed: {sorted(allowed_tags)})")

        # Soft buffer-proximity hedge. At the permissive default (one_access_per_activity_week=False)
        # this is free on the real instance — measured 0 pairs, all 3 scenarios, at no objective cost
        # (see docs/decisions.md). Regression bar stays "no worse than the organizer's sample" (14
        # pairs) rather than a hard-coded 0, since a hidden instance might not let it go all the way.
        proximity = report["detail"]["buffer_proximity_pairs"]
        if len(proximity) > SAMPLE_PROXIMITY_PAIRS:
            failures.append(f"Scenario {scenario_name}: {len(proximity)} buffer-proximity pairs > the sample's {SAMPLE_PROXIMITY_PAIRS}: {proximity}")

        print(f"Scenario {scenario_name}: {len(state.accesses)} accesses, feasible={report['feasible']}, " f"hard_violations={len(report['hard_violations'])} (tags={sorted(set(v['rule'] for v in report['hard_violations']))}), " f"buffer_proximity_pairs={len(proximity)}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("\ngreedy_scheduler.py holds the safety-net contract on all 3 scenarios.")


if __name__ == "__main__":
    main()
