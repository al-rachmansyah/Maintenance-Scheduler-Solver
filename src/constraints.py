"""
Constraints — Phase 1.5 (Role A + B), built before any scheduler.

Shared library of pure legality-check functions. The greedy scheduler, the
self-check validator, and (informing) the CP-SAT model all call into this
ONE module — see docs/decisions.md "Design philosophy" #4. Nothing in here
mutates anything; every check_* function takes the schedule committed SO
FAR (a ScheduleState) plus ONE candidate access, and returns whether that
candidate is legal to add. The caller (scheduler or validator) decides what
to do with an illegal result — this module only ever answers "is this
placement legal, and why/why not."

Ground truth used to validate this module: data/sample_instance/
03_submission_sample/ is a known-FEASIBLE Scenario A submission. Every
check_* function here should report zero hard violations when replayed
against it — see tests/test_constraints.py.

======================================================================
REVIEW POINTS (judgment calls made while writing this — please check
these against PS1_README.md yourself; they are NOT settled facts):
======================================================================

1. **Interchange crossover closes BOTH bounds of the other line.**
   §2.2 says a Live closure "also closes the other line's H01_H02 tunnel
   sector and H01/H02 platforms" — it doesn't say whether that's one bound
   or both. I close both bounds of the other line's H01_H02 tunnel and both
   platforms (6 locations total), on the theory that cutting traction power
   at a physically-shared interchange affects both tracks regardless of
   which bound the original activity was on. Empirically: neither sample
   Live activity (A074, A075) has anything scheduled on the locations this
   would have closed, in the sample submission — consistent with, but not
   proof of, this reading (see the grep check I ran before writing this).

2. **Mirroring/crossover produces no occupancy rows of its own.**
   Confirmed from data, not a guess: A074/A075's rows in the sample
   submission's SCHEDULE_OCCUPANCY.csv list ONLY their own line/bound path,
   never the mirrored or crossed-over locations. So `_closure_zone()` below
   is used purely to check exclusivity against OTHER activities — it must
   never be written out as if the activity itself occupies those locations.

3. **Scenario B/C completion-deadline check is a necessary, not sufficient,
   per-access condition.** `check_completion_deadline` (Scenario B only,
   hard) rejects any SINGLE access whose week falls after the contract's
   `planned_completion_date`. It cannot, by itself, guarantee the
   contract's LAST access lands on time — that's the scheduler's job
   (plan backwards from the deadline). This function only prevents the
   scheduler from ever placing an access that's already too late.

4. **ECLO continuity window (Scenario C, §2.4 rule 10) is checked per line,
   using only the accesses whose closure zone touches that line** — for a
   cross-line Live activity, both lines' windows are checked independently
   per the brief's own wording ("chosen independently per line").

5. **Legal-mix and capacity are treated as two separate rules** even
   though PS1_README's rule 5 states them together, because they have
   different scenario-dependence: legal-mix (PM alone / PC+3C / 4C) is
   hard in every scenario; capacity (slot count vs LOCATION_SUPPLY) is the
   scenario's flex point. Keeping them separate means SCENARIO_CONFIGS only
   has to touch one function.

6. **`week_of()`'s date -> week-number formula** (`(date - horizon_start).
   days // 7 + 1`) is derived, not given directly by any column — checked
   against the sample submission (A001's planned_start_date week is 21,
   sample first schedules it at week 22, i.e. later, which is consistent
   with rule 2 but doesn't independently prove the formula's offset is
   exactly right). Worth a second sanity check against another activity
   if you want more confidence before relying on it heavily.

======================================================================
SETTLED BY EVIDENCE (2026-09-19) — the buffer-sector-extension reading
was WRONG, not merely a kept-literal ambiguity. Reversing an earlier
"keep literal pending organizer clarification" decision:
======================================================================

Reported back as "the scheduling is terrible... reduce overruns to
zero" after the real instance's greedy/CP-SAT scores came in ~26x worse
than the organizer's own sample submission (835.8 vs 32.2
priority_weighted_score, Scenario A). Instrumented `check_all()` during
a live greedy run and found "closure" (buffer/mirroring) rejections
outnumbered every other rule's rejections COMBINED, by nearly 3:1 (456
vs 164). That pointed straight back at the already-known "34 false
violations against the feasible sample" discrepancy from the earlier
session — previously treated as a minor, contained granularity question;
this session's evidence showed it was actually the dominant cause of bad
scheduling, not a cosmetic footnote.

Tested three versions, each verified against BOTH the sample replay and
a real greedy run, not assumed:
1. Buffer/mirroring fully disabled: 0 violations vs. the sample, greedy's
   Scenario A score 25.2 (better than the sample's own 32.2).
2. Buffer's sector EXTENSION dropped, but same-location-vs-different-
   activity overlap still checked (own span only, no extension): 11
   violations remained, score 74.2 — better, not conclusive.
3. Buffer's sector extension dropped, AND the leftover own-span-vs-own-
   span check removed too (recognizing it was redundant with
   check_capacity/check_legal_mix once there's no extension — see
   `check_buffer_exclusion`'s docstring): **0 violations vs. the sample,
   and greedy's Scenario A score is 32.2 — an EXACT match to the
   organizer's own reference score**, not just "close." That exact match
   is what moved this from "plausible fix" to "confirmed."

Live-rail mirroring/interchange-crossover (§2.2, `check_live_mirroring`)
is UNCHANGED and unaffected — it's a separate rule with its own
independent evidence (the real Live activities' occupancy pattern), not
implicated by any of the above.
`check_buffer_exclusion` is kept as an explicit function returning
always-legal (not deleted), so PS1_README's rule 4 stays individually
traceable if the organizer's own answer ever says otherwise.

======================================================================
SETTLED BY THE 2026-09-18 PS1_README.md UPDATE (no longer a judgment call
— see docs/decisions.md's "Settled" section):
======================================================================

Predecessor precedence is now official §2.4 rule 3: "finish-to-start, zero
lag (FS+0). 'Finished' = the week of the predecessor's last scheduled
access night; the successor's first scheduled access night must fall in a
strictly later week. Cross-contract predecessor links are allowed;
predecessor cycles are not." This confirms our provisional week-
granularity-strictness assumption exactly — `check_predecessor_completion`
below is unchanged. What IS new: "predecessor cycles are not allowed" was
never checked anywhere before this update — see `check_predecessor_acyclic`,
a one-time whole-instance validation (not a per-candidate check, since a
cycle is a property of the data, not of any single placement). Callers
(scheduler, self-check) should call it once at startup, before scheduling
anything.

======================================================================
ADDED WHILE DESIGNING src/self_check.py (2026-09-18):
======================================================================

Rule 1 (§2.4), "100% of activities scheduled, nightly yields >=
total_accesses," had no check anywhere in this module — a real gap, found
because self_check.py needs to verify it and nothing existed to call. See
`check_workload_conservation`/`check_workload_conservation_all`, same
shape as `check_predecessor_acyclic`: whole-schedule, called once, not
part of `check_all()`. Also added `week_end_date()` (exact inverse of
`week_of()`, next to it) and `_activity_yield()` (extracted from what was
duplicated inline in `check_predecessor_completion`) for output_writer.py/
self_check.py to reuse rather than re-deriving.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from src.data_model import Instance, parse_location_id

# ----------------------------------------------------------------------
# Scenario configuration — one object, read by every check_* function that
# has scenario-dependent behaviour. See docs/decisions.md's "which rules
# are hard" resolution and Design philosophy #3/#5.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioConfig:
    name: str
    capacity_mode: str  # "hard" | "soft_above_1" | "unlimited_soft"
    date_hard: bool  # True only for Scenario B
    eclo_allowed: bool  # False only for Scenario A
    eclo_continuity_required: bool  # True only for Scenario C
    # RECONSIDERED 2026-09-20 (see docs/decisions.md — reversed back to False after
    # a user challenge to the 2026-09-20 "reinstated as default" call, re-reading the
    # FULL rule set rather than rule 10 in isolation). Rule 10's "an activity gets at
    # most one access-night per week" sits entirely inside a rule titled "ECLO
    # Continuity Window (Scenario C only)", used only to derive that rule's own "caps
    # any single activity at 2 ECLO nights within C" — i.e. plausibly scoped to ECLO
    # nights specifically, not a universal cap. Every OTHER rule that governs weekly
    # access-night usage (rule 7 "Weekly Allocation", rule 8 "Workfronts", rule 6
    # "Co-Sharing Exemption", §2.1 point 4, and §2.6's own access_night description)
    # is phrased at the (contract, access_type, week) grain — a cap on DISTINCT
    # access_night values used by the whole contract+type, never a cap on a single
    # activity's own count. Default False = permissive (an activity may use more than
    # one of its contract's granted weekly nights, still capped at
    # number_of_maximum_access_per_week distinct nights and number_of_workfronts
    # concurrent activities per night — rules 7/8 unchanged). True is kept as an
    # explicit, opt-in STRICT reading for anyone who wants to re-run the experiment
    # or if the organizer's own answer ever confirms the universal reading.
    one_access_per_activity_week: bool = False


SCENARIO_CONFIGS: dict[str, ScenarioConfig] = {
    "A": ScenarioConfig("A", capacity_mode="hard", date_hard=False, eclo_allowed=False, eclo_continuity_required=False),
    "B": ScenarioConfig("B", capacity_mode="unlimited_soft", date_hard=True, eclo_allowed=True, eclo_continuity_required=False),
    "C": ScenarioConfig("C", capacity_mode="soft_above_1", date_hard=False, eclo_allowed=True, eclo_continuity_required=True),
}

# ----------------------------------------------------------------------
# Result type
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class LegalityResult:
    legal: bool
    rule: str  # short tag; mirrors PS1_README §2.7's report tags where one exists
    reason: str = ""
    excess: float = 0.0  # only meaningful for check_capacity
    # Structured cause data for src/explain.py (added with the Phase 6 rewrite): WHERE the rule bit
    # and WHICH activities were responsible, so explanations never have to parse `reason` strings.
    location: str = ""
    blockers: tuple = ()  # activity_ids

    @staticmethod
    def ok(rule: str) -> "LegalityResult":
        return LegalityResult(True, rule)

    @staticmethod
    def fail(rule: str, reason: str, excess: float = 0.0, location: str = "", blockers: tuple = ()) -> "LegalityResult":
        return LegalityResult(False, rule, reason, excess, location, tuple(blockers))


# ----------------------------------------------------------------------
# Schedule representation
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduledAccess:
    """
    One committed access-night for one activity — the unit both
    SCHEDULE_ACCESS.csv and SCHEDULE_OCCUPANCY.csv rows come from.

    `co_share_group` is keyed per location because the sample submission
    confirms a single activity's access-night can sit in DIFFERENT
    co_share_groups at different locations along its own path in the same
    week (e.g. A003 week 12: PLAT:BET:S15:EB is group b2 while
    SEC:BET:H01_H02:EB is group b1) — so it is never a single label per
    access, only per (access, location).
    """

    activity_id: str
    contract_number: str
    access_type: str  # PM / PC / C
    nature_of_works: str  # Live / Non-live (Consist) / Non-live (Others)
    access_seq: int  # 1..total_accesses for this activity
    week: int
    eclo: bool
    access_night: int  # 1..cap, local index within (contract, week)
    locations: tuple[str, ...]  # this access-night's own resolved path (own line/bound only)
    co_share_group: dict[str, str]  # location_id -> group label, one choice per location


class ScheduleState:
    """
    Mutable accumulator of everything committed so far. check_* functions
    only ever READ from this; the caller (scheduler/validator) calls
    `.add()` itself, and only after every relevant check_* has passed.

    Linear scans throughout — fine at this instance's scale (54 activities,
    30-week horizon); revisit with indices if ever run against a much
    bigger hidden instance and profiling says it matters.
    """

    def __init__(self, instance: Instance):
        self.instance = instance
        self.accesses: list[ScheduledAccess] = []

    def add(self, access: ScheduledAccess) -> None:
        self.accesses.append(access)

    def at_location_week(self, location_id: str, week: int) -> list[ScheduledAccess]:
        return [a for a in self.accesses if a.week == week and location_id in a.locations]

    def for_contract_week(self, contract_number: str, week: int) -> list[ScheduledAccess]:
        return [a for a in self.accesses if a.contract_number == contract_number and a.week == week]

    def for_activity(self, activity_id: str) -> list[ScheduledAccess]:
        return [a for a in self.accesses if a.activity_id == activity_id]

    def eclo_touching_line(self, line: str) -> list[ScheduledAccess]:
        return [a for a in self.accesses if a.eclo and line in _lines_affected(a, self.instance)]


# ----------------------------------------------------------------------
# Small lookups
# ----------------------------------------------------------------------

_INSTANCE_CACHE: dict[int, dict] = {}


def _cache_for(instance: Instance) -> dict:
    """
    One-time-per-instance precomputed lookups.

    Found and fixed 2026-09-19 after a user-reported performance
    problem: profiling a single greedy_scheduler run on Scenario B
    (cProfile, not a guess) showed 93 of its ~93s total went into
    check_all()/is_legal() — called 8,901 times — and 65s of THAT was
    inside _own_span_and_buffer/_path_for_seq_range alone, because every
    single call was rebuilding a pandas `set_index()` or boolean-mask
    filter from scratch (79,093 set_index() calls, 260,317 DataFrame
    `__getitem__` calls measured). Instance data never changes during a
    run, so none of that needs to be redone per call — it needs to be
    built ONCE. This cache is exactly that; every _small lookup_ function
    below reads from it instead of re-deriving from instance.*
    DataFrames each time.

    Keyed by id(instance) rather than an attribute on Instance itself,
    to keep this a constraints.py-internal concern rather than touching
    data_model.py's dataclass. Safe under this project's actual usage
    (one Instance loaded per process, kept alive for the whole run) —
    would need a real eviction/weakref strategy if that ever changes
    (e.g. a long-lived server juggling many different Instance objects
    whose ids could be recycled by the GC).
    """
    key = id(instance)
    cache = _INSTANCE_CACHE.get(key)
    if cache is None:
        cache = _build_cache(instance)
        _INSTANCE_CACHE[key] = cache
    return cache


def _build_cache(instance: Instance) -> dict:
    # Plain dicts, not pd.Series — see sectors_by_line's comment below for why.
    contracts_by_number = {row["contract_number"]: row.to_dict() for _, row in instance.contracts.iterrows()}
    activities_by_id = {row["activity_id"]: row.to_dict() for _, row in instance.activities.iterrows()}
    buffer_by_nature = {row["nature_of_works"]: row.to_dict() for _, row in instance.buffer_location.iterrows()}

    sectors_by_line: dict[str, list] = {}
    bare_seq_by_line: dict[str, dict[str, int]] = {}
    for line in instance.sectors["line_code"].unique():
        # Plain dicts, not pd.Series — Series.__getitem__ has real per-call
        # overhead (measured: 401k calls, 2.1s) even for a single-key lookup.
        line_sectors = [row.to_dict() for _, row in instance.sectors[instance.sectors["line_code"] == line].sort_values("seq").iterrows()]
        sectors_by_line[line] = line_sectors
        bare_seq_by_line[line] = {row["sector_id"]: int(row["seq"]) for row in line_sectors}

    supply_by_location = dict(zip(instance.location_supply["location_id"], instance.location_supply["supply_capacity"]))

    return {
        "contracts": contracts_by_number,
        "activities": activities_by_id,
        "buffer": buffer_by_nature,
        "sectors_by_line": sectors_by_line,  # each list already sorted by seq
        "bare_seq_by_line": bare_seq_by_line,
        "supply_by_location": supply_by_location,
    }


def _contract_row(contract_number: str, instance: Instance) -> dict:
    return _cache_for(instance)["contracts"][contract_number]


def _activity_row(activity_id: str, instance: Instance) -> dict:
    return _cache_for(instance)["activities"][activity_id]


def _activity_yield(activity_id: str, state: ScheduleState) -> float:
    """Total access-night yield scheduled so far for one activity (standard=1.0, ECLO=1.5, §2.4 rule 1)."""
    return sum(1.5 if a.eclo else 1.0 for a in state.for_activity(activity_id))


def _opposite_bound_required(nature_of_works: str, instance: Instance) -> bool:
    return bool(int(_cache_for(instance)["buffer"][nature_of_works]["opposite_bound_required"]))


def _line_of(location_id: str) -> str:
    return location_id.split(":")[1]


def _bound_of(location_id: str) -> str:
    return location_id.split(":")[-1]


def _other_bound(bound: str) -> str:
    return "WB" if bound == "EB" else "EB"


def _other_line(line: str) -> str:
    return "BET" if line == "ALP" else "ALP"


def week_of(iso_date: str, instance: Instance) -> int:
    """
    Calendar date -> week number, per docs/decisions.md's derived formula
    (see Review point 7 above — sanity-checked against the sample, not
    independently proven).
    """
    horizon_start = date.fromisoformat(instance.parameters["horizon_start"])
    d = date.fromisoformat(iso_date)
    return (d - horizon_start).days // 7 + 1


def week_end_date(week: int, instance: Instance) -> date:
    """
    Week number -> its last calendar day (Sunday). Exact inverse of
    week_of() by construction (`week_of(week_end_date(week, instance),
    instance) == week` for any week >= 1) — verified this session against
    3 real dates in data/sample_instance/03_submission_sample/RESULTS.csv
    (e.g. week 28 <-> 2027-07-18 for contract C006), not just derived.

    No bounds-check against horizon_weeks: overrun weeks legitimately run
    past the nominal horizon (Design philosophy #1 — the scheduler must
    keep going, never declare a case impossible), so this stays pure
    arithmetic rather than raising/clamping.
    """
    horizon_start = date.fromisoformat(instance.parameters["horizon_start"])
    return horizon_start + timedelta(days=7 * week - 1)


# ----------------------------------------------------------------------
# Zone computation (shared by buffer + mirroring checks)
# ----------------------------------------------------------------------


def _seq_range_of_path(locations: tuple[str, ...], instance: Instance) -> tuple[str, str, int, int]:
    """Given a resolved path (own line/bound), return (line, bound, low_seq, high_seq)."""
    sec_locs = [loc for loc in locations if loc.startswith("SEC:")]
    line = _line_of(sec_locs[0])
    bound = _bound_of(sec_locs[0])
    bare_seq = _cache_for(instance)["bare_seq_by_line"][line]
    seqs = []
    for loc in sec_locs:
        parts = parse_location_id(loc)
        bare = f"{parts['kind']}:{parts['line']}:{parts['from']}_{parts['to']}"
        seqs.append(bare_seq[bare])
    return line, bound, min(seqs), max(seqs)


def _path_for_seq_range(line: str, bound: str, seq_lo: int, seq_hi: int, instance: Instance) -> set[str]:
    """
    Same platform/tunnel interleaving as resolve_activity_path(), but
    driven directly by a (possibly buffer-extended) seq range rather than
    a start/end activity id pair. Clips to the line's actual seq range.

    Pure-Python list filtering over the cached, pre-sorted sector list —
    NOT pandas boolean-mask filtering — this function alone was 39s of a
    93s greedy run on Scenario B before the fix (see _cache_for's
    docstring); it's called once per buffer-carrying candidate check,
    thousands of times per scheduling run.
    """
    sectors_on_line = _cache_for(instance)["sectors_by_line"][line]  # already sorted by seq
    seq_lo = max(seq_lo, int(sectors_on_line[0]["seq"]))
    seq_hi = min(seq_hi, int(sectors_on_line[-1]["seq"]))
    if seq_lo > seq_hi:
        return set()
    path_sectors = [row for row in sectors_on_line if seq_lo <= row["seq"] <= seq_hi]

    locs: set[str] = {f"PLAT:{line}:{path_sectors[0]['from_station_id']}:{bound}"}
    for row in path_sectors:
        locs.add(f"{row['sector_id']}:{bound}")
        locs.add(f"PLAT:{line}:{row['to_station_id']}:{bound}")
    return locs


def _own_span(access: ScheduledAccess) -> set[str]:
    """
    Just the activity's own booked path — no buffer-sector extension.

    Renamed from `_own_span_and_buffer` (2026-09-19): the buffer-sector
    extension was REMOVED, not merely narrowed, after strong evidence it
    doesn't match what the reference validator actually enforces — see
    docs/decisions.md's "Buffer/exclusion-zone rule" entry for the full
    chain. Short version: dropping the extension entirely (a) took the
    sample-replay's "closure" discrepancy from 34 down to 0, and (b) made
    this project's own greedy scheduler's Scenario A score EXACTLY match
    the organizer's own reference score (32.2, not just "close") — strong
    enough to treat this as a confirmed modeling bug, not an unresolved
    ambiguity kept literal pending clarification.
    """
    return set(access.locations)


def _touches_interchange(zone: set[str]) -> Optional[str]:
    """If `zone` includes an H01_H02 tunnel/platform location, return its line; else None."""
    for loc in zone:
        if "H01_H02" in loc or ":H01:" in loc or ":H02:" in loc:
            return _line_of(loc)
    return None


def _closure_zone(access: ScheduledAccess, instance: Instance) -> set[str]:
    """
    Every location this access renders unavailable to other, non-co-
    sharing possessions this week: own span, plus — Live only — the
    opposite-bound mirror of that span, plus — Live only, interchange —
    the other line's H01_H02 tunnel + platforms (both bounds; see Review
    point 2). No buffer-sector extension — see `_own_span`'s docstring.
    """
    zone = _own_span(access)
    if access.nature_of_works != "Live" or not _opposite_bound_required(access.nature_of_works, instance):
        return zone

    line, bound, lo, hi = _seq_range_of_path(access.locations, instance)
    zone |= _path_for_seq_range(line, _other_bound(bound), lo, hi, instance)

    other_line = _touches_interchange(zone)
    if other_line is not None:
        other = _other_line(line)
        for b in ("EB", "WB"):
            zone.add(f"SEC:{other}:H01_H02:{b}")
            zone.add(f"PLAT:{other}:H01:{b}")
            zone.add(f"PLAT:{other}:H02:{b}")
    return zone


def _lines_affected(access: ScheduledAccess, instance: Instance) -> set[str]:
    """Lines whose ECLO window this access counts against: its own line, plus the other line for a `Live` interchange crossover."""
    return {_line_of(loc) for loc in _closure_zone(access, instance)}


def _is_co_share_exempt(a: ScheduledAccess, b: ScheduledAccess, loc: str) -> bool:
    """Same rule as check_legal_mix's grouping: same location + same co_share_group = one possession."""
    return loc in a.co_share_group and loc in b.co_share_group and a.co_share_group[loc] == b.co_share_group[loc]


