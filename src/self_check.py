"""
Self-check validator — Phase 1 remainder (Role B).

Reads a submission's 3 CSVs (any submission directory, not just the sample
— including our own future scheduler output), replays every access through
src/constraints.py's shared legality library, and produces a report shaped
like PS1_README.md §2.7's `{scenario, feasible, hard_violations,
soft_scores, detail}` JSON. This is independent of the official validator
— it's our own local rule-checker, built so the constructive scheduler
(Phase 2) and this validator can never silently disagree about what's
legal, because they both call the exact same check_* functions.

======================================================================
REVIEW POINTS (judgment calls made while writing this — flag if wrong):
======================================================================

1. **`RESULTS.csv`'s `overrun_days` is clamped at 0** (never negative for
   an early finish) — user's explicit decision this session, matching
   `output_writer.write_results`'s same convention. Earliness is reported
   separately as `soft_scores.earliness_days_total`, derived independently
   here rather than clamped away, so the two numbers can't both be lost.

2. **`priority_weighted_score` uses one term per overrunning CONTRACT**,
   not one term per activity. §2.7 says the formula is "summed per
   overrunning activity" using each activity's own `activity_priority`
   nudge, but `overrun_days` only exists at contract grain (RESULTS.csv
   has no activity column) — fanning out per activity would multi-count
   the same overrun days once per activity in the contract. User's
   explicit decision this session: pick the activity actually responsible
   for the contract's final completion week (i.e. whichever activity's own
   last scheduled access-night falls in that week) and use only that
   activity's nudge, once, against the contract's own overrun_days. This
   matches §2.5's own "tie-breaker *within* a contract" wording better
   than a per-activity fan-out would. **Sub-case, itself arbitrary**: if
   several activities tie for the contract's final week, the lowest
   `activity_priority` number (highest nudge) among them wins — see
   `_responsible_activity_nudge()`.

3. **`objective_score` formula trusts §2.7's prose + worked example over
   §2.5's compact LaTeX.** §2.5's `Score_A`/`Score_C` LaTeX has no
   `activity_priority` nudge term at all, but §2.7's own worked example
   (`priority_overrun: {"1": 147, "2": 98, "3": 133}` yielding
   `priority_weighted_score: 18470.6`) does NOT reproduce from the naive
   tier-only sum (100*147 + 10*98 + 1*133 = 15813 != 18470.6) — confirmed
   by hand this session, not assumed. So: `objective_score` = A's/C's
   `priority_weighted_score` term (which DOES include the nudge) plus, for
   B/C, `7*excess_access_nights_total + 5*eclo_nights_total`. Same
   resolution style as docs/decisions.md's existing capacity §1-vs-§2.5
   precedent: trust the scenario-scoring-mechanics sections over the
   high-level summary. This is our own diagnostic number (not part of the
   files actually submitted for grading), so the stakes of a wrong guess
   here are lower than for `RESULTS.csv`'s own columns.

4. **`excess_access_nights_total` counts capacity excess unconditionally**
   — even when that excess was ALSO a hard `capacity` violation (e.g. any
   Scenario A breach). Rationale: it's real excess demand worth reporting
   as a diagnostic regardless of whether the scenario also hard-fails it.
   **Computed from the END-STATE hotspots** (`sum(h["excess"])` over
   `compute_capacity_hotspots`, one term per location-week), matching
   PS1_README §2.6's "summed across location-weeks." Fixed 2026-09-19:
   it used to sum `check_capacity`'s per-candidate `excess` across the
   replay, which re-counted the same over-capacity location-week once per
   access committed there (Scenario B's real output read 7.0 against a true
   end-state total of 2). See docs/decisions.md.

5. **Self-check recomputes everything from the replayed schedule and never
   trusts the submission's own `RESULTS.csv` numbers at face value** — the
   entire point of a self-check is to catch exactly that kind of
   mismatch. Any disagreement between the submission's claimed
   `RESULTS.csv` and the independently-recomputed numbers is surfaced as
   `detail.results_csv_mismatches`, not a new hard-violation tag (no
   PS1_README rule tag corresponds to "your RESULTS.csv lied").

6. **`detail.capacity_hotspots` is undefined by the spec.** Proposed:
   every `(location_id, week)` at or above capacity, as
   `{"location_id", "week", "slots_used", "supply_capacity", "excess"}`,
   sorted by excess desc then slots_used desc.

7. **No "closure" carve-out lives in this file.** `build_report()` reports
   exactly what `check_all()` says, including the known, already-logged
   buffer/mirroring discrepancy against the sample (see
   docs/decisions.md). That carve-out belongs only in
   tests/test_self_check.py, mirroring how tests/test_constraints.py
   already handles it — a production validator must never quietly hide a
   result it disagrees with.
"""

