"""
Validates src/api.py — the integration facade Role C's web app is meant to
import. Three things matter here that no other test covers:

  1. `load_instance_from_uploads` round-trips the sample instance from raw
     bytes (what a Streamlit `st.file_uploader` actually hands you), and
     raises a clean, user-presentable `SolveError` — not a bare exception —
     when a required file is missing.
  2. `validate_instance` accepts the real sample instance with zero errors,
     and actually catches a broken one (dangling contract reference).
  3. `solve()` returns a `SolveResult` whose numbers agree with calling the
     underlying engine/self_check/explain pipeline directly (this is a
     composition module — it must not silently transform the numbers) and
     whose DataFrames have the exact columns the output schema promises —
     checked for ALL THREE scenarios × both engines (not just Scenario A;
     an earlier version of this test only covered A, which would have
     missed a bug specific to B's date_hard/ECLO-forcing path or C's ECLO
     continuity window).

Run: python tests/test_api.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api import REQUIRED_INSTANCE_FILES, SolveError, load_instance_from_uploads, load_sample_instance, solve, validate_instance
from src.constraints import SCENARIO_CONFIGS
from src.data_model import DATA_DIR
from src.greedy_scheduler import schedule as greedy_schedule
from src.self_check import build_report, load_submission_csvs
from src.output_writer import write_submission

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"  [FAIL] {message}")
    else:
        print(f"  [ok]   {message}")


def main():
    instance = load_sample_instance()

    # --- 1. upload round-trip ---
    files = {name: (DATA_DIR / name).read_bytes() for name in REQUIRED_INSTANCE_FILES}
    uploaded = load_instance_from_uploads(files)
    check(len(uploaded.activities) == len(instance.activities), "uploaded instance has the same activity count as load_sample_instance()")
    check(len(uploaded.contracts) == len(instance.contracts), "uploaded instance has the same contract count")

    # differently-cased / prefixed filenames should still match
    loose = {name.lower().split("_", 1)[1]: data for name, data in files.items()}
    loose_instance = load_instance_from_uploads(loose)
    check(len(loose_instance.activities) == len(instance.activities), "loosely-named uploads (no numeric prefix, lowercase) still resolve")

    missing = dict(files)
    del missing["05_BUFFER_LOCATION.csv"]
    try:
        load_instance_from_uploads(missing)
        check(False, "missing file should raise SolveError")
    except SolveError as exc:
        check("05_BUFFER_LOCATION.csv" in str(exc), "missing-file SolveError names the missing file")

    # --- 2. validation ---
    report = validate_instance(instance)
    check(report.ok, f"sample instance validates with zero errors (got {report.errors})")

    broken = load_sample_instance()
    broken.activities = broken.activities.copy()
    broken.activities.loc[broken.activities.index[0], "contract_number"] = "C999_DOES_NOT_EXIST"
    broken_report = validate_instance(broken)
    check(not broken_report.ok, "a dangling contract_number reference is caught as an error")

    # --- 3. solve() agrees with the direct pipeline, for ALL 3 SCENARIOS x both engines ---
    import tempfile

    for scenario_name in ("A", "B", "C"):
        for engine in ("greedy", "cpsat"):
            result = solve(instance, scenario_name, engine=engine, time_limit_seconds=5.0)
            check(result.feasible, f"[{scenario_name}/{engine}] solve is feasible")
            check(result.metrics["hard_violation_count"] == 0, f"[{scenario_name}/{engine}] zero hard violations")
            check(set(result.schedule_access.columns) == {"activity_id", "access_seq", "week", "eclo", "access_night"}, f"[{scenario_name}/{engine}] SCHEDULE_ACCESS.csv columns match §2.6")
            check(set(result.schedule_occupancy.columns) == {"activity_id", "week", "location_id", "co_share_group"}, f"[{scenario_name}/{engine}] SCHEDULE_OCCUPANCY.csv columns match §2.6")
            check(set(result.results.columns) == {"scenario", "contract_number", "simulated_completion_date", "overrun_days"}, f"[{scenario_name}/{engine}] RESULTS.csv columns match §2.6")
            check(len(result.activity_timeline) == len(instance.activities), f"[{scenario_name}/{engine}] activity_timeline has one row per activity")
            check(result.metrics["objective_score"] == result.objective_score, f"[{scenario_name}/{engine}] metrics/objective_score agree")
            check(result.results["scenario"].eq(scenario_name).all(), f"[{scenario_name}/{engine}] RESULTS.csv rows all tag the requested scenario")

        # cross-check greedy's own numbers against calling greedy_scheduler + self_check directly
        direct_state = greedy_schedule(instance, SCENARIO_CONFIGS[scenario_name])
        with tempfile.TemporaryDirectory() as tmp:
            out = write_submission(direct_state, instance, scenario_name, Path(tmp))
            direct_report = build_report(instance, out)
        api_result = solve(instance, scenario_name, engine="greedy")
        check(api_result.metrics["priority_weighted_score"] == direct_report["soft_scores"]["priority_weighted_score"], f"[{scenario_name}] api.solve()'s greedy score matches calling greedy_scheduler+self_check directly")
        check(len(api_result.schedule_access) == len(direct_state.accesses), f"[{scenario_name}] api.solve()'s access count matches the direct pipeline")

    # Scenario C-specific: ECLO continuity window (§2.4 rule 10) only applies in C. A per-line
    # span > 2 calendar weeks would already show up as a hard violation in the loop above (this
    # instance's optimal C solution happens not to need ECLO at all — see eclo_nights_total in
    # tests/test_benchmark.py — so this just confirms the tag never fires, not that it's exercised).
    c_result = solve(instance, "C", engine="cpsat")
    check("eclo_continuity" not in {v["rule"] for v in c_result.report["hard_violations"]}, "[C] no eclo_continuity violations")

    # --- files()/zip export ---
    result = solve(instance, "A", engine="greedy")
    file_map = result.files()
    check(set(file_map) == {"SCHEDULE_ACCESS.csv", "SCHEDULE_OCCUPANCY.csv", "RESULTS.csv", "report.json"}, "files() returns exactly the 3 submission CSVs + report.json")
    zip_bytes = result.to_zip_bytes()
    check(len(zip_bytes) > 0, "to_zip_bytes() produces a non-empty archive")

    # --- opting into the strict reading produces a warning; the (permissive) default doesn't ---
    strict = solve(instance, "B", engine="greedy", strict_one_access_per_week=True)
    check(any("rule 10" in w for w in strict.warnings), "opting into strict_one_access_per_week attaches a warning")
    default = solve(instance, "B", engine="greedy")
    check(not default.warnings, "the default permissive config produces no warnings on the sample instance")

    # --- CLI on "someone else's data": `python -m src.api <dir> all --out <dir>` ---
    import shutil
    import tempfile as _tempfile

    from src.api import main as api_cli

    with _tempfile.TemporaryDirectory() as tmp:
        own_data = Path(tmp) / "own_data"
        shutil.copytree(DATA_DIR, own_data)
        out_root = Path(tmp) / "results"
        code = api_cli([str(own_data), "all", "--out", str(out_root)])
        check(code == 0, "CLI: `<dir> all` exits 0 when every scenario is feasible")
        for s in ("A", "B", "C"):
            produced = {p.name for p in (out_root / f"scenario_{s}").glob("*")}
            check(produced == {"SCHEDULE_ACCESS.csv", "SCHEDULE_OCCUPANCY.csv", "RESULTS.csv", "report.json"}, f"CLI: scenario_{s} has the 3 CSVs + report.json")
        check(api_cli([str(Path(tmp) / "does_not_exist"), "A"]) == 2, "CLI: a non-directory path exits 2 with a clean error")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("\nsrc/api.py's upload/validate/solve contract holds.")


if __name__ == "__main__":
    main()