def _zone_conflict(candidate: ScheduledAccess, other: ScheduledAccess, cand_zone: set[str], other_zone: set[str]) -> Optional[str]:
    """Return a colliding location if candidate and other's zones overlap outside a shared co-share slot, else None."""
    for loc in cand_zone & other_zone:
        if loc in candidate.locations and loc in other.locations and _is_co_share_exempt(candidate, other, loc):
            continue
        return loc
    return None


# ----------------------------------------------------------------------
# Rule 0 — Workload conservation (PS1_README §2.4 rule 1). Whole-schedule
# check, like check_predecessor_acyclic — a partially-built schedule is
# EXPECTED to be under-yield mid-construction, so this cannot be a
# per-candidate check_all() member. Call check_workload_conservation_all
# once, only after scheduling is believed complete (e.g. from self_check.py
# or at the end of a scheduler run) — this was a real gap found while
# designing self_check.py: no other function here checked "100% of
# activities scheduled, nightly yields >= total_accesses." Tag "workload"
# is invented — PS1_README names no tag for rule 1.
# ----------------------------------------------------------------------


def check_workload_conservation(activity_id: str, state: ScheduleState, instance: Instance) -> LegalityResult:
    activity = _activity_row(activity_id, instance)
    needed = float(activity["total_accesses"])
    done = _activity_yield(activity_id, state)
    if done < needed:
        return LegalityResult.fail("workload", f"{activity_id}: only {done}/{needed} access-night yield scheduled")
    return LegalityResult.ok("workload")


