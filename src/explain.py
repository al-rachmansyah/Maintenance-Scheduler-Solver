"""
Explainability — Phase 6 (Role A/B).

With every scenario landing at 0 hard violations and a small, near-optimal
soft score (see CLAUDE.md's "Current status" — 25.2 on A/C, 44 on B at the
time this was last measured, all comfortably below the organizer's own
32.2 sample), there is little of the delay drama Project_Framework.md
§8.7 originally imagined ("explain the delays"). Reframed per the user's
2026-09-19 "Go ahead": explain the CONSTRAINT PRESSURE and TRADE-OFFS that
made a fully feasible, near-zero-overrun schedule non-trivial to reach —
which locations were genuinely binding, where B/C spent their (small)
soft-cost budget and why, and how individual activities were actually
placed relative to their earliest legal week.

Deliberately reuses src/self_check.py's existing data rather than
reimplementing any analysis (Design philosophy #4: never reimplement a
rule/computation twice) — this module only narrates numbers self_check.py
already computes (`compute_capacity_hotspots`, `compute_soft_scores`,
`replay_submission`'s ScheduleState) into plain sentences.

======================================================================
REVIEW POINTS (judgment calls made while writing this — flag if wrong):
======================================================================

1. **"Binding" vs "at-capacity" hotspots.** `compute_capacity_hotspots`
   returns every `(location_id, week)` at or ABOVE capacity, most with
   `excess == 0` (fully utilized but not exceeded) and a few (only under
   Scenario B/C) with `excess > 0` (genuinely over capacity, absorbed via
   a soft-cost lever). This module calls the `excess == 0` ones
   "at-capacity" and the `excess > 0` ones "binding" — both are reported,
   because a location running at exactly 100% utilization with zero slack
   is a real bottleneck worth naming even though it caused no violation.

2. **`top_locations` frequency is counted across ALL hotspots** (binding
   + at-capacity), not binding alone — the question "which locations are
   under the most pressure" is about chronic full utilization, not just
   the rare cases that spilled into excess.

3. **Per-activity `earliest_legal_week`** is recomputed post-hoc from the
   FINAL (complete, feasible) schedule state: `planned_start_date`'s week,
   raised to `predecessor's last scheduled week + 1` if a predecessor
   exists. This mirrors `constraints.check_predecessor_completion`'s
   actual gate, but simplified: that function's `yield_done <
   pred_total_needed` partial-completion guard only matters while a
   schedule is still being BUILT incrementally (a predecessor might look
   "done" from a partial subset with more still to come). Here the
   predecessor's own accesses are already ALL committed by the time this
   runs, so its last scheduled week is unambiguous. This simplification is
   only valid for a complete, feasible submission — not reused for
   in-progress states.

4. **`delay_weeks = actual_first_week - earliest_legal_week`** is expected
   to be >= 0 for any submission that genuinely respected the live
   predecessor/planned-start gates when it was built. Not asserted here
   (this module narrates, it doesn't validate — that's self_check.py's
   job) — a negative value would only mean the recomputed bound doesn't
   match what was actually enforced, worth a second look rather than a
   crash.

5. **`summarize()` is intentionally bounded**, not one sentence per
   hotspot/activity (there are dozens) — top 3 binding hotspots, top 3
   most-delayed activities, top 3 highest multi-night-per-week activities.
   Matches CLAUDE.md Design philosophy #5's "turn logs into plain
   sentences," not a full dump.
"""

from pathlib import Path

import pandas as pd

from src.data_model import Instance, load_instance
from src.constraints import ScenarioConfig, ScheduleState, SCENARIO_CONFIGS, week_of
from src.self_check import (
    load_submission_csvs,
    scenario_from_results,
    build_scheduled_accesses,
    replay_submission,
    compute_capacity_hotspots,
    compute_soft_scores,
)


# ----------------------------------------------------------------------
# Capacity pressure
# ----------------------------------------------------------------------


