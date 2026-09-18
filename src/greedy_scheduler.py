"""
Greedy constructive scheduler — Phase 2 (Role A).

Per CLAUDE.md's Design philosophy #1: "greedy" describes the algorithm's
speed and lack of backtracking, NOT a license to skip hard rules. Every
placement this module ever commits has already cleared `constraints.
is_legal()` — the exact same oracle the self-check validator uses. This is
the safety net described in Project_Framework.md §3/§8.3: fast, simple,
never illegal, not necessarily well-scored. CP-SAT (Phase 3, not yet
built) is meant to replace this as the primary engine, with this module
kept alive as its automatic fallback on a timeout (Design philosophy #6).

======================================================================
INTERFACE CONTRACT — this is what makes swapping engines "easy":
======================================================================

    def schedule(instance: Instance, scenario: ScenarioConfig) -> ScheduleState

is the ONE entry point. `src/cpsat_engine.py` (not yet written) is
expected to expose a function with this exact same signature. A caller
(the Streamlit app, a CLI, or a future orchestrator that tries CP-SAT
first and falls back to this module on timeout) should be able to do:

    engine = cpsat_engine if want_optimal else greedy_scheduler
    state = engine.schedule(instance, scenario)
    output_writer.write_submission(state, instance, scenario.name)

with no other code to change. Nothing here (or expected in cpsat_engine.py)
should require the caller to know which engine actually ran.

======================================================================
ALGORITHM SHAPE: a week-by-week GLOBAL sweep, not one-activity-at-a-time.
======================================================================

First implementation attempt processed activities one at a time — fully
complete activity A's entire workload (looping over as many weeks as it
takes) before even starting activity B. That is a real bug, not just a
simplification: found because Scenario B deadlocked on contract C002
(workfront=1, cap=3/week, 26 total access-nights needed across 5
activities, planned_completion_date at week 26). `workfront` only limits
how many activities may share the exact SAME access_night — a cap-3
contract can still run 3 DIFFERENT activities concurrently across 3
different access_night slots in one week. Verified by hand before fixing
this: C002 needs a minimum of ceil(26/3) = 9 weeks of pure throughput if
interleaved, comfortably inside its week-26 deadline — the deadlock was
purely an artifact of serializing each activity's full completion before
the next could even attempt a week, which caps a contract's real
throughput at ~1 access-night/week regardless of its actual cap.

Fixed shape: one global week counter. Each week, every activity that
still needs more access-nights AND has reached its own earliest legal
week gets ONE attempt, in priority order (contract_priority,
activity_priority, planned_start_date, activity_id) — so when multiple
activities compete for the same limited weekly capacity, priority order
decides who wins the available slots. `check_predecessor_completion`
(inside `is_legal`) already gates a successor correctly against the LIVE
state, so no separate topological pre-sort is needed for correctness —
priority order alone is enough; predecessor gating just means a
successor's attempts keep failing harmlessly until its predecessor is
actually done.

======================================================================
REVIEW POINTS (simplifying assumptions — this is the safety net, not the
optimizer; each of these trades optimality for simplicity/speed, never
legality):
======================================================================

1. **~~One access-night per activity per week.~~ REVERSED 2026-09-19.**
   Originally adopted because every activity in the sample submission's
   ground truth happens to follow that pattern — an OBSERVATIONAL
   argument, not a rule PS1_README ever states. Found the hard way this
   was leaving real throughput on the table: on Scenario A/C, two
   low-priority contracts (C006, C010) each have one long activity (7
   access-nights) that's the ONLY thing its contract has active during
   its whole window, with the contract's full weekly cap sitting unused
   — yet under the old one-per-week rule it still took 7 CALENDAR weeks
   to finish, overrunning its deadline by 14 and 7 days respectively
   (confirmed by hand: `A036` starts week 22, deadline week 26, needs 7
   nights → old rule forces finish week 28; nothing else was competing
   for its contract's other 2 weekly nights those weeks). That's the
   entire cause of Scenario A/C's 25.2 penalty score — not resource
   contention, just this self-imposed pacing limit.

   Now: an activity may take multiple access-nights in the same week, up
   to its own contract's weekly cap, as long as nothing else needs those
   nights — `_sweep`'s inner while-loop (below) keeps giving an activity
   another attempt in the SAME week until it's done or a further attempt
   is refused. `check_weekly_allocation_and_workfronts` (constraints.py)
   was extended to require each of an activity's OWN accesses that week
   land on a genuinely different `access_night` (reusing the same one
   twice would mean "the same specific night, twice," which is
   meaningless) — that's what turns "keep trying" into "try successive
   DISTINCT nights" automatically, no extra bookkeeping needed here.
   `_choose_group_for_location` was also fixed to reuse an activity's own
   already-committed group at a (location, week) rather than re-running
   the join-eligibility check against itself (see Review point 2's
   note). Verified this doesn't introduce any violation against the
   sample submission (`tests/test_constraints.py`) — the sample simply
   never happens to need same-week repeats, so this is purely additive
   capability, not a behavior change for cases that don't use it.

2. **No backtracking across grouping choices.** For each (location, week)
   this activity's path touches, `_choose_group_for_location` picks ONE
   co_share_group candidate — **first checking whether this SAME activity
   already has a committed access at this (location, week) and, if so,
   reusing that exact group directly** (added 2026-09-19, needed once an
   activity could take multiple access-nights in one week: otherwise its
   own earlier-this-week access looks like "an existing occupant" and the
   normal join-eligibility check could wrongly refuse to let it rejoin
   itself — e.g. a PC seeing itself as "already 1 PC here"). Otherwise,
   prefer joining an existing legal-mix-compatible group over opening a
   new one, since co-sharing increases effective capacity (PS1_README
   §1's "Co-Sharing Increases Capacity" non-negotiable). If that specific
   choice turns out illegal (e.g. it would breach capacity), the whole
   week is abandoned for that activity — it does NOT try a different
   grouping at the same week first. A real backtracking search could
   sometimes still place the same week legally with a different choice;
   this module deliberately doesn't do that (see module docstring: greedy
   means no backtracking).

3. **ECLO is a same-week fallback, not a deadline-lookahead policy.** If
   every access_night at the current candidate week fails as a standard
   (eclo=0) night, and the scenario allows ECLO, the SAME week is retried
   with eclo=1 before giving up on that week for that activity. This is
   simpler than PS1_README §2.5's suggested cost-ordering (prefer
   slipping a Priority-3 contract over spending ECLO, prefer ECLO over
   exceeding capacity) — that kind of look-ahead cost comparison is
   exactly what Phase 3's CP-SAT engine is for. This greedy pass never
   proactively reaches for ECLO before it's actually needed to clear a
   week for that specific activity.

4. **Global search cap + last-resort failure, not a last-resort illegal
   placement.** The week sweep runs from the earliest of all activities'
   legal start weeks up to `horizon_weeks + _SEARCH_CAP_EXTRA_WEEKS` past
   it (a generous ~2-year buffer). Any activity still incomplete when the
   sweep ends raises `SchedulingDeadlock` rather than forcing an illegal
   placement. Design philosophy #1 frames an eventual illegal "last
   resort" as acceptable when NOTHING in the whole plan window works —
   implementing that least-bad-illegal-choice logic is a real feature
   this MVP doesn't attempt; a loud failure is more useful here than a
   silently-wrong submission.

5. **Soft buffer-proximity hedge (added 2026-09-19) — a JUDGMENT CALL, not
   a rule.** The hard buffer check is always-legal (docs/decisions.md), but
   the sample doesn't prove that's what a hidden validator does. As a
   hedge, `_sweep(buffer_patience=N)` lets each activity wait up to N weeks
   instead of taking a legal placement inside another activity's buffer
   footprint (constraints.buffer_proximity_conflicts), then take it anyway
   — so it can delay an activity but never strand one. `schedule()` runs a
   ladder of patience values and keeps the best by `self_check.score_state`
   (hard violations, objective, proximity pairs — lexicographic), which is
   what makes the hedge free: it only wins when it doesn't cost anything on
   the real objective. Waiting is a greedy heuristic, so it CAN shift later
   contention around — the pick-best is the safeguard, not an assumption
   that it never does.
"""

