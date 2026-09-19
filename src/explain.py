"""
Explainability — Phase 6 (Project_Framework.md §8.7, Decision #5).

Answers, for ANY submission (ours, the organizer's, or a hidden instance's),
the questions a works controller actually asks:

  * Why did this activity not start sooner?   (which rule blocked which
    location/week, and which other activities were in the way)
  * Why did this contract finish late / why is it infeasible?  (an
    unavoidable structural limit, or contention — and what it cost)
  * Why were extra access-nights or ECLO used?  (which scenario lever, what
    it cost, and whether the deadline arithmetic forced it)
  * If the schedule breaks a hard rule, which rule, where, how often?

Output of `build_explanation()` (all plain dicts/lists/strings, JSON-safe):

    scenario, feasible
    summary            bounded, prioritised list of plain-English lines (UI headline)
    events             every notable event, most important first:
                       {type, severity, activity_id?, contract_number?, sentence, data}
                       types: hard_violation | contract_overrun | start_delay |
                              excess_capacity | eclo_use
    by_activity        {activity_id: [status sentence, event sentences...]}  -> show next to an activity
    by_contract        {contract_number: [sentences...]}
    capacity_pressure  hotspot statistics (unchanged from the first Phase 6 version)
    scenario_tradeoffs excess-night / ECLO narrative (unchanged)
    activity_placements per-activity records: earliest legal week, delay, causes, sentences
    contract_outcomes  per-contract completion vs plan, with cause data

======================================================================
HOW CAUSES ARE FOUND (and why it is not a reimplementation of any rule)
======================================================================

The scheduler does not log why it declined a week, and a submission from any
other source has no log at all. So causes are RECONSTRUCTED from the final
schedule: for each week an activity could have started (or kept working) but
did not, take the final schedule with that activity's own accesses removed,
build the placement it would have needed (its own path, the same co-share-group
heuristic the scheduler uses, every access-night, standard and ECLO), and ask
`constraints.check_all` — the same single rule oracle everything else uses —
whether it was legal. If every variant fails, the rules that fail in ALL of
them are the cause, and `LegalityResult.location/.blockers` name the location
and the activities responsible. If a variant is legal, no hard rule blocked
that week in the final schedule; it is then attributed to the soft
buffer-proximity preference (if `buffer_proximity_conflicts` finds any) or to
sequencing behind other work.

REVIEW POINTS (judgment calls — flag if wrong):

1. **Causes describe the FINAL schedule, not the scheduler's state at the
   moment it decided.** An activity placed later in the sweep than the one being
   explained can appear as a blocker. That is still a true statement about the
   final plan ("that slot is held by A012"), and it is what a reader needs, but
   it is not a claim about decision order.
2. **"Unavoidable" (structural) overruns use an optimistic lower bound**:
   earliest legal week + ceil(nights needed / (nights per week x yield per
   night)) - 1, ignoring all contention. If even that bound is past the planned
   completion week, the overrun cannot be fixed under the stated rules (one
   night a week when the strict reading is on, contract cap otherwise, no ECLO in
   Scenario A). The earliest legal week itself uses the predecessor's ACTUAL
   finish, so a chain delayed by contention is reported as such in the
   predecessor's own explanation, not hidden.
3. **Scenario B/C "lever" sentences state what the scenario rules allow**
   (e.g. B: capacity is soft at 7 per excess night), not a proof that the
   scheduler compared alternatives.
4. **`summary` is deliberately bounded** (`MAX_SUMMARY_LINES`), ordered by
   severity; nothing is lost — every event is in `events`/`by_activity`.
5. **Imports two private helpers from self_check** (`_contract_overrun_days`,
   `_responsible_activity_nudge`) and one from greedy_scheduler
   (`_choose_group_for_location`) rather than copy them: the score attribution and
   the co-share heuristic must stay identical to what produced the numbers.
"""

import math
from pathlib import Path

import pandas as pd

from src.constraints import (
    SCENARIO_CONFIGS,
    ScenarioConfig,
    ScheduledAccess,
    ScheduleState,
    buffer_proximity_conflicts,
    check_all,
    week_of,
)
from src.data_model import Instance, load_instance
from src.greedy_scheduler import _choose_group_for_location
from src.self_check import (
    TIER_WEIGHT,
    _contract_overrun_days,
    _responsible_activity_nudge,
    build_scheduled_accesses,
    compute_capacity_hotspots,
    compute_objective_score,
    compute_soft_scores,
    load_submission_csvs,
    replay_submission,
    scenario_from_results,
)

MAX_SUMMARY_LINES = 16
_MAX_DIAGNOSED_WEEKS = 12  # cap on weeks re-checked per activity (keeps very long delays cheap)
_MAX_CAUSE_GROUPS = 3  # cause groups spelled out per sentence; the rest are counted
_ECLO_COST = 5  # PS1_README §2.5
_EXCESS_COST = 7  # PS1_README §2.5

