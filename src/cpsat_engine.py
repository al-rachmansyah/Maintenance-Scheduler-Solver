"""
CP-SAT engine — Phase 3 (Role A).

======================================================================
INTERFACE CONTRACT — same as src/greedy_scheduler.py:
======================================================================

    def schedule(instance: Instance, scenario: ScenarioConfig) -> ScheduleState

is the entry point (a third, optional `time_limit_seconds` kwarg exists
for testing/tuning — callers that only pass instance+scenario see no
difference). A caller can swap engines by changing which module it
imports, nothing else — see greedy_scheduler.py's own docstring for the
swap example.

======================================================================
HYBRID DESIGN — read this before touching the model. Revised twice this
session after two failed strategies — see Review point 1 for the actual
evidence, not just the conclusion.
======================================================================

Building a FULLY native CP-SAT encoding of every rule in constraints.py
(buffer/exclusion zones with their sector-sequence extension and
opposite-bound mirroring, legal-mix composition within an arbitrarily-
labeled co_share_group, capacity as true bin-packing across slots) is a
substantial, high-risk undertaking on its own — and Design philosophy #4
already says never reimplement a rule twice. So this engine deliberately
splits the problem — but NOT by literally replaying CP-SAT's exact
per-access weeks (that was tried first, see Review point 1, and made
things worse):

  - **CP-SAT decides the TEMPORAL PLAN'S SHAPE**: for each activity, an
    optimized finish_week and an ECLO usage pattern, respecting
    predecessor precedence and each contract's weekly-cap*workfront
    throughput ceiling exactly (clean integer/interval constraints),
    minimizing the real §2.5 objective (priority-weighted overrun for
    A/C, heavily-penalized-but-not-forbidden overrun for B, ECLO penalty
    for B/C).
  - **`greedy_scheduler`'s own week-by-week sweep still does ALL the
    actual placing** — the exact same, already-validated engine, unchanged
    in its mechanics. CP-SAT's solution only influences it through two
    narrow channels: (a) the ORDER activities are given a shot each week
    (sorted by CP-SAT's finish_week instead of the static contract-
    priority/EDF key), and (b) an ECLO PREFERENCE per access (from
    CP-SAT's slot-level eclo decisions, via `_sweep`'s optional
    `eclo_hint` parameter). Spatial legality (buffer/legal-mix/capacity)
    is therefore checked by the exact same `constraints.is_legal()`
    oracle everything else uses, inside a placement mechanism already
    proven to behave fairly under contention — never a second,
    independent placement engine.

Consequence, documented honestly rather than glossed over: this engine's
own objective does NOT know about locations at all, so it cannot directly
minimize `excess_access_nights_total` (a spatial/capacity concept) the
way it directly minimizes overrun/ECLO — that term is whatever falls out
of the sweep's own packing, same as plain greedy produces. `access_night`
itself is never a CP-SAT decision variable — PS1_README's own wording
("a local accounting index... independent of location/sector") plus the
fact that no §2.5 penalty term depends on its VALUE means the weekly-
cap*workfront ceiling is sufficient to GUARANTEE a valid access_night/
workfront assignment exists after the fact (a capacity-only bin-packing
fact, not an assumption) — so `_try_place_in_week`'s own access_night
trial loop is enough; modeling it inside CP-SAT would add model size for
zero objective benefit.

======================================================================
REVIEW POINTS:
======================================================================

1. **Why exact per-access week targets were abandoned — two real,
   measured failures, not a hunch.** First attempt: repair tried each
   CP-SAT-suggested (week, eclo) once, abandoning an activity's ENTIRE
   remaining plan on the first miss. Measured result on the real
   instance, Scenario A: 15/54 activities had their very FIRST
   CP-SAT-suggested week already spatially illegal (CP-SAT has zero
   awareness of physical bottlenecks like the interchange's capacity-1
   corridor, so many activities' individually-"optimal" early weeks
   piled up on it) — all 15 got dumped into one fallback sweep competing
   for whatever the other 39 activities' successful repairs had already
   used up. Scored 1178.8 (priority_weighted_score) — WORSE than plain
   greedy's 835.8, despite greedy's own output being the solver's hint.
   Second attempt: kept exact targets but let each access search forward
   from its target instead of aborting on first miss. Measured result:
   WORSE again (12900.3) — because activities were now processed in an
   order derived from CP-SAT's spatially-blind week choices, and an
   early, contested activity's UNBOUNDED forward search could hog many
   weeks before a later-processed but higher-real-priority activity got
   a turn — the opposite of `_sweep`'s fairness property (every activity
   gets ONE attempt per week, in a stable, consistent order). Conclusion:
   CP-SAT's per-access week choices are not trustworthy inputs to a
   placement mechanism once bottlenecks it can't see are involved — only
   its higher-level SHAPE (finish_week ordering, eclo pattern) is.

   Even that SHAPE turned out unsafe as a PRIMARY sort key: using
   CP-SAT's finish_week directly as the sole order (instead of
   `greedy_scheduler`'s own contract_priority-first key) let a tier-1
   activity end up processed AFTER tier-3 ones whenever CP-SAT's blind
   temporal plan happened to finish it later — losing the "tier-1 always
   wins contested capacity" guarantee the static order protects by
   construction. Measured score: 14615.3, worse than both prior attempts.
   Final fix: keep `greedy_scheduler._priority_key`'s exact primary key
   (contract deadline first when `scenario.date_hard`, else
   contract_priority, then activity_priority) and use CP-SAT's
   finish_week ONLY as a tie-break within that same tier, where it can't
   override priority — see `_repair_place`'s `tier_preserving_key`.

   Measured on the real instance with this final ordering: Scenario A
   matches greedy exactly (835.8 — no ties to break differently, ECLO is
   forbidden so `eclo_hint` is a no-op too); Scenario B genuinely
   improves (12406.8 vs greedy's 14007.0, AND fewer hard violations: 19
   vs 23); Scenario C mildly regresses (1047.9 vs 835.8). That mixed
   result — better on the scenario with real ECLO/ordering leverage,
   worse on one without much — is exactly why `schedule()` doesn't trust
   this heuristic blindly: it computes both `greedy_schedule()`'s result
   and this hybrid's result, scores each with self_check's own
   `compute_soft_scores`/`compute_objective_score` (the authoritative
   scorer, not a re-derived approximation), and keeps whichever is
   actually better (fewer hard violations first, then lower
   objective_score) — see `_score()`. This makes "CP-SAT never does
   worse than the safety net" true by construction, not by hoping the
   heuristic cooperates on every instance.

2. **The objective omits the `activity_priority` nudge.** §2.5's nudge
   (+0.3/+0.2/+0.0) only ever adjusts the SECONDARY tie-break within a
   contract's own tier band, never enough to cross tiers (see
   `docs/decisions.md`) — so the solver optimizes the dominant
   `contract_priority` term exactly and skips modeling the minor nudge,
   which would require reifying "which activity is responsible for this
   contract's finish week" inside the model itself.
   `self_check.compute_soft_scores` (already built, already the
   authoritative scorer) computes the true, nudge-included score
   afterward for reporting/benchmarking — this simplification only
   affects what the SOLVER steers toward internally, not what gets
   reported.

3. **Scenario B's `date_hard` is modeled as a heavy penalty, never a
   hard constraint** (`_DATE_OVERRUN_BIG_M`), so the model can never
   return INFEASIBLE purely because a deadline is tight — matching
   §1 non-negotiable #3. The magnitude (10,000/day, on top of the normal
   100/10/1 tier weight) is chosen to make the solver prefer literally
   any other lever before accepting overrun in B, without being large
   enough to cause numeric issues in CP-SAT's integer objective.

4. **Slot upper bound `K = total_accesses`** (standard-nights-only is the
   most nights any activity could ever need, since ECLO only reduces the
   nights needed) — this is a derived fact, not a guess.

5. **`time_limit_seconds` only bounds the CP-SAT solve itself — not this
   function's total wall-clock time**, since it also runs
   `greedy_scheduler.schedule()` (for the hint/fallback/`_score()`
   comparison) and `_repair_place`'s own sweep. Originally measured at
   ~85s end-to-end on Scenario B despite `solver.Solve()` finishing in
   0.07s — reported back as "horrible performance," correctly not
   trusted. Root-caused with `cProfile` to a real `constraints.py`
   performance bug (every `is_legal()` call was rebuilding pandas
   indices from scratch instead of caching them once per instance —
   see `constraints.py`'s `_cache_for()` and `docs/decisions.md`'s
   "Settled" entry for the full writeup), not this module's design.
   Fixed there, not patched around here. Post-fix: the same Scenario B
   call runs in ~1.5s.
"""

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from ortools.sat.python import cp_model