from dataclasses import dataclass, replace

from src.data_model import Instance, load_instance, resolve_activity_path
from src.constraints import (
    ScenarioConfig,
    SCENARIO_CONFIGS,
    ScheduledAccess,
    ScheduleState,
    week_of,
    is_legal,
    buffer_proximity_conflicts,
)

_SEARCH_CAP_EXTRA_WEEKS = 104  # ~2 years of buffer past the nominal horizon — see Review point 4


class SchedulingDeadlock(RuntimeError):
    """Raised when an activity finds no legal week within the search cap — see Review point 4."""


# ----------------------------------------------------------------------
# Priority order (attempt order WITHIN each week — see algorithm shape note)
# ----------------------------------------------------------------------


def _priority_key(activity_id: str, instance: Instance, scenario: ScenarioConfig) -> tuple:
    """
    Project_Framework.md §8.3's literal order is (contract_priority,
    activity_priority, planned_start_date) — right for A/C, where a
    contract's overrun cost is genuinely priority-weighted, so it's
    correct to let a Priority-1 contract win contested capacity over a
    Priority-3 one.

    Scenario B has NO such tradeoff: `date_hard=True` means EVERY
    contract's planned_completion_date is equally non-negotiable — there
    is no "acceptable to slip a low-priority contract" in B the way there
    is in A/C (see PS1_README §2.5: a feasible B submission has zero
    overrun BY CONSTRUCTION). Found by hand this session: with the
    contract-priority-first order, several contracts missed their
    deadline and deadlocked (every week after a missed deadline fails
    `planned_date` forever, confirmed via check_all before making this
    change) while OTHER, merely higher-priority contracts finished with
    slack to spare — priority order was starving urgent-but-low-priority
    contracts of scarce weekly capacity. Fix: when `scenario.date_hard`,
    sort by deadline first (Earliest-Deadline-First, a standard heuristic
    for hard-deadline scheduling), contract/activity priority only as a
    tie-break. Driven by the scenario config, not a scattered
    `if scenario == "B"` (Design philosophy #3/#5).
    """
    activity = instance.activities.set_index("activity_id").loc[activity_id]
    contract = instance.contracts.set_index("contract_number").loc[activity["contract_number"]]
    contract_priority = int(contract["contract_priority"])
    activity_priority = int(activity["activity_priority"])
    planned_start = activity["planned_start_date"]

    if scenario.date_hard:
        return (contract["planned_completion_date"], contract_priority, activity_priority, planned_start, activity_id)
    return (contract_priority, activity_priority, planned_start, activity_id)