_RULE_ORDER = ["predecessor", "capacity", "legal_mix", "closure", "weekly_allocation", "workfront", "one_per_week", "eclo", "eclo_continuity", "planned_date", "planned_start"]

_VIOLATION_LABELS = {
    "capacity": "location over its capacity",
    "legal_mix": "illegal possession mix (PM alone / 1 PC + 3 C / 4 C)",
    "closure": "collision with a Live closure (mirror / interchange crossover)",
    "weekly_allocation": "contract used more access-nights in a week than its allocation",
    "workfront": "too many concurrent activities on one night for the contract's workfronts",
    "planned_start": "access before the activity's planned start week",
    "predecessor": "access before its predecessor finished",
    "predecessor_cycle": "predecessor cycle in the input data",
    "workload": "activity not fully scheduled (workload conservation)",
    "eclo": "ECLO used where the scenario forbids it",
    "eclo_continuity": "ECLO nights spread wider than the 2-week window",
    "planned_date": "finished after a hard planned completion date",
    "one_per_week": "more than one access for an activity in a week (strict mode)",
}


# ----------------------------------------------------------------------
# Small formatting helpers
# ----------------------------------------------------------------------


def _fmt_weeks(weeks: list[int]) -> str:
    """[15, 16, 18] -> 'wk15-16, wk18'."""
    weeks = sorted(set(weeks))
    runs, start, prev = [], None, None
    for w in weeks:
        if start is None:
            start = prev = w
        elif w == prev + 1:
            prev = w
        else:
            runs.append((start, prev))
            start = prev = w
    if start is not None:
        runs.append((start, prev))
    return ", ".join(f"wk{a}" if a == b else f"wk{a}-{b}" for a, b in runs)


def _join(items: list[str], limit: int = 4) -> str:
    items = list(items)
    if len(items) > limit:
        return ", ".join(items[:limit]) + f" and {len(items) - limit} more"
    if len(items) > 1:
        return ", ".join(items[:-1]) + " and " + items[-1]
    return items[0] if items else "other work"


def _loc_label(location: str) -> str:
    return f"tunnel sector {location}" if location.startswith("SEC:") else f"platform {location}"


class _Ctx:
    """Read-only lookups shared by every explanation function."""

    def __init__(self, instance: Instance, scenario: ScenarioConfig, state: ScheduleState):
        self.instance, self.scenario, self.state = instance, scenario, state
        self.activities = instance.activities.set_index("activity_id")
        self.contracts = instance.contracts.set_index("contract_number")
        self.supply = dict(zip(instance.location_supply["location_id"], instance.location_supply["supply_capacity"]))

    def contract_of(self, activity_id: str) -> str:
        return self.activities.loc[activity_id, "contract_number"]

    def priority_of(self, contract_number: str) -> int:
        return int(self.contracts.loc[contract_number, "contract_priority"])

    def cap_of(self, contract_number: str) -> int:
        return int(self.contracts.loc[contract_number, "number_of_maximum_access_per_week"])

    def deadline_week(self, contract_number: str) -> int:
        return week_of(str(self.contracts.loc[contract_number, "planned_completion_date"]), self.instance)

    def who(self, activity_id: str) -> str:
        if activity_id not in self.activities.index:
            return activity_id
        cn = self.contract_of(activity_id)
        return f"{activity_id} ({cn}, priority {self.priority_of(cn)})"

    def earliest_legal(self, activity_id: str) -> tuple[int, int, dict | None]:
        """(earliest legal week, planned-start week, predecessor gate or None) — rules 2 and 3 of §2.4."""
        planned = week_of(str(self.activities.loc[activity_id, "planned_start_date"]), self.instance)
        earliest, gate = planned, None
        pred_id = self.activities.loc[activity_id, "predecessor_activity_id"]
        if pd.notna(pred_id):
            pred_accesses = self.state.for_activity(pred_id)
            if pred_accesses:
                pred_last = max(a.week for a in pred_accesses)
                if pred_last + 1 > planned:
                    earliest, gate = pred_last + 1, {"predecessor": pred_id, "predecessor_last_week": pred_last}
        return earliest, planned, gate

    def state_without(self, activity_id: str) -> ScheduleState:
        base = ScheduleState(self.instance)
        base.accesses = [a for a in self.state.accesses if a.activity_id != activity_id]
        return base


# ----------------------------------------------------------------------
# Cause reconstruction — the core of the module (see "HOW CAUSES ARE FOUND")
# ----------------------------------------------------------------------