def explain_capacity_pressure(hotspots: list[dict], state: ScheduleState) -> dict:
    """See Review points 1-2. `hotspots` is self_check.compute_capacity_hotspots's own output — not recomputed."""
    binding = [h for h in hotspots if h["excess"] > 0]
    at_capacity = [h for h in hotspots if h["excess"] == 0]

    frequency: dict[str, int] = {}
    for h in hotspots:
        frequency[h["location_id"]] = frequency.get(h["location_id"], 0) + 1
    top_locations = sorted(
        ({"location_id": loc, "count": count} for loc, count in frequency.items()),
        key=lambda x: -x["count"],
    )[:5]

    binding_detail = []
    for h in binding:
        occupants = state.at_location_week(h["location_id"], h["week"])
        activities = sorted({a.activity_id for a in occupants})
        binding_detail.append({**h, "activities": activities})

    return {
        "total_hotspots": len(hotspots),
        "binding_count": len(binding),
        "at_capacity_count": len(at_capacity),
        "top_locations": top_locations,
        "binding_detail": binding_detail,
    }


# ----------------------------------------------------------------------
# Scenario trade-offs
# ----------------------------------------------------------------------


def explain_scenario_tradeoffs(soft_scores: dict, hotspots: list[dict], scenario_name: str) -> dict:
    excess = soft_scores["excess_access_nights_total"]
    eclo = soft_scores["eclo_nights_total"]
    binding = [h for h in hotspots if h["excess"] > 0]

    if excess == 0 and eclo == 0:
        narrative = f"Scenario {scenario_name} needed no soft-cost levers: zero excess access-nights and zero ECLO nights."
    else:
        parts = []
        if excess:
            locs = sorted({h["location_id"] for h in binding})
            where = ", ".join(locs) if locs else "unlisted locations"
            parts.append(f"{excess:g} excess access-night(s) across {len(binding)} location-week(s) ({where})")
        if eclo:
            parts.append(f"{eclo} ECLO night(s) used to extend working hours")
        narrative = f"Scenario {scenario_name} spent its soft-cost budget on: " + "; ".join(parts) + "."

    return {
        "excess_access_nights_total": excess,
        "eclo_nights_total": eclo,
        "binding_hotspot_count": len(binding),
        "narrative": narrative,
    }


# ----------------------------------------------------------------------
# Per-activity placement rationale
# ----------------------------------------------------------------------


def explain_activity_placements(state: ScheduleState, instance: Instance) -> list[dict]:
    """See Review points 3-4."""
    activities = instance.activities.set_index("activity_id")
    placements = []

    for activity_id, activity in activities.iterrows():
        own = sorted(state.for_activity(activity_id), key=lambda a: (a.week, a.access_night))
        if not own:
            continue  # not scheduled at all — shouldn't happen for a complete, feasible submission

        base_week = week_of(activity["planned_start_date"], instance)
        pred_id = activity.get("predecessor_activity_id")
        predecessor_activity_id = None
        if pd.notna(pred_id):
            predecessor_activity_id = pred_id
            pred_accesses = state.for_activity(pred_id)
            if pred_accesses:
                base_week = max(base_week, max(a.week for a in pred_accesses) + 1)

        actual_first_week = own[0].week
        nights_per_week: dict[int, set[int]] = {}
        for a in own:
            nights_per_week.setdefault(a.week, set()).add(a.access_night)
        peak_week, peak_nights = max(((wk, len(nights)) for wk, nights in nights_per_week.items()), key=lambda t: t[1])

        placements.append(
            {
                "activity_id": activity_id,
                "contract_number": activity["contract_number"],
                "predecessor_activity_id": predecessor_activity_id,
                "earliest_legal_week": base_week,
                "actual_first_week": actual_first_week,
                "delay_weeks": actual_first_week - base_week,
                "max_nights_in_one_week": peak_nights,
                "peak_week": peak_week,
                "used_eclo": any(a.eclo for a in own),
                "nights_scheduled": len(own),
                "nights_needed": float(activity["total_accesses"]),
            }
        )

    return placements


