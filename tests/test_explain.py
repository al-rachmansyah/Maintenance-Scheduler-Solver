"""
Validates src/explain.py (Phase 6) two ways:

1. Against the organizer's own feasible sample submission — independent
   ground truth for the post-hoc `earliest_legal_week` recomputation:
   every activity in a genuinely feasible schedule must have started at or
   after its recomputed earliest legal week (delay_weeks >= 0).
2. Against real greedy-scheduler output for all 3 scenarios — checks that
   every number the narration reports agrees with self_check.py's own
   independently computed numbers (never a second, drifting copy), that
   Scenario A reports zero binding hotspots, and that the summary stays
   bounded.

Also a regression test for the `excess_access_nights_total` overcounting
bug found while writing explain.py (see docs/decisions.md): it must equal
the sum of end-state per-location-week excess, i.e. what the narration
itself lists as binding.

Run: python tests/test_explain.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_model import load_instance
from src.constraints import SCENARIO_CONFIGS
from src.greedy_scheduler import schedule
from src.output_writer import write_submission
from src.self_check import build_report
from src.explain import build_explanation

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "03_submission_sample"
MAX_SUMMARY_LINES = 12  # 1 headline + 1 top-locations + 3 binding + 1 tradeoff + 3 delayed + 3 multi-night


def check_explanation(label: str, instance, submission_dir: Path, failures: list[str]) -> dict:
    explanation = build_explanation(instance, submission_dir)
    report = build_report(instance, submission_dir)
    pressure = explanation["capacity_pressure"]
    tradeoffs = explanation["scenario_tradeoffs"]
    placements = explanation["activity_placements"]

    if explanation["scenario"] != report["scenario"]:
        failures.append(f"{label}: scenario {explanation['scenario']!r} != self_check's {report['scenario']!r}")
    if explanation["feasible"] != report["feasible"]:
        failures.append(f"{label}: feasible {explanation['feasible']} != self_check's {report['feasible']}")

    hotspots = report["detail"]["capacity_hotspots"]
    if pressure["total_hotspots"] != len(hotspots):
        failures.append(f"{label}: total_hotspots {pressure['total_hotspots']} != self_check's {len(hotspots)}")
    binding = [h for h in hotspots if h["excess"] > 0]
    if pressure["binding_count"] != len(binding):
        failures.append(f"{label}: binding_count {pressure['binding_count']} != {len(binding)}")
    if pressure["binding_count"] + pressure["at_capacity_count"] != pressure["total_hotspots"]:
        failures.append(f"{label}: binding + at_capacity != total_hotspots")

    soft_excess = report["soft_scores"]["excess_access_nights_total"]
    detail_excess = sum(h["excess"] for h in pressure["binding_detail"])
    if soft_excess != detail_excess:
        failures.append(f"{label}: soft_scores excess {soft_excess} != summed binding_detail excess {detail_excess}")
    if tradeoffs["excess_access_nights_total"] != soft_excess:
        failures.append(f"{label}: tradeoffs excess {tradeoffs['excess_access_nights_total']} != {soft_excess}")
    if tradeoffs["eclo_nights_total"] != report["soft_scores"]["eclo_nights_total"]:
        failures.append(f"{label}: tradeoffs eclo != soft_scores eclo")

    for h in pressure["binding_detail"]:
        if not h["activities"]:
            failures.append(f"{label}: binding hotspot {h['location_id']} wk{h['week']} names no activities")

    if len(placements) != len(instance.activities):
        failures.append(f"{label}: {len(placements)} placements for {len(instance.activities)} activities")
    for p in placements:
        if p["delay_weeks"] < 0:
            failures.append(f"{label}: {p['activity_id']} started wk{p['actual_first_week']} BEFORE earliest legal wk{p['earliest_legal_week']}")
        if p["max_nights_in_one_week"] < 1 or p["nights_scheduled"] < 1:
            failures.append(f"{label}: {p['activity_id']} has no scheduled nights")

    summary = explanation["summary"]
    if not summary or any(not line.strip() for line in summary):
        failures.append(f"{label}: empty summary or blank line")
    if len(summary) > MAX_SUMMARY_LINES:
        failures.append(f"{label}: summary has {len(summary)} lines, exceeds bound {MAX_SUMMARY_LINES}")

    print(f"{label}: scenario={explanation['scenario']} feasible={explanation['feasible']} hotspots={pressure['total_hotspots']} binding={pressure['binding_count']} excess={soft_excess} summary_lines={len(summary)}")
    return explanation


def main():
    instance = load_instance()
    failures: list[str] = []

    sample = check_explanation("sample", instance, SAMPLE_DIR, failures)
    if sample["scenario"] != "A" or not sample["feasible"]:
        failures.append("sample: expected feasible Scenario A")
    if sample["capacity_pressure"]["binding_count"] != 0:
        failures.append(f"sample: expected 0 binding hotspots, got {sample['capacity_pressure']['binding_count']}")

    for scenario_name, scenario in SCENARIO_CONFIGS.items():
        state = schedule(instance, scenario)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = write_submission(state, instance, scenario_name, Path(tmp))
            explanation = check_explanation(f"greedy {scenario_name}", instance, out_dir, failures)
        if not explanation["feasible"]:
            failures.append(f"greedy {scenario_name}: not feasible")
        if scenario_name == "A" and explanation["capacity_pressure"]["binding_count"] != 0:
            failures.append("greedy A: Scenario A must have zero binding (over-capacity) hotspots")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("\nexplain.py agrees with self_check.py on every number, for the sample and all 3 greedy scenarios.")


if __name__ == "__main__":
    main()