def _priority_order(instance: Instance, scenario: ScenarioConfig) -> list[str]:
    """
    Static priority order — NOT a topological sort. Predecessor gating is
    handled dynamically by check_predecessor_completion (inside is_legal)
    against the live state, so a successor simply keeps failing its
    attempts harmlessly until its predecessor is actually done; no
    pre-sort is needed for correctness, only this priority tie-break for
    who gets first crack at contested weekly capacity.
    """
    return sorted(instance.activities["activity_id"], key=lambda aid: _priority_key(aid, instance, scenario))


# ----------------------------------------------------------------------
# Co-sharing group choice (greedy heuristic — see Review point 2)
# ----------------------------------------------------------------------


def _can_join_group(existing_types: list[str], new_type: str) -> bool:
    pm, pc, c = existing_types.count("PM"), existing_types.count("PC"), existing_types.count("C")
    if pm or new_type == "PM":
        return False  # PM must be alone; nothing joins a PM slot, and PM never joins an existing (non-empty) slot
    if new_type == "PC":
        return pc == 0
    return (c < 3) if pc == 1 else (c < 4)


def _choose_group_for_location(location_id: str, week: int, access_type: str, activity_id: str, state: ScheduleState) -> str:
    """
    `activity_id` added 2026-09-19: an activity can now take more than
    one access-night in the same week (see docs/decisions.md's "one
    access per activity per week" reversal) — if it already has a
    committed access at this (location, week), REUSE that exact group
    directly rather than re-running the join-eligibility check. This
    isn't a new join, it's the same possession continuing; running it
    through `_can_join_group` would see the activity's own earlier
    access as "already 1 member of this type" and — for a PC or a full
    group of 4 C's — could wrongly refuse to let it rejoin itself.
    """
    existing = state.at_location_week(location_id, week)
    for a in existing:
        if a.activity_id == activity_id and location_id in a.co_share_group:
            return a.co_share_group[location_id]

    groups: dict[str, list[str]] = {}
    for a in existing:
        if location_id in a.co_share_group:
            groups.setdefault(a.co_share_group[location_id], []).append(a.access_type)

    for label, types in groups.items():
        if _can_join_group(types, access_type):
            return label

    i = 1
    while f"b{i}" in groups:
        i += 1
    return f"b{i}"