def _diagnose_week(ctx: _Ctx, activity_id: str, week: int, base: ScheduleState) -> dict:
    """Why could `activity_id` not have worked in `week`, given everything else in the final schedule?"""
    own0 = ctx.state.for_activity(activity_id)[0]
    contract = own0.contract_number
    groups = {loc: _choose_group_for_location(loc, week, own0.access_type, activity_id, base) for loc in own0.locations}
    eclo_options = (False, True) if ctx.scenario.eclo_allowed else (False,)

    failing_variants = []
    for eclo in eclo_options:
        for night in range(1, ctx.cap_of(contract) + 1):
            cand = ScheduledAccess(activity_id, contract, own0.access_type, own0.nature_of_works, 1, week, eclo, night, own0.locations, groups)
            failing = [r for r in check_all(cand, base, ctx.instance, ctx.scenario) if not r.legal]
            if not failing:
                conflicts = buffer_proximity_conflicts(cand, base, ctx.instance)
                if conflicts:
                    cause = {"rule": "buffer_preference", "kind": "soft", "location": "", "blockers": sorted({a.activity_id for a in conflicts})}
                else:
                    cause = {"rule": "sequencing", "kind": "sequencing", "location": "", "blockers": []}
                return {"week": week, "placeable": True, "causes": [{**cause, "week": week}]}
            failing_variants.append(failing)

    common = set.intersection(*[{r.rule for r in f} for f in failing_variants])
    if common:
        source = failing_variants[0]
        chosen = [r for r in source if r.rule in common]
    else:  # variants fail for different reasons (e.g. standard blocked by nights, ECLO blocked by window)
        chosen = min(failing_variants, key=len)
    chosen.sort(key=lambda r: _RULE_ORDER.index(r.rule) if r.rule in _RULE_ORDER else len(_RULE_ORDER))

    causes = []
    for r in chosen:
        cause = {"rule": r.rule, "kind": "hard", "week": week, "location": r.location, "blockers": list(r.blockers), "detail": r.reason}
        if r.rule == "capacity" and r.location:
            used = {a.co_share_group[r.location] for a in base.at_location_week(r.location, week) if r.location in a.co_share_group}
            cause["slots_used"], cause["capacity"] = len(used), int(ctx.supply.get(r.location, 0))
        causes.append(cause)
    return {"week": week, "placeable": False, "causes": causes}


def _extra_night_open(ctx: _Ctx, activity_id: str, week: int) -> bool:
    """Could `activity_id` have taken one MORE access-night in `week` (same possession) without breaking any rule?"""
    own_week = [a for a in ctx.state.for_activity(activity_id) if a.week == week]
    if not own_week:
        return False
    used = {a.access_night for a in own_week}
    own0 = own_week[0]
    for eclo in ((False, True) if ctx.scenario.eclo_allowed else (False,)):
        for night in range(1, ctx.cap_of(own0.contract_number) + 1):
            if night in used:
                continue
            cand = ScheduledAccess(activity_id, own0.contract_number, own0.access_type, own0.nature_of_works, 1, week, eclo, night, own0.locations, dict(own0.co_share_group))
            if all(r.legal for r in check_all(cand, ctx.state, ctx.instance, ctx.scenario)):
                return True
    return False


def _cause_phrase(ctx: _Ctx, activity_id: str, cause: dict) -> str:
    rule, loc = cause["rule"], cause.get("location", "")
    who = _join([ctx.who(b) for b in cause.get("blockers", [])]) if cause.get("blockers") else ""
    contract = ctx.contract_of(activity_id)
    if rule == "capacity":
        used, cap = cause.get("slots_used"), cause.get("capacity")
        fullness = f" ({used} of {cap} slots)" if used is not None else ""
        return f"{_loc_label(loc)} was at full capacity{fullness}" + (f", held by {who}" if who else "")
    if rule == "legal_mix":
        return f"{_loc_label(loc)} could not admit another crew of this type (one PM alone, or 1 PC + 3 C, or 4 C)" + (f"; it was used by {who}" if who else "")
    if rule == "closure":
        return f"{_loc_label(loc)} was closed by Live work ({who}) — opposite-bound mirror / interchange crossover" if who else f"{_loc_label(loc)} was closed by Live work"
    if rule == "weekly_allocation":
        return f"contract {contract}'s {ctx.cap_of(contract)} weekly access-night(s) were already taken" + (f" by {who}" if who else "")
    if rule == "workfront":
        wf = int(ctx.contracts.loc[contract, "number_of_workfronts"])
        return f"contract {contract}'s {wf} workfront(s) were busy on every available night" + (f" with {who}" if who else "")
    if rule == "predecessor":
        return f"its predecessor {who or 'activity'} had not finished yet"
    if rule == "one_per_week":
        return "it already had an access that week (strict one-access-per-week reading)"
    if rule == "eclo_continuity":
        return "an ECLO night there would break the 2-week ECLO window (Scenario C)"
    if rule == "eclo":
        return "only an ECLO night was open and ECLO is forbidden in this scenario"
    if rule == "planned_date":
        return "that week is past the contract's hard planned completion date"
    if rule == "buffer_preference":
        return f"it was held back to stay clear of the buffer footprint of {who} (soft preference, not a rule)"
    if rule == "sequencing":
        return "no hard rule blocked that week in the final schedule — it was simply scheduled later"
    return cause.get("detail", rule)