# ----------------------------------------------------------------------
# Plain-English summary (bounded — see Review point 5)
# ----------------------------------------------------------------------


def summarize(explanation: dict) -> list[str]:
    scenario = explanation["scenario"]
    feasible = explanation["feasible"]
    pressure = explanation["capacity_pressure"]
    tradeoffs = explanation["scenario_tradeoffs"]
    placements = explanation["activity_placements"]

    lines = [
        f"Scenario {scenario}: {'fully feasible' if feasible else 'INFEASIBLE'} schedule with "
        f"{pressure['total_hotspots']} location-week(s) at or above capacity "
        f"({pressure['binding_count']} exceeding it, {pressure['at_capacity_count']} exactly full)."
    ]

    top_locations = pressure["top_locations"][:3]
    if top_locations:
        desc = ", ".join(f"{loc['location_id']} ({loc['count']}x)" for loc in top_locations)
        lines.append(f"Most contested locations across the horizon: {desc}.")

    for h in pressure["binding_detail"][:3]:
        names = ", ".join(h["activities"])
        lines.append(
            f"Week {h['week']} at {h['location_id']}: {h['slots_used']} slots used against capacity "
            f"{h['supply_capacity']} (excess {h['excess']}) — activities involved: {names}."
        )

    lines.append(tradeoffs["narrative"])

    delayed = sorted([p for p in placements if p["delay_weeks"] > 0], key=lambda p: -p["delay_weeks"])[:3]
    for p in delayed:
        gate = f" (gated by predecessor {p['predecessor_activity_id']})" if p["predecessor_activity_id"] else ""
        lines.append(
            f"{p['activity_id']} ({p['contract_number']}) started wk{p['actual_first_week']}, "
            f"{p['delay_weeks']} week(s) after its earliest legal week {p['earliest_legal_week']}{gate}."
        )

    multi_night = sorted([p for p in placements if p["max_nights_in_one_week"] > 1], key=lambda p: -p["max_nights_in_one_week"])[:3]
    for p in multi_night:
        lines.append(
            f"{p['activity_id']} used {p['max_nights_in_one_week']} access-nights in a single week "
            f"(wk{p['peak_week']}) — relies on multiple accesses per week rather than one-per-week."
        )

    return lines


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def build_explanation(instance: Instance, submission_dir: Path, scenario: ScenarioConfig | None = None) -> dict:
    """`scenario` overrides the config looked up from RESULTS.csv — see self_check.build_report."""
    access_df, occupancy_df, results_df = load_submission_csvs(submission_dir)
    scenario_name = scenario_from_results(results_df)
    scenario = scenario or SCENARIO_CONFIGS[scenario_name]

    accesses = build_scheduled_accesses(access_df, occupancy_df, instance)
    state, results = replay_submission(instance, accesses, scenario)
    feasible = all(r.legal for r in results)

    soft_scores = compute_soft_scores(state, instance, scenario_name)
    hotspots = compute_capacity_hotspots(state, instance)

    explanation = {
        "scenario": scenario_name,
        "feasible": feasible,
        "capacity_pressure": explain_capacity_pressure(hotspots, state),
        "scenario_tradeoffs": explain_scenario_tradeoffs(soft_scores, hotspots, scenario_name),
        "activity_placements": explain_activity_placements(state, instance),
    }
    explanation["summary"] = summarize(explanation)
    return explanation


def main() -> None:
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m src.explain <submission_dir>")
        sys.exit(2)

    instance = load_instance()
    explanation = build_explanation(instance, Path(sys.argv[1]))
    for line in explanation["summary"]:
        print(line)
    sys.exit(0 if explanation["feasible"] else 1)


if __name__ == "__main__":
    main()