# ----------------------------------------------------------------------
# Per-activity scheduling
# ----------------------------------------------------------------------


def _try_place_in_week(
    activity_id: str,
    contract_number: str,
    access_type: str,
    nature_of_works: str,
    locations: tuple[str, ...],
    week: int,
    access_seq: int,
    cap: int,
    instance: Instance,
    scenario: ScenarioConfig,
    state: ScheduleState,
    eclo_preference: bool = False,
    avoid_buffer_proximity: bool = False,
) -> ScheduledAccess | None:
    """
    `eclo_preference` only changes trial ORDER, never legality — default
    False preserves this module's original behaviour exactly (try
    standard first, ECLO only as a same-week fallback). Added so
    src/cpsat_engine.py's repair phase can ask this to try whichever
    eclo value CP-SAT targeted FIRST, still falling back to the other
    value at the same week before giving up on it.

    `avoid_buffer_proximity` (default False = unchanged behaviour) makes a
    legal candidate that would sit within another activity's buffer
    footprint (see constraints.buffer_proximity_conflicts) count as "no
    placement this week" — a SOFT preference only, never a legality rule;
    the caller (`_sweep`) decides how long it is willing to keep waiting.
    """
    co_share_group = {loc: _choose_group_for_location(loc, week, access_type, activity_id, state) for loc in locations}

    if scenario.eclo_allowed:
        eclo_order = (eclo_preference, not eclo_preference)
    else:
        eclo_order = (False,)

    for eclo in eclo_order:
        for access_night in range(1, cap + 1):
            candidate = ScheduledAccess(
                activity_id=activity_id,
                contract_number=contract_number,
                access_type=access_type,
                nature_of_works=nature_of_works,
                access_seq=access_seq,
                week=week,
                eclo=eclo,
                access_night=access_night,
                locations=locations,
                co_share_group=co_share_group,
            )
            if not is_legal(candidate, state, instance, scenario):
                continue
            if avoid_buffer_proximity and buffer_proximity_conflicts(candidate, state, instance):
                # Doesn't depend on eclo/night, so no other night/eclo value at this week can fix it.
                return None
            return candidate
    return None


@dataclass
class _Progress:
    """
    Mutable per-activity tracker for the week-sweep in schedule().

    `next_eligible_week` — NOT the same as `earliest_week` (which stays
    fixed at the activity's planned_start_date-derived floor, kept for
    error messages/diagnostics). `next_eligible_week` is the live gate
    enforcing "one access-night per activity per week" (Review point 1)
    — advanced to `week + 1` every time this activity places an access,
    so a LATER sweep pass (see schedule()'s two-pass fallback) can never
    re-offer an already-used week to the same activity. Found the hard
    way: without this being an explicit, persistent field, the very first
    version of the two-pass fallback let pass 2 restart its week counter
    below a week pass 1 had already committed for the same activity,
    giving it two accesses in one week — which SCHEDULE_OCCUPANCY.csv's
    schema (activity_id,week,location_id,co_share_group — no access_seq
    column) cannot even represent, since a second access in the same week
    would collide with the first under that same (activity_id, week) key.
    So this isn't just an observed convention (Review point 1's original
    framing) — it's a structural requirement of the output schema itself.
    """

    activity_id: str
    contract_number: str
    access_type: str
    nature_of_works: str
    locations: tuple[str, ...]
    cap: int
    total_needed: float
    earliest_week: int
    yield_done: float = 0.0
    access_seq: int = 1
    next_eligible_week: int = 0  # set to earliest_week in _build_progress
    buffer_deferrals: int = 0  # weeks spent waiting to dodge a buffer-proximity conflict — see Review point 5
    deadline_week: int = 0  # week of the contract's planned_completion_date — drives the ECLO-when-forced rule in _sweep

    @property
    def done(self) -> bool:
        return self.yield_done >= self.total_needed


