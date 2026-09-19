"""
Validates src/explain.py (Phase 6, Project_Framework.md §8.7).

Two layers:

A. Consistency (sample + greedy A/B/C): every number the explanation reports
   agrees with self_check.py's own; every activity has sentences; every delay
   has reconstructed causes naming real blockers; every late contract has a
   cause; summary stays bounded.

B. Generality — the explanation must not be tuned to one dataset:
   1. strict one-access-per-week Scenario A (real overruns): each overrunning
      contract gets an overrun event, classified structural vs contention.
   2. strict Scenario B: ECLO and excess-capacity events, ECLO tied to the
      deadline arithmetic.
   3. `explain_hard_violations` on synthetic violations.
   4. An INFEASIBLE schedule (an access injected before its planned start):
      explained without crashing, violation reported, negative delay flagged.
   5. A modified instance (activities removed) through every scenario.

Also keeps the regression for the `excess_access_nights_total` overcounting bug
(docs/decisions.md): it must equal the sum of end-state per-location-week excess.

Run: python tests/test_explain.py
"""

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constraints import SCENARIO_CONFIGS, ScheduledAccess
from src.data_model import load_instance
from src.explain import MAX_SUMMARY_LINES, build_explanation, explain_hard_violations
from src.greedy_scheduler import schedule
from src.output_writer import write_submission
from src.self_check import build_report

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_instance" / "03_submission_sample"
EVENT_TYPES = {"hard_violation", "contract_overrun", "start_delay", "excess_capacity", "eclo_use"}
CAUSE_RULES = {"capacity", "legal_mix", "closure", "weekly_allocation", "workfront", "predecessor", "one_per_week", "eclo", "eclo_continuity", "planned_date", "buffer_preference", "sequencing"}


def check_explanation(label: str, instance, submission_dir: Path, failures: list[str], scenario=None) -> dict:
    explanation = build_explanation(instance, submission_dir, scenario=scenario)
    report = build_report(instance, submission_dir, scenario=scenario)
    pressure = explanation["capacity_pressure"]
    tradeoffs = explanation["scenario_tradeoffs"]
    placements = explanation["activity_placements"]
    activity_ids = set(instance.activities["activity_id"])

    if explanation["scenario"] != report["scenario"]:
        failures.append(f"{label}: scenario {explanation['scenario']!r} != self_check's {report['scenario']!r}")
    if explanation["feasible"] != report["feasible"]:
        failures.append(f"{label}: feasible {explanation['feasible']} != self_check's {report['feasible']}")
    if explanation["objective_score"] != report.get("objective_score"):
        failures.append(f"{label}: objective {explanation['objective_score']} != self_check's {report.get('objective_score')}")

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

    # --- placements + per-activity sentences ---
    if len(placements) != len(instance.activities):
        failures.append(f"{label}: {len(placements)} placements for {len(instance.activities)} activities")
    for p in placements:
        if explanation["feasible"] and p["delay_weeks"] < 0:
            failures.append(f"{label}: {p['activity_id']} started wk{p['actual_first_week']} BEFORE earliest legal wk{p['earliest_legal_week']}")
        if p["max_nights_in_one_week"] < 1 or p["nights_scheduled"] < 1:
            failures.append(f"{label}: {p['activity_id']} has no scheduled nights")
        if not p["sentences"] or p["activity_id"] not in p["sentences"][0]:
            failures.append(f"{label}: {p['activity_id']} has no status sentence naming it")
        if p["delay_weeks"] > 0:
            if not p["delay_causes"]:
                failures.append(f"{label}: {p['activity_id']} delayed {p['delay_weeks']}wk but no reconstructed cause")
            for c in p["delay_causes"]:
                if c["rule"] not in CAUSE_RULES:
                    failures.append(f"{label}: {p['activity_id']} has unknown cause rule {c['rule']!r}")
                for b in c["blockers"] or []:
                    if b not in activity_ids:
                        failures.append(f"{label}: {p['activity_id']} cause names unknown blocker {b!r}")
    if set(explanation["by_activity"]) != {p["activity_id"] for p in placements}:
        failures.append(f"{label}: by_activity keys != placed activities")

    # --- events ---
    delayed = {p["activity_id"] for p in placements if p["delay_weeks"] > 0}
    delay_events = {e["activity_id"] for e in explanation["events"] if e["type"] == "start_delay"}
    if delayed != delay_events:
        failures.append(f"{label}: start_delay events {sorted(delay_events)} != delayed activities {sorted(delayed)}")
    for e in explanation["events"]:
        if e["type"] not in EVENT_TYPES or not e["sentence"].strip() or not isinstance(e["severity"], float):
            failures.append(f"{label}: malformed event {e['type']!r}")
    severities = [e["severity"] for e in explanation["events"]]
    if severities != sorted(severities, reverse=True):
        failures.append(f"{label}: events not ordered most-important first")
    violation_total = sum(e["data"]["count"] for e in explanation["events"] if e["type"] == "hard_violation")
    if violation_total != len(report["hard_violations"]):
        failures.append(f"{label}: hard_violation events count {violation_total} != self_check's {len(report['hard_violations'])}")

    # --- contracts ---
    outcomes = explanation["contract_outcomes"]
    if {o["contract_number"] for o in outcomes} != set(instance.contracts["contract_number"]) or set(explanation["by_contract"]) != set(instance.contracts["contract_number"]):
        failures.append(f"{label}: contract outcomes do not cover every contract")
    late = [o for o in outcomes if o["status"] == "late"]
    if len(late) != report["soft_scores"]["contracts_overrunning"]:
        failures.append(f"{label}: {len(late)} late contracts != self_check's {report['soft_scores']['contracts_overrunning']}")
    if sum(o["overrun_days"] for o in late) != report["soft_scores"]["overrun_days_total"]:
        failures.append(f"{label}: summed overrun days != self_check's")
    for o in late:
        if o["cause"] is None or o["cause"]["type"] not in ("structural", "contention", "pace"):
            failures.append(f"{label}: late contract {o['contract_number']} has no classified cause")
    if round(sum(o["cost"] for o in late), 4) != round(report["soft_scores"]["priority_weighted_score"], 4):
        failures.append(f"{label}: summed contract overrun cost {sum(o['cost'] for o in late)} != priority_weighted_score {report['soft_scores']['priority_weighted_score']}")

    summary = explanation["summary"]
    if not summary or any(not line.strip() for line in summary):
        failures.append(f"{label}: empty summary or blank line")
    if len(summary) > MAX_SUMMARY_LINES:
        failures.append(f"{label}: summary has {len(summary)} lines, exceeds bound {MAX_SUMMARY_LINES}")

    print(f"{label}: scenario={explanation['scenario']} feasible={explanation['feasible']} hotspots={pressure['total_hotspots']} binding={pressure['binding_count']} late_contracts={len(late)} events={len(explanation['events'])} summary_lines={len(summary)}")
    return explanation