import json
from pathlib import Path

import pandas as pd

from src.data_model import Instance, load_instance
from src.constraints import (
    ScenarioConfig,
    ScheduledAccess,
    ScheduleState,
    SCENARIO_CONFIGS,
    LegalityResult,
    check_all,
    check_predecessor_acyclic,
    check_workload_conservation_all,
    buffer_proximity_pairs,
    week_end_date,
)

TIER_WEIGHT = {1: 100, 2: 10, 3: 1}  # PS1_README §2.5 per-overrun-day weight by contract_priority; also used by src/explain.py
FORMULA_VERSION = "self_check-2026-09-18"  # bumped if the §2.5/§2.7 scoring interpretation above changes


# ----------------------------------------------------------------------
# Loading — the ONE place that reads a submission's 3 CSVs. Moved here
# (from what was tests/test_constraints.py's private build_accesses())
# so production code and tests share one loader instead of two.
# ----------------------------------------------------------------------


def load_submission_csvs(submission_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read a submission directory's 3 CSVs, in the order (access, occupancy, results)."""
    submission_dir = Path(submission_dir)
    access_df = pd.read_csv(submission_dir / "SCHEDULE_ACCESS.csv")
    occupancy_df = pd.read_csv(submission_dir / "SCHEDULE_OCCUPANCY.csv")
    results_df = pd.read_csv(submission_dir / "RESULTS.csv")
    return access_df, occupancy_df, results_df


def scenario_from_results(results_df: pd.DataFrame) -> str:
    """PS1_README §2.7: 'scenario ... read from RESULTS.csv'; §2.6: one scenario per file, never mixed."""
    scenarios = results_df["scenario"].unique()
    if len(scenarios) != 1:
        raise ValueError(f"RESULTS.csv must contain exactly one scenario, found {sorted(scenarios)}")
    return scenarios[0]


def build_scheduled_accesses(access_df: pd.DataFrame, occupancy_df: pd.DataFrame, instance: Instance) -> list[ScheduledAccess]:
    """
    Join SCHEDULE_ACCESS.csv + SCHEDULE_OCCUPANCY.csv rows (both keyed by
    activity_id+week) with instance.activities/instance.contracts to
    reconstruct the ScheduledAccess objects constraints.py's check_*
    functions expect. Returned in chronological replay order (week, then
    activity_id, then access_seq) — predecessor/weekly-allocation checks
    are only meaningful if earlier weeks are committed before later ones.

    An activity CAN legitimately have more than one access in the same
    week as of 2026-09-19 (see docs/decisions.md's "one access per
    activity per week" reversal) — SCHEDULE_OCCUPANCY.csv's schema
    (activity_id, week, location_id, co_share_group) has no access_seq
    column, so multiple accesses in one week all share the SAME
    occupancy rows here, which is exactly correct as long as they were
    all committed to the SAME locations/group (the same possession
    continuing). What's still genuinely ambiguous — and asserted against
    below — is a (activity_id, week, location_id) triple mapped to
    DIFFERENT co_share_group values, which the schema cannot represent
    at all and always indicates a real bug upstream. Found this the hard
    way once already: an early version of the greedy scheduler's two-pass
    Scenario-B fallback (src/greedy_scheduler.py) produced exactly this,
    and it surfaced as a bogus "legal_mix" hard violation instead of the
    real bug. Fail loudly here instead.
    """
    inconsistent = occupancy_df.groupby(["activity_id", "week", "location_id"])["co_share_group"].nunique()
    inconsistent = inconsistent[inconsistent > 1]
    if not inconsistent.empty:
        raise ValueError(f"SCHEDULE_OCCUPANCY.csv has inconsistent co_share_group values for: {inconsistent.index.tolist()}")

    activities = instance.activities.set_index("activity_id")
    contracts = instance.contracts.set_index("contract_number")

    accesses = []
    for _, row in access_df.iterrows():
        activity_id = row["activity_id"]
        week = int(row["week"])
        occ_rows = occupancy_df[(occupancy_df["activity_id"] == activity_id) & (occupancy_df["week"] == week)]
        locations = tuple(occ_rows["location_id"])
        co_share_group = dict(zip(occ_rows["location_id"], occ_rows["co_share_group"]))

        contract_number = activities.loc[activity_id, "contract_number"]
        contract = contracts.loc[contract_number]

        accesses.append(
            ScheduledAccess(
                activity_id=activity_id,
                contract_number=contract_number,
                access_type=contract["access_type"],
                nature_of_works=contract["nature_of_activity"],
                access_seq=int(row["access_seq"]),
                week=week,
                eclo=bool(row["eclo"]),
                access_night=int(row["access_night"]),
                locations=locations,
                co_share_group=co_share_group,
            )
        )

    accesses.sort(key=lambda a: (a.week, a.activity_id, a.access_seq))
    return accesses


# ----------------------------------------------------------------------
# Replay
# ----------------------------------------------------------------------


def replay_submission(instance: Instance, accesses: list[ScheduledAccess], scenario: ScenarioConfig) -> tuple[ScheduleState, list[LegalityResult]]:
    """
    Commit every access in order, collecting every check_* result along
    the way (unconditionally — a self-check must surface every violation
    across the whole horizon, unlike a real scheduler which would reject
    an illegal candidate outright rather than commit it anyway).
    """
    state = ScheduleState(instance)
    results: list[LegalityResult] = [check_predecessor_acyclic(instance)]

    for candidate in accesses:
        results.extend(check_all(candidate, state, instance, scenario))
        state.add(candidate)

    results.extend(check_workload_conservation_all(state, instance))
    return state, results


# ----------------------------------------------------------------------
# Soft scores + diagnostics
# ----------------------------------------------------------------------


def _activity_ids_for_contract(contract_number: str, instance: Instance) -> set[str]:
    activities = instance.activities
    return set(activities[activities["contract_number"] == contract_number]["activity_id"])


def _weeks_for_contract(contract_number: str, state: ScheduleState, instance: Instance) -> list[int]:
    contract_activity_ids = _activity_ids_for_contract(contract_number, instance)
    return [a.week for a in state.accesses if a.activity_id in contract_activity_ids]


def _contract_overrun_days(state: ScheduleState, instance: Instance) -> dict[str, int]:
    """Per contract_number: signed (simulated_completion_date - planned_completion_date) days, unclamped. See Review point 1."""
    overruns: dict[str, int] = {}
    for contract_number, contract in instance.contracts.set_index("contract_number").iterrows():
        weeks = _weeks_for_contract(contract_number, state, instance)
        if not weeks:
            overruns[contract_number] = 0
            continue
        simulated = week_end_date(max(weeks), instance)
        planned = pd.Timestamp(contract["planned_completion_date"]).date()
        overruns[contract_number] = (simulated - planned).days
    return overruns


def _responsible_activity_nudge(contract_number: str, final_week: int, state: ScheduleState, instance: Instance) -> float:
    """
    See Review point 2: the activity whose own last scheduled access-night
    falls in the contract's final (overrunning) week. Ties broken by
    lowest activity_priority number (highest nudge) — itself arbitrary.
    """
    activities = instance.activities.set_index("activity_id")
    contract_activity_ids = _activity_ids_for_contract(contract_number, instance)
    candidates = [a.activity_id for a in state.accesses if a.activity_id in contract_activity_ids and a.week == final_week]
    if not candidates:
        return 0.0
    priorities = [int(activities.loc[aid, "activity_priority"]) for aid in candidates]
    best_priority = min(priorities)  # 1 = High, wins the tie-break
    return {1: 0.3, 2: 0.2, 3: 0.0}[best_priority]


def compute_capacity_hotspots(state: ScheduleState, instance: Instance) -> list[dict]:
    """See Review point 6 — undefined by spec, proposed shape."""
    supply = instance.location_supply.set_index("location_id")["supply_capacity"]
    seen: set[tuple[str, int]] = set()
    hotspots = []
    for access in state.accesses:
        for loc in access.locations:
            key = (loc, access.week)
            if key in seen:
                continue
            seen.add(key)
            slots_used = len({a.co_share_group[loc] for a in state.at_location_week(loc, access.week) if loc in a.co_share_group})
            cap = int(supply.loc[loc])
            if slots_used >= cap:
                hotspots.append({"location_id": loc, "week": access.week, "slots_used": slots_used, "supply_capacity": cap, "excess": max(0, slots_used - cap)})
    hotspots.sort(key=lambda h: (-h["excess"], -h["slots_used"]))
    return hotspots


def compute_soft_scores(state: ScheduleState, instance: Instance, scenario_name: str) -> dict:
    contracts = instance.contracts.set_index("contract_number")
    overrun_by_contract = _contract_overrun_days(state, instance)

    overrun_days_total = 0
    earliness_days_total = 0
    contracts_overrunning = 0
    priority_overrun = {"1": 0, "2": 0, "3": 0}
    priority_weighted_score = 0.0
    tier_weight = TIER_WEIGHT

    for contract_number, raw_days in overrun_by_contract.items():
        # raw_days is the unclamped signed diff from _contract_overrun_days;
        # clamp here for the totals, but derive earliness from the sign
        # BEFORE clamping so neither number is lost (see Review point 1).
        clamped = max(0, raw_days)
        if clamped > 0:
            contracts_overrunning += 1
            overrun_days_total += clamped
            tier = int(contracts.loc[contract_number, "contract_priority"])
            priority_overrun[str(tier)] += clamped
            weeks = _weeks_for_contract(contract_number, state, instance)
            final_week = max(weeks)  # weeks is non-empty here: raw_days != 0 required at least one access
            nudge = _responsible_activity_nudge(contract_number, final_week, state, instance)
            priority_weighted_score += tier_weight[tier] * (1 + nudge) * clamped
        else:
            earliness_days_total += max(0, -raw_days)

    excess_access_nights_total = sum(h["excess"] for h in compute_capacity_hotspots(state, instance))
    eclo_nights_total = sum(1 for a in state.accesses if a.eclo)

    return {
        "scenario": scenario_name,
        "overrun_days_total": overrun_days_total,
        "contracts_overrunning": contracts_overrunning,
        "earliness_days_total": earliness_days_total,
        "excess_access_nights_total": excess_access_nights_total,
        "eclo_nights_total": eclo_nights_total,
        "priority_overrun": priority_overrun,
        "priority_weighted_score": round(priority_weighted_score, 4),
    }


def compute_objective_score(soft_scores: dict, scenario_name: str) -> float:
    """See Review point 3."""
    excess_term = 7 * soft_scores["excess_access_nights_total"] + 5 * soft_scores["eclo_nights_total"]
    if scenario_name == "A":
        return round(soft_scores["priority_weighted_score"], 4)
    if scenario_name == "B":
        return round(excess_term, 4)
    return round(soft_scores["priority_weighted_score"] + excess_term, 4)


def score_state(instance: Instance, scenario: ScenarioConfig, state: ScheduleState) -> tuple[int, float, int]:
    """
    (hard_violation_count, objective_score, buffer_proximity_pair_count) —
    compared lexicographically by the engines to pick the better of two
    candidate schedules: fewer hard violations always wins, then the real
    §2.5 objective, and only then — as a pure tie-break — fewer
    buffer-proximity pairs (the soft hedge described in constraints.py's
    "Rule 4, SOFT companion" section). That ordering is what makes the
    hedge "free": it can never buy itself a worse objective.
    """
    accesses = sorted(state.accesses, key=lambda a: (a.week, a.activity_id, a.access_seq))
    replayed_state, results = replay_submission(instance, accesses, scenario)
    hard_violation_count = sum(1 for r in results if not r.legal)
    soft_scores = compute_soft_scores(replayed_state, instance, scenario.name)
    proximity = len(buffer_proximity_pairs(replayed_state, instance))
    return (hard_violation_count, compute_objective_score(soft_scores, scenario.name), proximity)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def build_report(instance: Instance, submission_dir: Path, scenario: ScenarioConfig | None = None) -> dict:
    """
    `scenario` overrides the config looked up from RESULTS.csv's scenario
    letter — only needed to validate a submission produced under a non-
    default config (e.g. `one_access_per_activity_week=False`); by default
    the strict, README-compliant config for that letter is used.
    """
    access_df, occupancy_df, results_df = load_submission_csvs(submission_dir)
    scenario_name = scenario_from_results(results_df)
    scenario = scenario or SCENARIO_CONFIGS[scenario_name]

    accesses = build_scheduled_accesses(access_df, occupancy_df, instance)
    state, results = replay_submission(instance, accesses, scenario)

    hard_violations = [{"rule": r.rule, "severity": "hard", "detail": r.reason} for r in results if not r.legal]
    feasible = len(hard_violations) == 0

    soft_scores = compute_soft_scores(state, instance, scenario_name)
    eclo_nights = soft_scores["eclo_nights_total"]

    report = {
        "scenario": scenario_name,
        "feasible": feasible,
        "hard_violations": hard_violations,
        "soft_scores": soft_scores,
        "detail": {
            "capacity_hotspots": compute_capacity_hotspots(state, instance),
            "nights_scheduled": len(state.accesses),
            "eclo_nights": eclo_nights,
            # Soft diagnostic, NOT a hard violation — see constraints.py "Rule 4, SOFT companion".
            "buffer_proximity_pairs": [{"activity_a": a, "activity_b": b, "week": w} for a, b, w in buffer_proximity_pairs(state, instance)],
        },
    }

    # See Review point 5 — recomputed dates vs. the submission's own RESULTS.csv claims.
    recomputed = _contract_overrun_days(state, instance)
    mismatches = []
    for _, row in results_df.iterrows():
        claimed = int(row["overrun_days"])
        actual = max(0, recomputed.get(row["contract_number"], 0))
        if claimed != actual:
            mismatches.append({"contract_number": row["contract_number"], "claimed_overrun_days": claimed, "recomputed_overrun_days": actual})
    if mismatches:
        report["detail"]["results_csv_mismatches"] = mismatches

    if feasible:
        report["objective_score"] = compute_objective_score(soft_scores, scenario_name)
        report["formula_version"] = FORMULA_VERSION

    return report


def _json_default(obj):
    """Fallback for json.dumps — numpy scalars (e.g. from a pandas .loc[...]) have no native JSON type."""
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def write_report(report: dict, out_dir: Path) -> Path:
    """
    Write the §2.7 output report to `<out_dir>/report.json`, alongside the
    3 submission CSVs. Added 2026-09-20: `build_report()` already produces
    the exact §2.7 shape, but it was only ever printed to stdout or asserted
    against in tests — never materialized as a file next to the submission
    it describes, which is what a reader of `outputs/scenario_<X>/` would
    actually expect to find per §2.7 ("The output report"). Both scheduler
    CLIs (`greedy_scheduler.main`/`cpsat_engine.main`) and `src.api.solve()`
    now call this after writing the CSVs.
    """
    out_dir = Path(out_dir)
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, indent=2, default=_json_default))
    return path


def main() -> None:
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m src.self_check <submission_dir>")
        sys.exit(2)

    instance = load_instance()
    report = build_report(instance, Path(sys.argv[1]))
    print(json.dumps(report, indent=2, default=_json_default))
    sys.exit(0 if report["feasible"] else 1)


if __name__ == "__main__":
    main()