def _eclo_forced_by_deadline(p: _Progress, week: int, scenario: ScenarioConfig) -> bool:
    """
    Scenario B only (hard deadlines + ECLO allowed): True when even taking
    one STANDARD night every remaining week up to the contract's deadline
    could no longer finish this activity — so this access has to be ECLO
    or the deadline is unreachable. Uses ECLO as LATE as possible and only
    when forced, which is also the minimum-ECLO plan for a single activity
    (e.g. needs 7, has 5 weeks -> 4 ECLO nights, exactly the optimum).

    Found 2026-09-20 when one-access-per-activity-week was reinstated: with
    at most one access a week, A036 (needs 7, starts wk22, deadline wk26)
    can only finish on time WITH ECLO, and this scheduler's old "ECLO only
    as a same-week fallback" rule never reached for it -> deadlock.
    """
    if not (scenario.date_hard and scenario.eclo_allowed):
        return False
    weeks_after = p.deadline_week - week
    per_week = 1 if scenario.one_access_per_activity_week else p.cap
    return (p.total_needed - p.yield_done - 1.0) > weeks_after * per_week


def _reset_for_relaxed_retry(remaining: list[_Progress], state: ScheduleState) -> int:
    """
    Rewind each still-incomplete activity to just after its own last
    committed access (or its earliest week if it has none) and return the
    week to restart the relaxed pass from.

    The strict pass sweeps every week up to `max_week` and advances
    `next_eligible_week` past each one, so without this rewind the relaxed
    retry would start beyond `max_week` and never place anything —
    a latent bug (the retry silently did nothing) that only surfaced once
    one-access-per-week made Scenario B's strict pass fail.
    """
    for p in remaining:
        last = max((a.week for a in state.for_activity(p.activity_id)), default=None)
        p.next_eligible_week = p.earliest_week if last is None else max(p.earliest_week, last + 1)
    return min(p.next_eligible_week for p in remaining)


def _build_progress(instance: Instance, scenario: ScenarioConfig) -> list[_Progress]:
    activities = instance.activities.set_index("activity_id")
    contracts = instance.contracts.set_index("contract_number")
    progress = []
    for activity_id in _priority_order(instance, scenario):
        activity = activities.loc[activity_id]
        contract = contracts.loc[activity["contract_number"]]
        earliest_week = week_of(activity["planned_start_date"], instance)
        progress.append(
            _Progress(
                activity_id=activity_id,
                contract_number=activity["contract_number"],
                access_type=contract["access_type"],
                nature_of_works=contract["nature_of_activity"],
                locations=tuple(resolve_activity_path(activity["start_location_id"], activity["end_location_id"], instance)),
                cap=int(contract["number_of_maximum_access_per_week"]),
                total_needed=float(activity["total_accesses"]),
                earliest_week=earliest_week,
                next_eligible_week=earliest_week,
                deadline_week=week_of(contract["planned_completion_date"], instance),
            )
        )
    return progress