def _group_causes(diagnoses: list[dict]) -> list[dict]:
    """Merge consecutive weeks with the same cause: [{'weeks': [...], 'cause': {...}}, ...] in week order."""
    grouped: dict[tuple, dict] = {}
    for d in diagnoses:
        primary = d["causes"][0]  # most important cause for that week
        sig = (primary["rule"], primary.get("location", ""), tuple(primary.get("blockers", [])))
        entry = grouped.setdefault(sig, {"weeks": [], "cause": primary, "all_causes": d["causes"]})
        entry["weeks"].append(d["week"])
    return sorted(grouped.values(), key=lambda g: g["weeks"][0])


def _causes_sentence(ctx: _Ctx, activity_id: str, groups: list[dict]) -> str:
    parts = [f"{_fmt_weeks(g['weeks'])}: {_cause_phrase(ctx, activity_id, g['cause'])}" for g in groups[:_MAX_CAUSE_GROUPS]]
    text = "; ".join(parts)
    if len(groups) > _MAX_CAUSE_GROUPS:
        text += f"; and {len(groups) - _MAX_CAUSE_GROUPS} other blocked stretch(es)"
    return text


# ----------------------------------------------------------------------
# Capacity pressure + scenario trade-offs (statistics — unchanged from the first Phase 6 version)
# ----------------------------------------------------------------------


def explain_capacity_pressure(hotspots: list[dict], state: ScheduleState) -> dict:
    """`hotspots` is self_check.compute_capacity_hotspots's own output — not recomputed."""
    binding = [h for h in hotspots if h["excess"] > 0]
    at_capacity = [h for h in hotspots if h["excess"] == 0]

    frequency: dict[str, int] = {}
    for h in hotspots:
        frequency[h["location_id"]] = frequency.get(h["location_id"], 0) + 1
    top_locations = sorted(({"location_id": loc, "count": count} for loc, count in frequency.items()), key=lambda x: -x["count"])[:5]

    binding_detail = []
    for h in binding:
        occupants = state.at_location_week(h["location_id"], h["week"])
        binding_detail.append({**h, "activities": sorted({a.activity_id for a in occupants})})

    return {
        "total_hotspots": len(hotspots),
        "binding_count": len(binding),
        "at_capacity_count": len(at_capacity),
        "top_locations": top_locations,
        "binding_detail": binding_detail,
    }


def explain_scenario_tradeoffs(soft_scores: dict, hotspots: list[dict], scenario_name: str) -> dict:
    excess = soft_scores["excess_access_nights_total"]
    eclo = soft_scores["eclo_nights_total"]
    overrun = soft_scores["overrun_days_total"]
    binding = [h for h in hotspots if h["excess"] > 0]

    parts = []
    if overrun:
        parts.append(f"{overrun} day(s) of completion overrun across {soft_scores['contracts_overrunning']} contract(s) (priority-weighted score {soft_scores['priority_weighted_score']:g})")
    if excess:
        locs = sorted({h["location_id"] for h in binding})
        parts.append(f"{excess:g} excess access-night(s) across {len(binding)} location-week(s) ({', '.join(locs) if locs else 'unlisted locations'})")
    if eclo:
        parts.append(f"{eclo} ECLO night(s) used to extend working hours")
    narrative = f"Scenario {scenario_name} spent its flexibility on: " + "; ".join(parts) + "." if parts else f"Scenario {scenario_name} needed no flexibility: no overrun, no excess access-nights and no ECLO nights."

    return {"excess_access_nights_total": excess, "eclo_nights_total": eclo, "overrun_days_total": overrun, "binding_hotspot_count": len(binding), "narrative": narrative}


# ----------------------------------------------------------------------
# Per-activity: start delays
# ----------------------------------------------------------------------