from src.data_model import Instance, load_instance
from src.constraints import ScenarioConfig, SCENARIO_CONFIGS, ScheduleState, week_of
from src.greedy_scheduler import (
    schedule as greedy_schedule,
    _build_progress,
    _sweep,
    SchedulingDeadlock,
    _SEARCH_CAP_EXTRA_WEEKS,
    _BUFFER_PATIENCE_LADDER,
    _reset_for_relaxed_retry,
)

_CPSAT_MAX_EXTRA_WEEKS = 20  # CP-SAT's OWN model window — smaller than greedy's 104-week safety margin,
# kept modest so the model stays a tractable size; the repair phase still has greedy's full margin available.
_SOLVE_TIME_LIMIT_SECONDS = 20.0
_DATE_OVERRUN_BIG_M = 10_000  # see Review point 3
_ECLO_PENALTY_PER_NIGHT = 5  # PS1_README §2.5
_TIER_WEIGHT = {1: 100, 2: 10, 3: 1}  # PS1_README §2.5


@dataclass
class _ActivityVars:
    activity_id: str
    contract_number: str
    presence: list  # BoolVar per slot
    week: list  # IntVar per slot (0 = inactive sentinel)
    eclo: list  # BoolVar per slot
    finish_week: object  # IntVar
    intervals: list  # IntervalVar per slot, for the per-contract Cumulative constraint