def _sweep(instance: Instance, scenario: ScenarioConfig, state: ScheduleState, progress: list[_Progress], start_week: int, max_week: int, eclo_hint: dict[str, list[bool]] | None = None, buffer_patience: int = 0) -> list[_Progress]:
    """
    `buffer_patience` (default 0 = unchanged behaviour): how many weeks each
    activity is willing to WAIT rather than take a legal placement that
    lands in another activity's buffer footprint (Review point 5). Once an
    activity has waited that many weeks it takes conflicting placements
    like any other, so this can only ever delay an activity by at most
    `buffer_patience` weeks, never strand it.

    One week-by-week global sweep (see module docstring's ALGORITHM
    SHAPE). Runs until every activity in `progress` is done or `max_week`
    is reached — does NOT raise; returns whichever remain incomplete so
    the caller decides what to do (retry relaxed, or give up).

    `eclo_hint` is optional and generic — this module never sets it
    itself (default behaviour is unchanged), but src/cpsat_engine.py
    passes one: `eclo_hint[activity_id][i]` is whether that activity's
    (0-indexed) i-th access should PREFER to be ECLO, per CP-SAT's own
    temporal plan. Only changes trial order within `_try_place_in_week`,
    never legality — the ordering itself (`progress`'s own list order,
    set by the caller before calling this) is where CP-SAT's influence
    actually comes from; see cpsat_engine.py's module docstring for why
    exact per-week targets were abandoned in favour of this.
    """
    week = start_week
    while week <= max_week and any(not p.done for p in progress):
        for p in progress:
            if p.done or week < p.next_eligible_week:
                continue
            # An activity may take MORE THAN ONE access-night in this same
            # week now (see Review point 1) — keep trying for this same
            # activity, same week, until it's done or a further attempt
            # fails (blocked by weekly-cap/workfront/etc., or nothing left
            # to gain), THEN advance it to next_eligible_week = week + 1.
            # check_weekly_allocation_and_workfronts already refuses to let
            # the SAME activity reuse the SAME access_night twice, so each
            # successive attempt here lands on a genuinely different night.
            while not p.done:
                hints = eclo_hint.get(p.activity_id) if eclo_hint else None
                preference = hints[p.access_seq - 1] if hints and p.access_seq - 1 < len(hints) else False
                preference = preference or _eclo_forced_by_deadline(p, week, scenario)
                avoid = p.buffer_deferrals < buffer_patience
                place_args = (p.activity_id, p.contract_number, p.access_type, p.nature_of_works, p.locations, week, p.access_seq, p.cap, instance, scenario, state)
                candidate = _try_place_in_week(*place_args, eclo_preference=preference, avoid_buffer_proximity=avoid)
                if candidate is None:
                    # Only a DEFERRAL if a plain legal placement existed and was skipped
                    # purely to dodge a buffer-proximity conflict — a week that was simply
                    # blocked doesn't spend any of this activity's patience.
                    if avoid and _try_place_in_week(*place_args, eclo_preference=preference) is not None:
                        p.buffer_deferrals += 1
                    break
                state.add(candidate)
                p.yield_done += 1.5 if candidate.eclo else 1.0
                p.access_seq += 1
            p.next_eligible_week = week + 1
        week += 1
    return [p for p in progress if not p.done]


# ----------------------------------------------------------------------
# Public entry point — week-by-week global sweep, see module docstring
# ----------------------------------------------------------------------


_BUFFER_PATIENCE_LADDER = (0, 1, 2, 4, 8, 16)  # weeks an activity will wait to dodge a buffer-proximity conflict; 0 = the plain, unhedged greedy


def schedule(instance: Instance, scenario: ScenarioConfig) -> ScheduleState:
    """
    The one entry point — see module docstring's INTERFACE CONTRACT.

    Runs the sweep once per rung of `_BUFFER_PATIENCE_LADDER` and keeps the
    best by `self_check.score_state` (hard violations, then objective, then
    buffer-proximity pairs — see Review point 5). Rung 0 is the plain
    unhedged greedy and always runs first, so a deadlock there still raises
    exactly as before, and a hedged rung can only WIN by not costing any
    violation or objective (ties go to the smaller patience).
    """
    from src.self_check import score_state

    best_state, best_score = None, None
    for patience in _BUFFER_PATIENCE_LADDER:
        try:
            state = _schedule_once(instance, scenario, patience)
        except SchedulingDeadlock:
            if patience == 0:
                raise
            continue
        score = score_state(instance, scenario, state)
        if best_score is None or score < best_score:
            best_state, best_score = state, score
        if best_score[2] == 0 and patience > 0:
            break  # nothing left to hedge; no need to try more patient rungs
    return best_state