def check_workload_conservation_all(state: ScheduleState, instance: Instance) -> list[LegalityResult]:
    """Convenience: one result per activity in the instance, in activity_id order."""
    return [check_workload_conservation(aid, state, instance) for aid in instance.activities["activity_id"]]


# ----------------------------------------------------------------------
# Rule 1 — Planned start date (PS1_README §2.4 rule 2)
# ----------------------------------------------------------------------


def check_planned_start_date(candidate: ScheduledAccess, instance: Instance) -> LegalityResult:
    activity = _activity_row(candidate.activity_id, instance)
    earliest_week = week_of(activity["planned_start_date"], instance)
    if candidate.week < earliest_week:
        return LegalityResult.fail(
            "planned_start",
            f"{candidate.activity_id} week {candidate.week} is before its planned_start_date week {earliest_week}",
        )
    return LegalityResult.ok("planned_start")


# ----------------------------------------------------------------------
# Rule 2 — Legal mix pattern (PS1_README §2.4 rule 5; always hard — see
# Review point 6 for why it's split out from capacity)
# ----------------------------------------------------------------------


def check_legal_mix(candidate: ScheduledAccess, state: ScheduleState) -> LegalityResult:
    """
    Counts DISTINCT ACTIVITIES in a slot, not raw access-rows. Matters as
    of 2026-09-19: an activity can now legally take more than one
    access-night in the same week (see docs/decisions.md's "one access
    per activity per week" reversal) — its own earlier-this-week access
    would otherwise appear as a SECOND "member" of its own slot and get
    double-counted as if a second crew had joined, when it's really the
    same possession continuing. Deduplicating by activity_id makes a
    revisit a no-op for this count, exactly as it should be.
    """
    for loc in candidate.locations:
        group = candidate.co_share_group[loc]
        members = [a for a in state.at_location_week(loc, candidate.week) if a.co_share_group.get(loc) == group]
        type_by_activity = {m.activity_id: m.access_type for m in members}
        type_by_activity[candidate.activity_id] = candidate.access_type
        types = list(type_by_activity.values())
        pm, pc, c = types.count("PM"), types.count("PC"), types.count("C")
        others = tuple(sorted(a for a in type_by_activity if a != candidate.activity_id))
        if pm:
            if pm > 1 or pc or c:
                return LegalityResult.fail("legal_mix", f"{loc}/wk{candidate.week}/{group}: PM must be alone (found PM={pm} PC={pc} C={c})", location=loc, blockers=others)
        elif pc:
            if pc > 1 or c > 3:
                return LegalityResult.fail("legal_mix", f"{loc}/wk{candidate.week}/{group}: max 1 PC + 3 C (found PC={pc} C={c})", location=loc, blockers=others)
        else:
            if c > 4:
                return LegalityResult.fail("legal_mix", f"{loc}/wk{candidate.week}/{group}: max 4 C (found C={c})", location=loc, blockers=others)
    return LegalityResult.ok("legal_mix")