def _build_model(instance: Instance, scenario: ScenarioConfig) -> tuple[cp_model.CpModel, dict, int, int]:
    activities = instance.activities.set_index("activity_id")
    contracts = instance.contracts.set_index("contract_number")
    horizon_start = date.fromisoformat(instance.parameters["horizon_start"])
    horizon_weeks = int(instance.parameters["horizon_weeks"])

    earliest_week = {aid: week_of(activities.loc[aid, "planned_start_date"], instance) for aid in activities.index}
    start_week = min(earliest_week.values())
    max_week = start_week + horizon_weeks + _CPSAT_MAX_EXTRA_WEEKS

    model = cp_model.CpModel()
    activity_vars: dict[str, _ActivityVars] = {}

    for activity_id, activity in activities.iterrows():
        contract_number = activity["contract_number"]
        total_needed = int(activity["total_accesses"])
        k_slots = total_needed  # see Review point 4

        presence, week_vars, eclo_vars, contributions = [], [], [], []
        prev_week = None
        for k in range(k_slots):
            p = model.NewBoolVar(f"pres_{activity_id}_{k}")
            w = model.NewIntVar(0, max_week, f"week_{activity_id}_{k}")
            e = model.NewBoolVar(f"eclo_{activity_id}_{k}")
            if not scenario.eclo_allowed:
                model.Add(e == 0)

            if k > 0:
                model.Add(p <= presence[k - 1])  # prefix-active pattern
            model.Add(w >= earliest_week[activity_id]).OnlyEnforceIf(p)
            model.Add(w == 0).OnlyEnforceIf(p.Not())
            if prev_week is not None:
                model.Add(w > prev_week).OnlyEnforceIf(p)

            contribution = model.NewIntVar(0, 3, f"contrib_{activity_id}_{k}")
            model.Add(contribution == 0).OnlyEnforceIf(p.Not())
            model.Add(contribution == 2).OnlyEnforceIf([p, e.Not()])
            model.Add(contribution == 3).OnlyEnforceIf([p, e])

            presence.append(p)
            week_vars.append(w)
            eclo_vars.append(e)
            contributions.append(contribution)
            prev_week = w

        model.Add(sum(contributions) >= 2 * total_needed)  # half-unit yield accounting (1.0 std = 2, 1.5 eclo = 3)

        finish_week = model.NewIntVar(0, max_week, f"finish_{activity_id}")
        model.AddMaxEquality(finish_week, week_vars)

        interval_vars = [
            model.NewOptionalFixedSizeIntervalVar(start=week_vars[k], size=1, is_present=presence[k], name=f"iv_{activity_id}_{k}") for k in range(k_slots)
        ]

        activity_vars[activity_id] = _ActivityVars(
            activity_id=activity_id,
            contract_number=contract_number,
            presence=presence,
            week=week_vars,
            eclo=eclo_vars,
            finish_week=finish_week,
            intervals=interval_vars,
        )

    # Predecessor precedence (hard) — needs every activity's finish_week to exist first, hence the second pass.
    for activity_id, activity in activities.iterrows():
        pred_id = activity.get("predecessor_activity_id")
        if isinstance(pred_id, str) and pred_id:
            av = activity_vars[activity_id]
            pred_finish = activity_vars[pred_id].finish_week
            for k in range(len(av.presence)):
                model.Add(av.week[k] > pred_finish).OnlyEnforceIf(av.presence[k])

    # Weekly-allocation + workfront (hard) — one Cumulative per contract; capacity = cap*workfront covers
    # BOTH rules at once (see module docstring's HYBRID DESIGN — access_night is never modeled directly).
    for contract_number, contract in contracts.iterrows():
        cap = int(contract["number_of_maximum_access_per_week"])
        workfront = int(contract["number_of_workfronts"])
        intervals = [iv for aid, av in activity_vars.items() if av.contract_number == contract_number for iv in av.intervals]
        if intervals:
            model.AddCumulative(intervals, [1] * len(intervals), cap * workfront)

    # Objective — PS1_README §2.5, see Review points 2/3.
    objective_terms = []
    for contract_number, contract in contracts.iterrows():
        tier = int(contract["contract_priority"])
        deadline_offset_days = (date.fromisoformat(contract["planned_completion_date"]) - horizon_start).days
        contract_activity_ids = [aid for aid, av in activity_vars.items() if av.contract_number == contract_number]
        if not contract_activity_ids:
            continue
        contract_finish_week = model.NewIntVar(0, max_week, f"cfinish_{contract_number}")
        model.AddMaxEquality(contract_finish_week, [activity_vars[aid].finish_week for aid in contract_activity_ids])

        # finish_offset_days = 7*week - 1 (exact inverse of constraints.week_end_date's own formula)
        overrun = model.NewIntVar(0, max_week * 7, f"overrun_{contract_number}")
        model.AddMaxEquality(overrun, [0, 7 * contract_finish_week - 1 - deadline_offset_days])

        weight = _TIER_WEIGHT[tier] * (_DATE_OVERRUN_BIG_M if scenario.date_hard else 1)
        objective_terms.append(weight * overrun)

    if scenario.eclo_allowed:
        all_eclo = [e for av in activity_vars.values() for e in av.eclo]
        if all_eclo:
            objective_terms.append(_ECLO_PENALTY_PER_NIGHT * sum(all_eclo))

    model.Minimize(sum(objective_terms))
    return model, activity_vars, start_week, max_week


