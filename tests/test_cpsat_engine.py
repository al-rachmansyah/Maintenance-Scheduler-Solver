"""
Validates src/cpsat_engine.py against the real instance for all three
scenarios. Same safety-net CONTRACT as tests/test_greedy_scheduler.py —
100% workload always placed, zero hard violations of ANY kind on all 3
scenarios (as of 2026-09-19's "one access per activity per week"
reversal, see docs/decisions.md — no scenario needs a relaxation on the
real instance anymore), and repeats within a week (legitimate again as of
2026-09-20's default flip — see constraints.py's ScenarioConfig
docstring) must stay self-consistent (same locations/co_share_group as
their earlier visit that week). This engine must hold all of it too, regardless of
whether the CP-SAT-informed heuristic or the greedy fallback ends up
winning `schedule()`'s internal comparison (see src/cpsat_engine.py's
`_score()`).

Also checks the interface contract itself: `schedule(instance, scenario)`
with the SAME positional signature as `greedy_scheduler.schedule` (a
caller must be able to swap engines by only changing the import).

Slower than most tests here — greedy_scheduler's own runtime dominates
(see src/cpsat_engine.py Review point 5), independent of the CP-SAT
solver's own time_limit_seconds. Expect Scenario B in particular to take
tens of seconds. tests/test_benchmark.py is the place for a full
score comparison against greedy and the sample submission; this test is
just the correctness contract.

Run: python tests/test_cpsat_engine.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_model import load_instance
from src.constraints import SCENARIO_CONFIGS
from src.cpsat_engine import schedule
from src.output_writer import write_submission
from src.self_check import build_report

SAMPLE_PROXIMITY_PAIRS = 14  # what the organizer's own sample scores on constraints.buffer_proximity_pairs

_TIME_LIMIT = 10.0  # solver's own per-scenario budget; kept short since the real bottleneck is greedy, not the solve


def main():
    instance = load_instance()
    total_needed = float(instance.activities["total_accesses"].sum())
    failures = []

    for scenario_name, allowed_tags in (("A", set()), ("B", set()), ("C", set())):
        scenario = SCENARIO_CONFIGS[scenario_name]
        state = schedule(instance, scenario, time_limit_seconds=_TIME_LIMIT)

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

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = write_submission(state, instance, scenario_name, Path(tmp))
            report = build_report(instance, out_dir)

        bad_tags = sorted(set(v["rule"] for v in report["hard_violations"]) - allowed_tags)
        if bad_tags:
            failures.append(f"Scenario {scenario_name}: unexpected hard violation tags {bad_tags} (allowed: {sorted(allowed_tags)})")

        proximity = report["detail"]["buffer_proximity_pairs"]  # soft hedge — see test_greedy_scheduler.py
        if len(proximity) > SAMPLE_PROXIMITY_PAIRS:
            failures.append(f"Scenario {scenario_name}: {len(proximity)} buffer-proximity pairs > the sample's {SAMPLE_PROXIMITY_PAIRS}: {proximity}")

        print(f"Scenario {scenario_name}: {len(state.accesses)} accesses, feasible={report['feasible']}, " f"hard_violations={len(report['hard_violations'])} (tags={sorted(set(v['rule'] for v in report['hard_violations']))}), " f"buffer_proximity_pairs={len(proximity)}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("\ncpsat_engine.py holds the safety-net contract on all 3 scenarios.")


if __name__ == "__main__":
    main()