# ----------------------------------------------------------------------
# Rule 3 — Capacity (PS1_README §2.4 rule 5 + §2.5's scenario flex point;
# co-share-credit-aware per docs/decisions.md)
# ----------------------------------------------------------------------


def check_capacity(candidate: ScheduledAccess, state: ScheduleState, scenario: ScenarioConfig, instance: Instance) -> LegalityResult:
    supply = _cache_for(instance)["supply_by_location"]
    worst_excess = 0.0
    worst_loc = None
    for loc in candidate.locations:
        existing_groups = {a.co_share_group[loc] for a in state.at_location_week(loc, candidate.week) if loc in a.co_share_group}
        slots_used = len(existing_groups | {candidate.co_share_group[loc]})
        cap = int(supply[loc])
        excess = max(0, slots_used - cap)
        if excess > worst_excess:
            worst_excess = excess
            worst_loc = loc

    if worst_excess == 0:
        return LegalityResult.ok("capacity")

    if scenario.capacity_mode == "hard":
        legal = False
    elif scenario.capacity_mode == "soft_above_1":
        legal = worst_excess <= 1
    else:  # unlimited_soft
        legal = True

    occupants = tuple(sorted({a.activity_id for a in state.at_location_week(worst_loc, candidate.week) if a.activity_id != candidate.activity_id}))
    result = LegalityResult(legal, "capacity", f"{worst_loc}/wk{candidate.week}: {worst_excess} slot(s) over supply_capacity", excess=worst_excess, location=worst_loc, blockers=occupants)
    return result