def _apply_hint(model: cp_model.CpModel, activity_vars: dict, greedy_state: ScheduleState, scenario: ScenarioConfig) -> None:
    for activity_id, av in activity_vars.items():
        accesses = sorted(greedy_state.for_activity(activity_id), key=lambda a: a.access_seq)
        for k in range(len(av.presence)):
            if k < len(accesses):
                model.AddHint(av.presence[k], True)
                model.AddHint(av.week[k], accesses[k].week)
                if scenario.eclo_allowed:
                    model.AddHint(av.eclo[k], accesses[k].eclo)
            else:
                model.AddHint(av.presence[k], False)
                model.AddHint(av.week[k], 0)


def _extract_plan(solver: cp_model.CpSolver, activity_vars: dict) -> dict[str, tuple[int, list[bool]]]:
    """Per activity: (finish_week, eclo_sequence) — see module docstring's HYBRID DESIGN."""
    plan = {}
    for activity_id, av in activity_vars.items():
        slots = [(solver.Value(av.week[k]), solver.BooleanValue(av.eclo[k])) for k in range(len(av.presence)) if solver.BooleanValue(av.presence[k])]
        slots.sort(key=lambda s: s[0])
        finish_week = slots[-1][0] if slots else solver.Value(av.finish_week)
        eclo_sequence = [eclo for _week, eclo in slots]
        plan[activity_id] = (finish_week, eclo_sequence)
    return plan