def explain_activity_placements(state: ScheduleState, instance: Instance, scenario: ScenarioConfig | None = None) -> list[dict]:
    """One record per scheduled activity: earliest legal week, delay, reconstructed causes, sentences."""
    ctx = _Ctx(instance, scenario or SCENARIO_CONFIGS["A"], state)
    placements = []

    for activity_id in ctx.activities.index:
        own = sorted(state.for_activity(activity_id), key=lambda a: (a.week, a.access_night))
        if not own:
            continue  # not scheduled at all — reported as a workload violation, not here

        contract = ctx.contract_of(activity_id)
        earliest, planned, gate = ctx.earliest_legal(activity_id)
        first_week, last_week = own[0].week, max(a.week for a in own)
        nights_per_week: dict[int, set[int]] = {}
        for a in own:
            nights_per_week.setdefault(a.week, set()).add(a.access_night)
        peak_week, peak_nights = max(((wk, len(n)) for wk, n in nights_per_week.items()), key=lambda t: t[1])
        delay = first_week - earliest

        sentences, causes, delay_cause_text = [], [], ""
        span = _fmt_weeks([first_week]) if first_week == last_week else f"wk{first_week}-{last_week}"
        status = f"{activity_id} ({contract}): {len(own)} access-night(s), {span}"
        status += "; started in its earliest legal week." if delay == 0 else "."
        if delay < 0:
            status = f"{activity_id} ({contract}): started wk{first_week}, BEFORE its earliest legal wk{earliest} (see hard violations)."
        sentences.append(status)

        if gate:
            sentences.append(
                f"It could not start before wk{earliest}: planned start was wk{planned}, but predecessor {ctx.who(gate['predecessor'])} finishes in wk{gate['predecessor_last_week']} (finish-to-start, zero lag)."
            )

        if delay > 0:
            base = ctx.state_without(activity_id)
            blocked = list(range(earliest, first_week))
            diagnosed = [_diagnose_week(ctx, activity_id, w, base) for w in blocked[:_MAX_DIAGNOSED_WEEKS]]
            causes = _group_causes(diagnosed)
            delay_cause_text = _causes_sentence(ctx, activity_id, causes)
            text = f"{activity_id} ({contract}) started in wk{first_week}, {delay} week(s) after its earliest legal week (wk{earliest}). " + delay_cause_text + "."
            if len(blocked) > _MAX_DIAGNOSED_WEEKS:
                text += f" (Only the first {_MAX_DIAGNOSED_WEEKS} of {len(blocked)} weeks were analysed.)"
            sentences.append(text)

        placements.append(
            {
                "activity_id": activity_id,
                "contract_number": contract,
                "predecessor_activity_id": gate["predecessor"] if gate else (ctx.activities.loc[activity_id, "predecessor_activity_id"] if pd.notna(ctx.activities.loc[activity_id, "predecessor_activity_id"]) else None),
                "planned_start_week": planned,
                "earliest_legal_week": earliest,
                "actual_first_week": first_week,
                "finish_week": last_week,
                "delay_weeks": delay,
                "max_nights_in_one_week": peak_nights,
                "peak_week": peak_week,
                "used_eclo": any(a.eclo for a in own),
                "nights_scheduled": len(own),
                "nights_needed": float(ctx.activities.loc[activity_id, "total_accesses"]),
                "gate": gate,
                "delay_causes": [{"weeks": g["weeks"], **{k: g["cause"].get(k) for k in ("rule", "kind", "location", "blockers", "slots_used", "capacity")}} for g in causes],
                "delay_cause_text": delay_cause_text,
                "sentences": sentences,
            }
        )
    return placements


# ----------------------------------------------------------------------
# Per-contract: completion vs plan, structural vs contention
# ----------------------------------------------------------------------


def _best_finish_week(ctx: _Ctx, activity_id: str, earliest: int, with_eclo: bool) -> int:
    """Optimistic (contention-free) earliest finish week — Review point 2."""
    contract = ctx.contract_of(activity_id)
    per_week = 1 if ctx.scenario.one_access_per_activity_week else ctx.cap_of(contract)
    yield_per_night = 1.5 if (with_eclo and ctx.scenario.eclo_allowed) else 1.0
    needed = float(ctx.activities.loc[activity_id, "total_accesses"])
    return earliest + math.ceil(needed / (per_week * yield_per_night)) - 1