# ----------------------------------------------------------------------
# Rule 4 — Buffer / exclusion zones (PS1_README §2.4 rule 4) — ALWAYS
# LEGAL, deliberately. See docs/decisions.md's "Buffer/exclusion-zone
# rule" entry.
# ----------------------------------------------------------------------


def check_buffer_exclusion(candidate: ScheduledAccess, state: ScheduleState, instance: Instance) -> LegalityResult:
    """
    The buffer-sector-extension reading of this rule was tried, measured
    against the sample submission, and found actively wrong — not just
    an unresolved granularity question. Two findings, both concrete:

    1. Replaying the sample through the literal spatial-extension check
       produced 34 false "closure" violations against a submission the
       organizer ships as feasible — including one pair (same contract,
       same access_night) that's provably simultaneous and STILL
       violated it, ruling out "maybe it's just a same-week-vs-same-
       night granularity artifact."
    2. Once the buffer-sector-extension is dropped entirely, own-span
       overlap between buffer-carrying activities is ALREADY governed
       correctly by check_capacity/check_legal_mix (which legitimately
       allow different co_share_groups to coexist at one location-week
       up to supply_capacity) — re-checking it here on top of that adds
       an extra "must share the same co_share_group" restriction that
       has no basis once there's no actual sector extension involved.
       With the extension gone, this function's own-span-vs-own-span
       check became pure redundant-and-wrong overlap with those two.

    Kept as an explicit function (not deleted, not removed from
    check_all()) so this rule stays individually named and traceable
    against PS1_README's rule list, in case the organizer's own
    clarification ever says otherwise — the fix, if so, is restoring the
    buffer-sector-extension body this function and `_own_span` used to
    have (see git history / earlier session's version, or
    docs/decisions.md).
    """
    return LegalityResult.ok("closure")