def _repair_place(instance: Instance, scenario: ScenarioConfig, plan: dict[str, tuple[int, list[bool]]], buffer_patience: int = 0) -> ScheduleState:
    """
    `buffer_patience` is passed straight to `greedy_scheduler._sweep` — the
    same soft buffer-proximity hedge greedy uses (see greedy_scheduler's
    Review point 5 and constraints.py's "Rule 4, SOFT companion").

    See module docstring's HYBRID DESIGN and Review point 1 for why this
    is deliberately NOT "replay CP-SAT's exact weeks" — two measured
    failures of that approach led here. `greedy_scheduler._sweep` does
    all the actual placing, unmodified; CP-SAT only supplies an ECLO
    preference per access (via `_sweep`'s `eclo_hint` parameter) and a
    SECONDARY tie-break on activity order.

    That tie-break is deliberately secondary, not primary: sorting
    purely by CP-SAT's own finish_week (ignoring contract_priority
    entirely) was tried and measured worse than plain greedy on the real
    instance, because CP-SAT's finish_week reflects only its
    spatially-blind temporal plan — a tier-1 activity can easily get a
    LATER finish_week than a tier-3 one in that blind model (CP-SAT sees
    no reason to rush it), which then means the tier-1 activity loses
    its "always wins contested weekly capacity" guarantee once that
    order drives the real, spatially-aware sweep. Fix: keep the exact
    same PRIMARY key `greedy_scheduler._priority_key` already uses
    (contract deadline first when `scenario.date_hard`, else
    contract_priority first, then activity_priority) — this protects
    the tier-priority invariant — and use CP-SAT's finish_week only to
    break ties WITHIN that same tier, where it can't override priority.
    """
    state = ScheduleState(instance)
    progress = _build_progress(instance, scenario)
    activities = instance.activities.set_index("activity_id")
    contracts = instance.contracts.set_index("contract_number")

    finish_week_of = {aid: fw for aid, (fw, _seq) in plan.items()}
    eclo_hint = {aid: seq for aid, (_fw, seq) in plan.items()}

    def tier_preserving_key(p):
        contract = contracts.loc[p.contract_number]
        activity_priority = int(activities.loc[p.activity_id, "activity_priority"])
        primary = contract["planned_completion_date"] if scenario.date_hard else int(contract["contract_priority"])
        return (primary, activity_priority, finish_week_of.get(p.activity_id, p.earliest_week), p.earliest_week, p.activity_id)

    ordered = sorted(progress, key=tier_preserving_key)

    horizon_weeks = int(instance.parameters["horizon_weeks"])
    start_week = min(p.earliest_week for p in progress)
    max_week = start_week + horizon_weeks + _SEARCH_CAP_EXTRA_WEEKS

    stale = _sweep(instance, scenario, state, ordered, start_week, max_week, eclo_hint=eclo_hint, buffer_patience=buffer_patience)

    if stale and scenario.date_hard:
        relaxed = replace(scenario, date_hard=False)
        retry_start = _reset_for_relaxed_retry(stale, state)
        stale = _sweep(instance, relaxed, state, stale, retry_start, max_week, eclo_hint=eclo_hint, buffer_patience=buffer_patience)

    if stale:
        details = ", ".join(f"{p.activity_id} ({p.contract_number}): {p.yield_done}/{p.total_needed}" for p in stale)
        raise SchedulingDeadlock(f"CP-SAT-ordered sweep exhausted the search window for: {details}")

    return state