def explain_contract_outcomes(ctx: _Ctx, placements: list[dict]) -> list[dict]:
    placement_of = {p["activity_id"]: p for p in placements}
    overruns = _contract_overrun_days(ctx.state, ctx.instance)
    outcomes = []

    for contract_number in ctx.contracts.index:
        activity_ids = [a for a in ctx.activities.index[ctx.activities["contract_number"] == contract_number] if a in placement_of]
        if not activity_ids:
            outcomes.append({"contract_number": contract_number, "status": "not_scheduled", "overrun_days": 0, "sentences": [f"Contract {contract_number}: none of its activities were scheduled."], "cause": None})
            continue

        days = overruns.get(contract_number, 0)
        planned_week = ctx.deadline_week(contract_number)
        final_week = max(placement_of[a]["finish_week"] for a in activity_ids)
        tier = ctx.priority_of(contract_number)

        if days <= 0:
            note = f"finished wk{final_week}, {(-days)} day(s) before its planned completion." if days < 0 else f"finished wk{final_week}, exactly on its planned completion."
            outcomes.append({"contract_number": contract_number, "status": "early" if days < 0 else "on_time", "overrun_days": 0, "final_week": final_week, "planned_week": planned_week, "sentences": [f"Contract {contract_number} {note}"], "cause": None})
            continue

        # ---- late ----
        finishing = [a for a in activity_ids if placement_of[a]["finish_week"] == final_week]
        critical = min(finishing, key=lambda a: (int(ctx.activities.loc[a, "activity_priority"]), a))
        p = placement_of[critical]
        earliest, planned_start, gate = ctx.earliest_legal(critical)
        nudge = _responsible_activity_nudge(contract_number, final_week, ctx.state, ctx.instance)
        cost = TIER_WEIGHT[tier] * (1 + nudge) * days

        best = _best_finish_week(ctx, critical, earliest, with_eclo=True)
        limits = []
        if ctx.scenario.one_access_per_activity_week:
            limits.append("at most one access-night per activity per week")
        else:
            limits.append(f"the contract's cap of {ctx.cap_of(contract_number)} access-night(s) per week")
        if not ctx.scenario.eclo_allowed:
            limits.append("ECLO is forbidden in Scenario A")
        needed = float(ctx.activities.loc[critical, "total_accesses"])

        if best > planned_week:
            cause_type = "structural"
            why = (
                f"{critical} alone needs {needed:g} access-night(s) and cannot start before wk{earliest}"
                + (f" (predecessor {gate['predecessor']} finishes wk{gate['predecessor_last_week']})" if gate else f" (its planned start)")
                + f"; even working every possible night under {' and '.join(limits)}, it cannot finish before wk{best}, after the planned completion week wk{planned_week}. This overrun is unavoidable for this data under those rules."
            )
            causes_data = {"critical_activity": critical, "best_possible_finish_week": best}
        else:
            cause_type = "contention"
            base = ctx.state_without(critical)
            own_weeks = {a.week for a in ctx.state.for_activity(critical)}
            gap_weeks = [w for w in range(p["actual_first_week"], final_week) if w not in own_weeks][:_MAX_DIAGNOSED_WEEKS]
            diagnosed = [_diagnose_week(ctx, critical, w, base) for w in gap_weeks]
            gap_groups = _group_causes(diagnosed)
            pieces = []
            if p["delay_weeks"] > 0:
                pieces.append(f"it started {p['delay_weeks']} week(s) late (wk{p['actual_first_week']} vs earliest legal wk{earliest}) — {p['delay_cause_text']}")
            if gap_groups:
                pieces.append("it then paused while " + _causes_sentence(ctx, critical, gap_groups))
            if not pieces:
                cap = ctx.cap_of(contract_number)
                own_by_week: dict[int, int] = {}
                for a in ctx.state.for_activity(critical):
                    own_by_week[a.week] = own_by_week.get(a.week, 0) + 1
                open_weeks = [w for w, n in own_by_week.items() if n < cap and _extra_night_open(ctx, critical, w)]
                if open_weeks:
                    cause_type = "pace"  # not blocked by any rule: the schedule simply used fewer nights than were available
                    typical = max(set(own_by_week.values()), key=list(own_by_week.values()).count)
                    faster = _best_finish_week(ctx, critical, earliest, with_eclo=False) if not ctx.scenario.eclo_allowed else _best_finish_week(ctx, critical, earliest, with_eclo=True)
                    pieces.append(
                        f"it worked every week from wk{earliest} to wk{final_week} but only {typical} night(s) a week, although its contract allows {cap} and an extra night was still legal in {len(open_weeks)} of those {len(own_by_week)} weeks; "
                        f"using them it could have finished by about wk{faster}. The overrun comes from the pace this schedule chose, not from a hard rule"
                    )
                else:
                    pieces.append(f"it worked every week from its earliest legal week (wk{earliest}) to wk{final_week}, and no extra night was open to it; the contract's weekly allocation ({cap} night(s) per week, shared by its activities) and the {needed:g} nights required set the pace")
            why = f"The last activity to finish was {critical}: " + "; ".join(pieces) + "."
            causes_data = {"critical_activity": critical, "gap_causes": [{"weeks": g["weeks"], "rule": g["cause"]["rule"], "location": g["cause"].get("location"), "blockers": g["cause"].get("blockers")} for g in gap_groups]}

        if ctx.scenario.date_hard:
            lever = f"Scenario B treats the planned completion date as a hard rule, so this breaks it (rule tag planned_date)."
        else:
            lever = f"Scenario {ctx.scenario.name} lets the completion date slip; the cost is {cost:g} (priority-{tier} weight {TIER_WEIGHT[tier]} x {days} day(s)" + (f" x {1 + nudge:g} activity-priority nudge" if nudge else "") + ")."
        sentence = f"Contract {contract_number} finished {days} day(s) after its planned completion (wk{final_week} vs wk{planned_week}). {why} {lever}"
        outcomes.append(
            {
                "contract_number": contract_number,
                "status": "late",
                "overrun_days": days,
                "final_week": final_week,
                "planned_week": planned_week,
                "priority": tier,
                "cost": round(cost, 4),
                "cause": {"type": cause_type, **causes_data},
                "sentences": [sentence],
            }
        )
    return outcomes