# ----------------------------------------------------------------------
# Rule 4, SOFT companion — buffer-proximity (2026-09-19). NEVER a hard
# check, NEVER part of check_all()/is_legal(): the sample submission
# neither proves nor refutes a week-level buffer rule (see docs/
# decisions.md's "EVIDENCE CORRECTION" note under the buffer entry), so
# the always-legal call above stands. This is the "conservative middle
# ground" agreed with the user instead: schedulers PREFER not to put two
# buffer-carrying activities with overlapping buffer footprints in the
# same week unless they are co-share-connected, but only when avoiding it
# is free (see greedy_scheduler's `buffer_patience` and the pick-best
# rule in schedule()). It's a hedge against a hidden validator that
# enforces week-level buffers, not a claim about what the rules are.
#
# JUDGMENT CALLS (all deliberately conservative — a false positive here
# costs at most a tie-break, a false negative leaves the hedge weaker):
#   - Week granularity, not night: cross-contract nights aren't
#     comparable in this schema (access_night is contract-local), so any
#     two accesses in the same week are treated as possibly same-night.
#   - Buffer-carrying = up_to_buffer_sectors > 0 (Live=2, Consist=1;
#     Others=0 never carries one). Footprint = own span extended by that
#     many sectors each side on the same line+bound.
#   - Co-share-connected = "combined possession" reading: connected through
#     ANY chain of shared (location, co_share_group) pairs among that
#     week's accesses (e.g. A001~A040~A007 counts as one possession), not
#     just a shared group at the exact overlapping location.
# ----------------------------------------------------------------------


def _buffer_footprint(access: ScheduledAccess, instance: Instance) -> frozenset[str]:
    """Own span extended by up_to_buffer_sectors each side; empty if the nature carries no buffer."""
    cache = _cache_for(instance)
    buffer_sectors = int(cache["buffer"][access.nature_of_works]["up_to_buffer_sectors"])
    if buffer_sectors == 0:
        return frozenset()
    memo = cache.setdefault("buffer_footprint", {})
    key = (access.locations, buffer_sectors)
    footprint = memo.get(key)
    if footprint is None:
        if any(loc.startswith("SEC:") for loc in access.locations):
            line, bound, lo, hi = _seq_range_of_path(access.locations, instance)
            footprint = frozenset(_path_for_seq_range(line, bound, lo - buffer_sectors, hi + buffer_sectors, instance))
        else:
            footprint = frozenset(access.locations)
        memo[key] = footprint
    return footprint


def buffer_proximity_conflicts(candidate: ScheduledAccess, state: ScheduleState, instance: Instance) -> list[ScheduledAccess]:
    """
    Other accesses in `candidate`'s week, from a DIFFERENT activity, whose
    buffer footprint overlaps `candidate`'s and that are not in the same
    combined co-share possession as it. Empty list = no proximity concern.
    """
    footprint = _buffer_footprint(candidate, instance)
    if not footprint:
        return []

    week_accesses = [a for a in state.accesses if a.week == candidate.week]

    possession = {(loc, grp) for loc, grp in candidate.co_share_group.items()}
    connected: set[int] = set()
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(week_accesses):
            if i in connected:
                continue
            pairs = set(a.co_share_group.items())
            if pairs & possession:
                connected.add(i)
                possession |= pairs
                changed = True

    conflicts = []
    for i, a in enumerate(week_accesses):
        if a.activity_id == candidate.activity_id or i in connected:
            continue
        if footprint & _buffer_footprint(a, instance):
            conflicts.append(a)
    return conflicts