def schedule(instance: Instance, scenario: ScenarioConfig, time_limit_seconds: float = _SOLVE_TIME_LIMIT_SECONDS) -> ScheduleState:
    """The one entry point — see module docstring's INTERFACE CONTRACT."""
    try:
        greedy_state = greedy_schedule(instance, scenario)
    except SchedulingDeadlock:
        greedy_state = None

    model, activity_vars, start_week, max_week = _build_model(instance, scenario)
    if greedy_state is not None:
        _apply_hint(model, activity_vars, greedy_state, scenario)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if greedy_state is not None:
            return greedy_state
        raise SchedulingDeadlock(f"CP-SAT returned {solver.StatusName(status)} and greedy also failed to produce a fallback")

    plan = _extract_plan(solver, activity_vars)
    cpsat_state, cpsat_score = None, None
    for patience in _BUFFER_PATIENCE_LADDER:
        try:
            candidate = _repair_place(instance, scenario, plan, buffer_patience=patience)
        except SchedulingDeadlock:
            if patience == 0:
                break  # unhedged repair itself deadlocked — fall back below, same as before
            continue
        score = _score(instance, scenario, candidate)
        if cpsat_score is None or score < cpsat_score:
            cpsat_state, cpsat_score = candidate, score
        if cpsat_score[2] == 0 and patience > 0:
            break
    if cpsat_state is None:
        if greedy_state is not None:
            return greedy_state
        raise SchedulingDeadlock("CP-SAT-ordered repair sweep deadlocked and greedy also failed to produce a fallback")

    if greedy_state is None:
        return cpsat_state

    # Never regress vs. the safety net: the tier-preserving order + eclo_hint
    # heuristic in _repair_place measurably helps on some scenarios (Scenario B:
    # fewer hard violations AND a better score than plain greedy on the real
    # instance) and measurably hurts on others (a mild regression on C) — rather
    # than chase a single ordering that wins everywhere, just score both with
    # the exact same authoritative scorer self_check.py uses and keep whichever
    # is actually better. This is what makes the warm start's "CP-SAT can never
    # do worse than greedy" promise (discussed before writing this module) true
    # by construction rather than by hoping the heuristic always cooperates.
    if cpsat_score <= _score(instance, scenario, greedy_state):
        return cpsat_state
    return greedy_state


def _score(instance: Instance, scenario: ScenarioConfig, state: ScheduleState) -> tuple[int, float, int]:
    """(hard_violation_count, objective_score, buffer_proximity_pairs) — lexicographic; see self_check.score_state."""
    from src.self_check import score_state

    return score_state(instance, scenario, state)


def main() -> None:
    import sys

    from src.output_writer import write_submission
    from src.self_check import build_report, write_report

    scenario_name = sys.argv[1] if len(sys.argv) > 1 else "A"
    if scenario_name not in SCENARIO_CONFIGS:
        print("usage: python -m src.cpsat_engine [A|B|C]")
        sys.exit(2)

    instance = load_instance()
    state = schedule(instance, SCENARIO_CONFIGS[scenario_name])
    # This IS the primary/official engine (schedule() already picks whichever of
    # {cpsat, greedy} scores better — see above), so it writes to the un-suffixed
    # outputs/scenario_<X>/ — the committed Public Test Results deliverable, matching
    # PS1_README.md's/Project_Framework.md's own template exactly (no per-engine
    # suffix). greedy_scheduler.main() writes its OWN isolated result elsewhere
    # (outputs/scenario_<X>_greedy_only/, gitignored) purely for dev-time comparison.
    out_dir = write_submission(state, instance, scenario_name, Path("outputs") / f"scenario_{scenario_name}")
    report = build_report(instance, out_dir)
    write_report(report, out_dir)  # §2.7 output report, alongside the 3 submission CSVs

    print(f"Wrote {len(state.accesses)} accesses to {out_dir}")
    print(f"feasible: {report['feasible']}")
    print(f"hard_violations: {len(report['hard_violations'])} (tags: {sorted(set(v['rule'] for v in report['hard_violations']))})")
    print(f"soft_scores: {report['soft_scores']}")


if __name__ == "__main__":
    main()