def _schedule_once(instance: Instance, scenario: ScenarioConfig, buffer_patience: int) -> ScheduleState:
    """
    One full sweep at a fixed `buffer_patience`.

    Two passes. The first is strict: every hard rule enforced exactly as
    `scenario` defines it. If anything is still incomplete when the
    search cap is exhausted, PS1_README §1's non-negotiable #3 ("must not
    stop or declare the case impossible... keep scheduling under
    congestion") means crashing outright is worse than the alternative:
    a second pass retries ONLY the still-incomplete activities with the
    scenario's OWN designated hard-deadline flex point relaxed
    (`date_hard=False` — never buffer, capacity, or workfront, which stay
    hard no matter what). This only ever does anything for Scenario B
    (the only scenario with `date_hard=True`); for A/C it's a no-op,
    consistent with earlier testing showing neither deadlocks on the real
    instance. Confirmed by hand this session (see the "closure" tag was
    NEVER the blocker in every deadlock case checked) that what forces
    this fallback on the real instance is genuine multi-contract
    contention for shared weekly capacity ahead of a hard deadline — a
    real combinatorial scheduling problem, exactly what CP-SAT (Phase 3)
    exists to solve well; this greedy pass only guarantees a COMPLETE,
    mostly-legal answer, honestly reporting whatever hard violation
    remains rather than hiding or crashing on it.
    """
    state = ScheduleState(instance)
    progress = _build_progress(instance, scenario)

    horizon_weeks = int(instance.parameters["horizon_weeks"])
    start_week = min(p.earliest_week for p in progress)
    max_week = start_week + horizon_weeks + _SEARCH_CAP_EXTRA_WEEKS

    remaining = _sweep(instance, scenario, state, progress, start_week, max_week, buffer_patience=buffer_patience)

    if remaining and scenario.date_hard:
        relaxed = replace(scenario, date_hard=False)
        retry_start = _reset_for_relaxed_retry(remaining, state)
        remaining = _sweep(instance, relaxed, state, remaining, retry_start, max_week, buffer_patience=buffer_patience)

    if remaining:
        details = ", ".join(f"{p.activity_id} ({p.contract_number}): {p.yield_done}/{p.total_needed}" for p in remaining)
        raise SchedulingDeadlock(f"no legal placement found by week {max_week}, even with the scenario's own hard-deadline flex point relaxed, for: {details}")

    return state


def main() -> None:
    import sys
    from pathlib import Path

    from src.output_writer import write_submission
    from src.self_check import build_report, write_report

    scenario_name = sys.argv[1] if len(sys.argv) > 1 else "A"
    if scenario_name not in SCENARIO_CONFIGS:
        print("usage: python -m src.greedy_scheduler [A|B|C]")
        sys.exit(2)

    instance = load_instance()
    state = schedule(instance, SCENARIO_CONFIGS[scenario_name])
    # Deliberately NOT outputs/scenario_<X> (no suffix) — that's the committed
    # Public Test Results deliverable, written by src.cpsat_engine (the primary
    # engine, which already picks whichever of {cpsat, greedy} scores better —
    # see its schedule()). This is greedy's OWN result in isolation, kept only
    # for dev-time comparison (tests/test_benchmark.py); gitignored, not a
    # second deliverable. See docs/decisions.md's "outputs/ folder" entry.
    out_dir = write_submission(state, instance, scenario_name, Path("outputs") / f"scenario_{scenario_name}_greedy_only")
    report = build_report(instance, out_dir)
    write_report(report, out_dir)

    print(f"Wrote {len(state.accesses)} accesses to {out_dir}")
    print(f"feasible: {report['feasible']}")
    print(f"hard_violations: {len(report['hard_violations'])} " f"(tags: {sorted(set(v['rule'] for v in report['hard_violations']))})")
    print(f"soft_scores: {report['soft_scores']}")


if __name__ == "__main__":
    main()