def buffer_proximity_pairs(state: ScheduleState, instance: Instance) -> list[tuple[str, str, int]]:
    """
    Every distinct (activity_a, activity_b, week) — a < b — whose buffer
    footprints overlap without being co-share-connected, judged on the
    FINAL state (order-independent: a chain like A001~A040~A007 counts as
    one possession no matter which access was committed first, unlike the
    incremental `buffer_proximity_conflicts` a scheduler must use mid-
    construction). Used as a diagnostic/tie-break metric (self_check,
    tests, engine pick-best), never as a hard violation.
    """
    by_week: dict[int, list[ScheduledAccess]] = {}
    for a in state.accesses:
        by_week.setdefault(a.week, []).append(a)

    pairs: set[tuple[str, str, int]] = set()
    for week, accesses in by_week.items():
        parent = list(range(len(accesses)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        owner_of_slot: dict[tuple[str, str], int] = {}
        for i, a in enumerate(accesses):
            for slot in a.co_share_group.items():
                if slot in owner_of_slot:
                    parent[find(i)] = find(owner_of_slot[slot])
                else:
                    owner_of_slot[slot] = i

        footprints = [_buffer_footprint(a, instance) for a in accesses]
        for i in range(len(accesses)):
            if not footprints[i]:
                continue
            for j in range(i + 1, len(accesses)):
                if accesses[i].activity_id == accesses[j].activity_id or find(i) == find(j):
                    continue
                if footprints[i] & footprints[j]:
                    a, b = sorted((accesses[i].activity_id, accesses[j].activity_id))
                    pairs.add((a, b, week))
    return sorted(pairs)


# ----------------------------------------------------------------------
# Rule 5 — Live-rail opposite-bound mirroring + interchange crossover
# (PS1_README §2.2 + §2.4 rule 4) — unaffected by the buffer-extension
# removal above; this is a separate, independently-evidenced rule (the
# two real Live activities in the sample, A074/A075, confirmed this
# session too — see docs/decisions.md).
# ----------------------------------------------------------------------


def check_live_mirroring(candidate: ScheduledAccess, state: ScheduleState, instance: Instance) -> LegalityResult:
    """
    Checked symmetrically: this fires whether `candidate` is the Live
    activity extending into someone else's slot, or `candidate` is an
    ordinary activity that would collide with an EXISTING Live activity's
    mirror/crossover zone (see docs comment on _closure_zone).
    """
    cand_zone = _closure_zone(candidate, instance)

    for other in state.accesses:
        if other.week != candidate.week or other.activity_id == candidate.activity_id:
            continue
        if other.nature_of_works != "Live":
            continue
        other_zone = _closure_zone(other, instance)
        conflict = _zone_conflict(candidate, other, cand_zone, other_zone)
        if conflict is not None:
            return LegalityResult.fail("closure", f"{candidate.activity_id} collides with Live activity {other.activity_id}'s mirror/crossover at {conflict}/wk{candidate.week}", location=conflict, blockers=(other.activity_id,))
    return LegalityResult.ok("closure")


# ----------------------------------------------------------------------
# Rule 6/7 — Weekly allocation cap + workfronts (PS1_README §2.4 rules 7, 8)
# ----------------------------------------------------------------------


def check_one_access_per_activity_week(candidate: ScheduledAccess, state: ScheduleState, scenario: ScenarioConfig) -> LegalityResult:
    """
    Optional STRICT reading of PS1_README §2.4 rule 10's "an activity gets
    at most one access-night per week" — off by default; see
    `ScenarioConfig.one_access_per_activity_week`'s docstring for why (the
    phrase reads as scoped to rule 10's own ECLO-continuity derivation, not
    a universal cap — rules 6/7/8 govern weekly access-night usage at the
    contract+type grain instead). Toggle:
    `scenario.one_access_per_activity_week` (default False). Tag
    "one_per_week" is invented — the README names no tag for this.
    """
    if not scenario.one_access_per_activity_week:
        return LegalityResult.ok("one_per_week")
    if any(a.activity_id == candidate.activity_id and a.week == candidate.week for a in state.accesses):
        return LegalityResult.fail("one_per_week", f"{candidate.activity_id} wk{candidate.week}: already has an access this week (max one per activity per week)")
    return LegalityResult.ok("one_per_week")


def check_weekly_allocation_and_workfronts(candidate: ScheduledAccess, state: ScheduleState, instance: Instance) -> LegalityResult:
    """
    Only relevant beyond one-per-week when `one_access_per_activity_week`
    is switched OFF: an activity may then take more than one access-night in
    the same week (see docs/decisions.md) — but each of ITS OWN accesses
    that week must still land on a DIFFERENT access_night. `access_night`
    represents one specific calendar night out of the contract's granted
    set; the same activity claiming the same access_night twice would
    mean "working the same specific night twice," which doesn't mean
    anything. Nothing checked this before this session because no
    activity could previously have more than one access in a week at
    all — now that one can, this needed adding.
    """
    contract = _contract_row(candidate.contract_number, instance)
    existing = state.for_contract_week(candidate.contract_number, candidate.week)

    own_repeat = [a for a in existing if a.activity_id == candidate.activity_id and a.access_night == candidate.access_night]
    if own_repeat:
        return LegalityResult.fail("weekly_allocation", f"{candidate.activity_id} wk{candidate.week}: already has an access on access_night {candidate.access_night} this week")

    distinct_nights = {a.access_night for a in existing} | {candidate.access_night}
    cap = int(contract["number_of_maximum_access_per_week"])
    if len(distinct_nights) > cap:
        return LegalityResult.fail(
            "weekly_allocation",
            f"{candidate.contract_number} wk{candidate.week}: {len(distinct_nights)} distinct access_nights > cap {cap}",
            blockers=tuple(sorted({a.activity_id for a in existing if a.activity_id != candidate.activity_id})),
        )

    same_night = [a for a in existing if a.access_night == candidate.access_night]
    distinct_activities = {a.activity_id for a in same_night} | {candidate.activity_id}
    workfronts = int(contract["number_of_workfronts"])
    if len(distinct_activities) > workfronts:
        return LegalityResult.fail(
            "workfront",
            f"{candidate.contract_number} wk{candidate.week} night{candidate.access_night}: "
            f"{len(distinct_activities)} concurrent activities > {workfronts} workfronts",
            blockers=tuple(sorted(distinct_activities - {candidate.activity_id})),
        )
    return LegalityResult.ok("weekly_allocation")


# ----------------------------------------------------------------------
# Rule 8 — Predecessor precedence (PS1_README §2.4 rule 3, confirmed
# 2026-09-18 — previously our own provisional assumption, now official
# text: "finish-to-start, zero lag (FS+0). 'Finished' = the week of the
# predecessor's last scheduled access night; the successor's first
# scheduled access night must fall in a strictly later week.")
# ----------------------------------------------------------------------


def check_predecessor_completion(candidate: ScheduledAccess, state: ScheduleState, instance: Instance) -> LegalityResult:
    """
    The `yield_done < pred_total_needed` gate below is an implementation
    necessity, not an extra rule: since we check this incrementally as the
    schedule is BUILT (not just once at the end against a finished
    schedule), the predecessor might currently show a "last scheduled
    week" from only a partial subset of its accesses, with more of its
    own accesses still to be placed — possibly landing after that partial
    max. Without this gate, a not-yet-fully-scheduled predecessor could
    look "finished" prematurely. Cross-contract predecessor links are
    explicitly allowed by the brief and require no special-casing here —
    this function never inspects contract_number.
    """
    activity = _activity_row(candidate.activity_id, instance)
    pred_id = activity.get("predecessor_activity_id")
    if pd.isna(pred_id):
        return LegalityResult.ok("predecessor")

    pred_accesses = state.for_activity(pred_id)
    pred_total_needed = float(_activity_row(pred_id, instance)["total_accesses"])
    yield_done = _activity_yield(pred_id, state)

    if yield_done < pred_total_needed:
        return LegalityResult.fail("predecessor", f"{candidate.activity_id}'s predecessor {pred_id} not yet fully complete ({yield_done}/{pred_total_needed})", blockers=(pred_id,))

    pred_last_week = max(a.week for a in pred_accesses)
    if candidate.week <= pred_last_week:
        return LegalityResult.fail("predecessor", f"{candidate.activity_id} wk{candidate.week} not after predecessor {pred_id}'s last week {pred_last_week}", blockers=(pred_id,))
    return LegalityResult.ok("predecessor")


def check_predecessor_acyclic(instance: Instance) -> LegalityResult:
    """
    PS1_README §2.4 rule 3's closing clause: "predecessor cycles are not
    [allowed]." This is a property of the INSTANCE data (the
    predecessor_activity_id column), not of any candidate placement — call
    this ONCE, e.g. at scheduler/self-check startup, never per-candidate.

    Each activity has at most one predecessor, so the "graph" is a simple
    functional graph (out-degree <= 1): walking the chain from any start
    node and re-visiting a node proves a cycle. O(n^2) worst case, trivial
    at this instance's scale (54 activities).
    """
    preds = instance.activities.set_index("activity_id")["predecessor_activity_id"]
    for start in preds.index:
        seen = [start]
        current = preds.get(start)
        while pd.notna(current):
            if current in seen:
                return LegalityResult.fail("predecessor_cycle", f"predecessor cycle: {' -> '.join(seen + [current])}")
            seen.append(current)
            current = preds.get(current)
    return LegalityResult.ok("predecessor_cycle")


# ----------------------------------------------------------------------
# Rule 9 — ECLO hard-forbidden in A (PS1_README §2.5)
# ----------------------------------------------------------------------


def check_eclo_allowed(candidate: ScheduledAccess, scenario: ScenarioConfig) -> LegalityResult:
    if candidate.eclo and not scenario.eclo_allowed:
        return LegalityResult.fail("eclo", f"{candidate.activity_id} wk{candidate.week}: ECLO not allowed in Scenario {scenario.name}")
    return LegalityResult.ok("eclo")


# ----------------------------------------------------------------------
# Rule 10 — ECLO continuity window, Scenario C only (PS1_README §2.4 rule 10)
# ----------------------------------------------------------------------


def check_eclo_continuity_window(candidate: ScheduledAccess, state: ScheduleState, scenario: ScenarioConfig) -> LegalityResult:
    if not scenario.eclo_continuity_required or not candidate.eclo:
        return LegalityResult.ok("eclo_continuity")

    # §2.4 rule 10: "A cross-line Live activity's ECLO nights must fit both lines' windows at once" —
    # so use the closure zone (which includes the interchange crossover), not just the activity's own path.
    lines_touched = _lines_affected(candidate, state.instance)
    for line in lines_touched:
        weeks = {a.week for a in state.eclo_touching_line(line)} | {candidate.week}
        span = max(weeks) - min(weeks) + 1
        if span > 2:
            return LegalityResult.fail(
                "eclo_continuity",
                f"{candidate.activity_id} wk{candidate.week}: ECLO on line {line} would span {span} weeks (max 2)",
            )
    return LegalityResult.ok("eclo_continuity")


# ----------------------------------------------------------------------
# Rule 11 — Completion deadline, Scenario B only (PS1_README §2.5; see
# Review point 4 — necessary but not sufficient)
# ----------------------------------------------------------------------


def check_completion_deadline(candidate: ScheduledAccess, scenario: ScenarioConfig, instance: Instance) -> LegalityResult:
    if not scenario.date_hard:
        return LegalityResult.ok("planned_date")

    contract = _contract_row(candidate.contract_number, instance)
    deadline_week = week_of(contract["planned_completion_date"], instance)
    if candidate.week > deadline_week:
        return LegalityResult.fail(
            "planned_date",
            f"{candidate.activity_id} wk{candidate.week} is past contract {candidate.contract_number}'s planned_completion_date week {deadline_week}",
        )
    return LegalityResult.ok("planned_date")


# ----------------------------------------------------------------------
# Aggregate entry point
# ----------------------------------------------------------------------


def check_all(candidate: ScheduledAccess, state: ScheduleState, instance: Instance, scenario: ScenarioConfig) -> list[LegalityResult]:
    """
    Run every PER-CANDIDATE rule and return all results (not short-
    circuited) — useful for explainability logging.

    Does NOT include `check_predecessor_acyclic` or
    `check_workload_conservation_all` — both are one-time, whole-schedule/
    whole-instance checks (no single candidate/week involved), not
    per-placement ones. Callers MUST call `check_predecessor_acyclic(
    instance)` once before scheduling anything, and
    `check_workload_conservation_all(state, instance)` once after
    scheduling is believed complete.
    """
    return [
        check_planned_start_date(candidate, instance),
        check_one_access_per_activity_week(candidate, state, scenario),
        check_legal_mix(candidate, state),
        check_capacity(candidate, state, scenario, instance),
        check_buffer_exclusion(candidate, state, instance),
        check_live_mirroring(candidate, state, instance),
        check_weekly_allocation_and_workfronts(candidate, state, instance),
        check_predecessor_completion(candidate, state, instance),
        check_eclo_allowed(candidate, scenario),
        check_eclo_continuity_window(candidate, state, scenario),
        check_completion_deadline(candidate, scenario, instance),
    ]


def is_legal(candidate: ScheduledAccess, state: ScheduleState, instance: Instance, scenario: ScenarioConfig) -> bool:
    """Short-circuiting convenience for the scheduler's hot loop."""
    return all(r.legal for r in check_all(candidate, state, instance, scenario))