# ----------------------------------------------------------------------
# ECLO and excess capacity
# ----------------------------------------------------------------------


def explain_eclo_use(ctx: _Ctx, placements: list[dict]) -> list[dict]:
    events = []
    for p in placements:
        if not p["used_eclo"]:
            continue
        aid = p["activity_id"]
        eclo_weeks = sorted(a.week for a in ctx.state.for_activity(aid) if a.eclo)
        contract = p["contract_number"]
        deadline = ctx.deadline_week(contract)
        std_finish = _best_finish_week(ctx, aid, p["earliest_legal_week"], with_eclo=False)
        cost = _ECLO_COST * len(eclo_weeks)
        if std_finish > deadline:
            reason = f"standard nights alone could not finish before wk{std_finish}, after the planned completion week wk{deadline}, so ECLO was needed to shorten it"
            forced = True
        else:
            reason = "standard nights alone would meet the deadline on paper, so ECLO here reflects congestion around the activity rather than deadline arithmetic"
            forced = False
        lever = "Scenario B's dates are hard, so ECLO is the lever" if ctx.scenario.date_hard else f"Scenario {ctx.scenario.name} allows ECLO at {_ECLO_COST} per night"
        events.append(
            {
                "type": "eclo_use",
                "severity": float(cost),
                "activity_id": aid,
                "contract_number": contract,
                "sentence": f"{aid} ({contract}) used {len(eclo_weeks)} ECLO night(s) ({_fmt_weeks(eclo_weeks)}), cost {cost}: {reason}. {lever}.",
                "data": {"eclo_weeks": eclo_weeks, "cost": cost, "forced_by_deadline": forced, "standard_only_finish_week": std_finish, "deadline_week": deadline},
            }
        )
    return events


def explain_excess_capacity(ctx: _Ctx, pressure: dict) -> list[dict]:
    events = []
    for h in pressure["binding_detail"]:
        who = _join([ctx.who(a) for a in h["activities"]])
        if ctx.scenario.capacity_mode == "soft_above_1":
            rule = "Scenario C tolerates 1 excess access-night per location-week"
        else:
            rule = "Scenario B treats capacity as soft"
        cost = _EXCESS_COST * h["excess"]
        events.append(
            {
                "type": "excess_capacity",
                "severity": float(cost),
                "sentence": f"wk{h['week']}: {_loc_label(h['location_id'])} carried {h['slots_used']} slot(s) against a capacity of {h['supply_capacity']} ({h['excess']} excess, cost {cost}), shared by {who}. {rule}, at {_EXCESS_COST} per excess night, so the scheduler accepted it rather than push work past its dates.",
                "data": {**h},
            }
        )
    return events


# ----------------------------------------------------------------------
# Hard violations (infeasible submissions)
# ----------------------------------------------------------------------


def explain_hard_violations(violations: list[dict]) -> list[dict]:
    """`violations` = [{'rule', 'detail'}] as in self_check's report. One event per rule tag, with counts and examples."""
    by_rule: dict[str, list[str]] = {}
    for v in violations:
        by_rule.setdefault(v["rule"], []).append(v.get("detail", ""))
    events = []
    for rule, details in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        label = _VIOLATION_LABELS.get(rule, rule)
        examples = "; ".join(details[:2])
        more = f" (+{len(details) - 2} more)" if len(details) > 2 else ""
        events.append(
            {
                "type": "hard_violation",
                "severity": 1000.0 + len(details),
                "sentence": f"{len(details)} hard violation(s) of rule '{rule}' — {label}. e.g. {examples}{more}",
                "data": {"rule": rule, "count": len(details), "examples": details[:5]},
            }
        )
    return events


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------