def explain_state(label, instance, scenario_name, cfg, failures):
    state = schedule(instance, cfg)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = write_submission(state, instance, scenario_name, Path(tmp))
        return check_explanation(label, instance, out_dir, failures, scenario=cfg)


def main():
    instance = load_instance()
    failures: list[str] = []

    # ---------------- A. consistency ----------------
    sample = check_explanation("sample", instance, SAMPLE_DIR, failures)
    if sample["scenario"] != "A" or not sample["feasible"]:
        failures.append("sample: expected feasible Scenario A")
    if sample["capacity_pressure"]["binding_count"] != 0:
        failures.append(f"sample: expected 0 binding hotspots, got {sample['capacity_pressure']['binding_count']}")
    sample_overruns = [e for e in sample["events"] if e["type"] == "contract_overrun"]
    if len(sample_overruns) != 3:
        failures.append(f"sample: the organizer's sample overruns 3 contracts, got {len(sample_overruns)} overrun events")
    # C006/C010: one long activity took 1 night/week though the contract allows 3 — a "pace" cause, not a hard limit
    pace = {e["contract_number"] for e in sample_overruns if e["data"]["cause"]["type"] == "pace"}
    if pace != {"C006", "C010"}:
        failures.append(f"sample: expected C006 and C010 to be explained as 'pace' overruns, got {sorted(pace)}")

    for scenario_name, scenario in SCENARIO_CONFIGS.items():
        explanation = explain_state(f"greedy {scenario_name}", instance, scenario_name, scenario, failures)
        if not explanation["feasible"]:
            failures.append(f"greedy {scenario_name}: not feasible")
        if scenario_name == "A" and explanation["capacity_pressure"]["binding_count"] != 0:
            failures.append("greedy A: Scenario A must have zero binding (over-capacity) hotspots")

    # ---------------- B1. strict A: real overruns ----------------
    strict_a = explain_state("strict A", instance, "A", replace(SCENARIO_CONFIGS["A"], one_access_per_activity_week=True), failures)
    late = [o for o in strict_a["contract_outcomes"] if o["status"] == "late"]
    if not late:
        failures.append("strict A: expected overrunning contracts (the one-access-per-week reading forces them)")
    for o in late:
        if o["cause"]["type"] != "structural" or "unavoidable" not in o["sentences"][0]:
            failures.append(f"strict A: contract {o['contract_number']} should be a structural, unavoidable overrun (single long activity, one night/week, no ECLO)")
        if "Scenario A" not in o["sentences"][0]:
            failures.append(f"strict A: overrun sentence for {o['contract_number']} doesn't state the scenario lever")
    if len([e for e in strict_a["events"] if e["type"] == "contract_overrun"]) != len(late):
        failures.append("strict A: overrun events != late contracts")

    # ---------------- B2. strict B: ECLO + excess capacity ----------------
    strict_b = explain_state("strict B", instance, "B", replace(SCENARIO_CONFIGS["B"], one_access_per_activity_week=True), failures)
    eclo_events = [e for e in strict_b["events"] if e["type"] == "eclo_use"]
    if not eclo_events or not all(e["data"]["forced_by_deadline"] for e in eclo_events):
        failures.append("strict B: expected ECLO events, each forced by the hard deadline arithmetic")
    if len([e for e in strict_b["events"] if e["type"] == "excess_capacity"]) != strict_b["capacity_pressure"]["binding_count"]:
        failures.append("strict B: excess_capacity events != binding hotspots")
    if sum(e["data"]["cost"] for e in eclo_events) != 5 * strict_b["scenario_tradeoffs"]["eclo_nights_total"]:
        failures.append("strict B: ECLO event costs don't add up to 5 x eclo nights")

    # ---------------- B3. hard-violation narration ----------------
    events = explain_hard_violations([{"rule": "capacity", "detail": "L/wk3: 1 slot(s) over"}, {"rule": "capacity", "detail": "L2/wk4: 1 slot(s) over"}, {"rule": "some_new_rule", "detail": "x"}])
    if [e["data"]["count"] for e in events] != [2, 1] or "capacity" not in events[0]["sentence"] or "some_new_rule" not in events[1]["sentence"]:
        failures.append(f"explain_hard_violations mis-grouped or dropped an unknown rule tag: {[e['sentence'] for e in events]}")
    if explain_hard_violations([]):
        failures.append("explain_hard_violations([]) should be empty")

    # ---------------- B4. infeasible schedule ----------------
    cfg = SCENARIO_CONFIGS["A"]
    state = schedule(instance, cfg)
    victim = state.for_activity("A001")[0]
    planned_week = 21  # A001's planned start week on this instance (see docs/decisions.md)
    injected = ScheduledAccess(
        victim.activity_id, victim.contract_number, victim.access_type, victim.nature_of_works, max(a.access_seq for a in state.for_activity("A001")) + 1,
        1, False, 1, victim.locations, {loc: "zz_injected" for loc in victim.locations},
    )
    state.add(injected)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = write_submission(state, instance, "A", Path(tmp))
        infeasible = check_explanation("infeasible A", instance, out_dir, failures, scenario=cfg)
    if infeasible["feasible"] or not any(e["type"] == "hard_violation" and e["data"]["rule"] == "planned_start" for e in infeasible["events"]):
        failures.append("infeasible A: an access before the planned start must be reported as a planned_start violation")
    if not any("INFEASIBLE" in line for line in infeasible["summary"]):
        failures.append("infeasible A: summary must say INFEASIBLE")
    a001 = next(p for p in infeasible["activity_placements"] if p["activity_id"] == "A001")
    if a001["delay_weeks"] >= 0 or "BEFORE" not in a001["sentences"][0]:
        failures.append(f"infeasible A: A001 starting before wk{planned_week} should be flagged, got delay {a001['delay_weeks']}")

    # ---------------- B5. modified instance ----------------
    acts = instance.activities
    predecessor_ids = set(acts["predecessor_activity_id"].dropna())
    droppable = acts[~acts["activity_id"].isin(predecessor_ids) & acts["predecessor_activity_id"].isna()]
    dropped = set(droppable["activity_id"].iloc[::3])
    variant = replace(instance, activities=acts[~acts["activity_id"].isin(dropped)].reset_index(drop=True))
    for scenario_name, scenario in SCENARIO_CONFIGS.items():
        explain_state(f"variant {scenario_name} (-{len(dropped)} activities)", variant, scenario_name, scenario, failures)

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("\nexplain.py is consistent with self_check.py and holds up on overruns, ECLO, violations, an infeasible schedule and a modified instance.")


if __name__ == "__main__":
    main()
