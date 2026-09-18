"""
Benchmark: compares penalty scores across three sources —
  1. The organizer's own sample submission (Scenario A only — it's the
     only scenario we have a real answer key for).
  2. src/greedy_scheduler.py's own output (the safety net).
  3. src/cpsat_engine.py's own output (the hybrid engine).

This is an EVALUATION script (penalty-score comparison), not a fast
regression check, so it's kept separate from the main suite — but since
the 2026-09-19 constraints.py caching fix it's no longer actually slow:
all 3 scenarios x both engines run in a few seconds combined (was
~30-85s on Scenario B alone before that fix — see src/cpsat_engine.py's
Review point 5 for the profiling writeup). Still fine to run on demand
rather than every commit.

What it asserts (not just prints): src/cpsat_engine.py's own "never
regress vs. the safety net" design (see its `_score()`/`schedule()`) means
its output must NEVER score worse than plain greedy's, on every scenario,
by construction — this test is the one place that actually verifies that
promise held, rather than just trusting the code comment.

Run: python tests/test_benchmark.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_model import load_instance
from src.constraints import SCENARIO_CONFIGS
from src.output_writer import write_submission
from src.self_check import build_report
import src.greedy_scheduler as greedy_scheduler
import src.cpsat_engine as cpsat_engine

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "03_submission_sample"
CPSAT_TIME_LIMIT = 10.0  # solver's own budget per scenario — see src/cpsat_engine.py Review point 5
# for why this doesn't bound this script's OWN total runtime.


def score_state(instance, scenario_name, state):
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = write_submission(state, instance, scenario_name, Path(tmp))
        return build_report(instance, out_dir)


def main():
    instance = load_instance()
    failures = []
    rows = []

    sample_report = build_report(instance, SAMPLE_DIR)
    rows.append(("A", "organizer sample", sample_report))

    for scenario_name in ("A", "B", "C"):
        scenario = SCENARIO_CONFIGS[scenario_name]

        t0 = time.time()
        greedy_state = greedy_scheduler.schedule(instance, scenario)
        greedy_report = score_state(instance, scenario_name, greedy_state)
        greedy_time = time.time() - t0
        rows.append((scenario_name, "greedy", greedy_report, greedy_time))

        t0 = time.time()
        cpsat_state = cpsat_engine.schedule(instance, scenario, time_limit_seconds=CPSAT_TIME_LIMIT)
        cpsat_report = score_state(instance, scenario_name, cpsat_state)
        cpsat_time = time.time() - t0
        rows.append((scenario_name, "cpsat", cpsat_report, cpsat_time))

        # The one thing this benchmark actually asserts: cpsat_engine's own
        # "pick whichever genuinely scores better" design must never let it
        # regress vs. plain greedy — see src/cpsat_engine.py's schedule().
        greedy_violations = len(greedy_report["hard_violations"])
        cpsat_violations = len(cpsat_report["hard_violations"])
        if cpsat_violations > greedy_violations:
            failures.append(f"Scenario {scenario_name}: cpsat has MORE hard violations than greedy ({cpsat_violations} > {greedy_violations})")
        elif cpsat_violations == greedy_violations:
            greedy_obj = greedy_report.get("objective_score", float("inf"))
            cpsat_obj = cpsat_report.get("objective_score", float("inf"))
            # objective_score only exists when feasible; fall back to priority_weighted_score
            # (still meaningful even when infeasible, e.g. Scenario B's residual planned_date violations)
            if "objective_score" not in greedy_report:
                greedy_obj = greedy_report["soft_scores"]["priority_weighted_score"]
                cpsat_obj = cpsat_report["soft_scores"]["priority_weighted_score"]
            if cpsat_obj > greedy_obj:
                failures.append(f"Scenario {scenario_name}: cpsat scores worse than greedy at equal violation count ({cpsat_obj} > {greedy_obj})")

    # priority_weighted_score is always 0 for Scenario B by construction (no overrun term in
    # its objective, PS1_README §2.5) — objective_score is the number that's actually comparable
    # across all 3 scenarios (A: priority_weighted_score; B: excess+eclo term; C: both summed).
    print(f"{'scenario':<10}{'source':<20}{'feasible':<10}{'violations':<12}{'priority_weighted':<20}{'objective':<12}{'time(s)':<10}")
    for row in rows:
        if len(row) == 3:
            scenario_name, source, report = row
            time_str = "-"
        else:
            scenario_name, source, report, elapsed = row
            time_str = f"{elapsed:.1f}"
        pws = report["soft_scores"]["priority_weighted_score"]
        objective = report.get("objective_score", "n/a")
        print(f"{scenario_name:<10}{source:<20}{str(report['feasible']):<10}{len(report['hard_violations']):<12}{pws:<20}{str(objective):<12}{time_str:<10}")

    print()
    print("Scenario A vs. the organizer's own sample (only scenario with a real answer key):")
    sample_pws = sample_report["soft_scores"]["priority_weighted_score"]
    for scenario_name, source, report, *_ in [r for r in rows if r[0] == "A" and r[1] != "organizer sample"]:
        diff = round(report["soft_scores"]["priority_weighted_score"] - sample_pws, 4)
        verdict = f"beats sample by {-diff}" if diff < 0 else (f"trails sample by {diff}" if diff > 0 else "matches sample exactly")
        print(f"  {source}: {report['soft_scores']['priority_weighted_score']} vs sample's {sample_pws} ({verdict})")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("\ncpsat_engine.py never scored worse than greedy_scheduler.py on any scenario — the safety-net guarantee holds.")


if __name__ == "__main__":
    main()