def summarize(explanation: dict) -> list[str]:
    """Bounded, severity-ordered headline lines (Review point 4). Everything else stays in `events`."""
    scenario, feasible = explanation["scenario"], explanation["feasible"]
    pressure, tradeoffs = explanation["capacity_pressure"], explanation["scenario_tradeoffs"]
    events = explanation["events"]

    head = f"Scenario {scenario}: " + ("feasible schedule" if feasible else f"INFEASIBLE schedule ({sum(e['data']['count'] for e in events if e['type'] == 'hard_violation')} hard violation(s))")
    if explanation.get("objective_score") is not None:
        head += f", objective score {explanation['objective_score']:g}"
    head += f"; {pressure['total_hotspots']} location-week(s) at or above capacity ({pressure['binding_count']} exceeding it, {pressure['at_capacity_count']} exactly full)."
    lines = [head]

    for e in [e for e in events if e["type"] == "hard_violation"][:3]:
        lines.append(e["sentence"])
    lines.append(tradeoffs["narrative"])
    for e in [e for e in events if e["type"] == "contract_overrun"][:3]:
        lines.append(e["sentence"])
    for e in [e for e in events if e["type"] == "eclo_use"][:2]:
        lines.append(e["sentence"])
    for e in [e for e in events if e["type"] == "excess_capacity"][:2]:
        lines.append(e["sentence"])
    for e in [e for e in events if e["type"] == "start_delay"][:3]:
        lines.append(e["sentence"])
    top = pressure["top_locations"][:3]
    if top:
        lines.append("Most contested locations across the horizon: " + ", ".join(f"{t['location_id']} ({t['count']}x)" for t in top) + ".")
    return lines[:MAX_SUMMARY_LINES]


def build_explanation(instance: Instance, submission_dir: Path, scenario: ScenarioConfig | None = None) -> dict:
    """`scenario` overrides the config looked up from RESULTS.csv — see self_check.build_report."""
    access_df, occupancy_df, results_df = load_submission_csvs(submission_dir)
    scenario_name = scenario_from_results(results_df)
    scenario = scenario or SCENARIO_CONFIGS[scenario_name]

    accesses = build_scheduled_accesses(access_df, occupancy_df, instance)
    state, results = replay_submission(instance, accesses, scenario)
    violations = [{"rule": r.rule, "detail": r.reason} for r in results if not r.legal]
    feasible = not violations

    soft_scores = compute_soft_scores(state, instance, scenario_name)
    hotspots = compute_capacity_hotspots(state, instance)
    ctx = _Ctx(instance, scenario, state)

    pressure = explain_capacity_pressure(hotspots, state)
    placements = explain_activity_placements(state, instance, scenario)
    outcomes = explain_contract_outcomes(ctx, placements)

    events = explain_hard_violations(violations)
    for o in outcomes:
        if o["status"] == "late":
            events.append({"type": "contract_overrun", "severity": o["cost"] if not scenario.date_hard else 1000.0 + o["overrun_days"], "contract_number": o["contract_number"], "activity_id": o["cause"]["critical_activity"], "sentence": o["sentences"][0], "data": {k: o[k] for k in ("overrun_days", "final_week", "planned_week", "priority", "cost", "cause")}})
    for p in placements:
        if p["delay_weeks"] > 0:
            events.append({"type": "start_delay", "severity": float(p["delay_weeks"]), "activity_id": p["activity_id"], "contract_number": p["contract_number"], "sentence": p["sentences"][-1], "data": {k: p[k] for k in ("earliest_legal_week", "actual_first_week", "delay_weeks", "delay_causes")}})
    events += explain_eclo_use(ctx, placements)
    events += explain_excess_capacity(ctx, pressure)
    events.sort(key=lambda e: -e["severity"])

    by_activity = {p["activity_id"]: list(p["sentences"]) for p in placements}
    for e in events:
        aid = e.get("activity_id")
        if aid and e["type"] in ("contract_overrun", "eclo_use"):
            by_activity.setdefault(aid, []).append(e["sentence"])
        if e["type"] == "excess_capacity":
            for aid in e["data"].get("activities", []):
                by_activity.setdefault(aid, []).append(e["sentence"])
    by_contract = {o["contract_number"]: list(o["sentences"]) for o in outcomes}

    objective = compute_objective_score(soft_scores, scenario_name) if feasible else None
    explanation = {
        "scenario": scenario_name,
        "feasible": feasible,
        "objective_score": objective,
        "capacity_pressure": pressure,
        "scenario_tradeoffs": explain_scenario_tradeoffs(soft_scores, hotspots, scenario_name),
        "activity_placements": placements,
        "contract_outcomes": outcomes,
        "events": events,
        "by_activity": by_activity,
        "by_contract": by_contract,
    }
    explanation["summary"] = summarize(explanation)
    return explanation


def main() -> None:
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print("usage: python -m src.explain <submission_dir> [--all]   (--all also prints every event and per-activity sentence)")
        sys.exit(2)

    instance = load_instance()
    explanation = build_explanation(instance, Path(args[0]))
    for line in explanation["summary"]:
        print(line)
    if "--all" in sys.argv:
        print("\n== all events ==")
        for e in explanation["events"]:
            print(f"[{e['type']}] {e['sentence']}")
        print("\n== by activity ==")
        for aid, sentences in explanation["by_activity"].items():
            for s in sentences:
                print(f"{aid}: {s}")
    sys.exit(0 if explanation["feasible"] else 1)


if __name__ == "__main__":
    main()
