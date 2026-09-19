# Railway Track Access Optimisation — hackathon (PS1)

Building a scheduler for a hackathon problem statement (dual-line rail
network, contested nightly track access). Full spec: `PS1_README.md`.
Team plan: `Project_Framework.md`. The user owns **Role A (Solver Lead)**
and **Role B (Data & Validation Lead)** solo; teammates own Role C
(Streamlit app) and Role D (docs/video) separately.

**Read `docs/decisions.md` first, in full.** It has open assumptions and
corrected design decisions that are NOT yet reflected in
`Project_Framework.md` (the user is updating that file by hand). Don't
re-derive any of this from scratch — it was already decided and logged,
sometimes after correcting an earlier mistake. In particular: the
framework's Phase 2 (§8.3) description of the greedy scheduler is
OUTDATED — see "Design philosophy" below for the corrected version.

## Environment

- Python 3.13.6 locally (Windows, VS Code). `pip install -r requirements.txt`
  — pandas, ortools (CP-SAT), numpy, protobuf. Versions were frozen from a
  Python 3.11 prototyping session; pip will resolve its own compatible
  wheels on 3.13.
- Use the project venv's interpreter (`.venv\Scripts\python.exe`) for
  everything below — the bare `python` on this machine's PATH has no pandas.
- Run tests from repo root: `python tests/smoke_test.py`,
  `python tests/test_path_resolver.py`, `python tests/test_constraints.py`,
  `python tests/test_output_writer.py`, `python tests/test_self_check.py`,
  `python tests/test_greedy_scheduler.py`, `python tests/test_cpsat_engine.py`,
  `python tests/test_explain.py`, `python tests/test_api.py` (all insert repo
  root onto `sys.path` themselves). `python tests/test_benchmark.py` is a
  separate, slower evaluation script (penalty scores vs. the organizer's
  sample). The 9 fast files together run in ~35-40s (measured 2026-09-20;
  `test_cpsat_engine.py`/`test_api.py` dominate — each schedules all 3
  scenarios, and the CP-SAT engine also runs greedy internally for its
  hint/fallback/pick-best) — much better than pre-caching-fix territory,
  but no longer the ~12s this section used to claim; re-measure if it
  matters rather than trusting either number blindly.
- Explain a submission in plain English: `python -m src.explain <submission_dir>`.
- Run either scheduler end-to-end (both default to Scenario A): `python -m
  src.cpsat_engine [A|B|C]` — the PRIMARY engine (picks whichever of
  {cpsat, greedy} scores better internally) — writes the committed
  deliverable to `outputs/scenario_<X>/` (3 CSVs + `report.json`, no
  suffix — matches PS1_README §2.6/§2.7 and Project_Framework's Appendix A
  exactly). `python -m src.greedy_scheduler [A|B|C]` writes plain greedy's
  OWN result in isolation to `outputs/scenario_<X>_greedy_only/`
  (gitignored, dev-time comparison only — see `tests/test_benchmark.py`
  for the number that actually matters, "cpsat never worse than greedy").
  Both print a self-check summary.
- **Role C (the web app) should integrate via `src/api.py`**, not the
  individual engine/writer/checker modules directly — see that module's
  own docstring and `docs/decisions.md`'s "`src/api.py` integration
  facade" entry. `python -m src.api sample A` is a CLI sanity-check with
  the same shape.

## Repo layout (per Project_Framework.md's Appendix A)
src/data_model.py # Instance dataclass, load_instance(), resolve_activity_path()
# (written + validated: 54/54 activities match sample ground truth)
src/constraints.py # shared hard-rule legality library — see "Current status"
src/greedy_scheduler.py # Phase 2 safety-net engine — schedule(instance, scenario) -> ScheduleState
src/cpsat_engine.py # Phase 3 engine — same signature; hybrid design, see its own docstring
src/output_writer.py # ScheduleState -> the 3 submission CSVs (§2.6 schemas)
src/self_check.py # submission CSVs -> constraints.py replay -> §2.7 report shape;
# also owns load_submission_csvs()/build_scheduled_accesses(), reused by tests
src/explain.py # Phase 6 — plain-English capacity-pressure/trade-off/placement narration
src/api.py # integration facade for Role C's web app — see its own docstring
tests/smoke_test.py # referential-integrity checks on the raw 8 CSVs
tests/test_path_resolver.py # validates resolve_activity_path() against
# 03_submission_sample's ground truth (54/54 activities match)
tests/test_constraints.py # replays the sample submission through every check_*
tests/test_output_writer.py # round-trips the sample submission through write_submission()
tests/test_self_check.py # checks build_report() against hand-verified sample values
tests/test_greedy_scheduler.py # runs schedule() for A/B/C, checks the safety-net contract holds
tests/test_cpsat_engine.py # same safety-net contract, for cpsat_engine.py
tests/test_explain.py # explain.py's numbers must agree with self_check.py's, for the sample + greedy A/B/C
tests/test_api.py # src/api.py: upload round-trip, validation errors, solve() output shape
tests/test_benchmark.py # evaluation script — penalty-score comparison across
# the organizer's sample submission, greedy, and cpsat for all 3 scenarios
data/sample_instance/01_data/ # the 8 instance CSVs
data/sample_instance/02_references/ # network_diagram.svg, PS1.drawio
data/sample_instance/03_submission_sample/ # organizer's own feasible Scenario-A example
docs/decisions.md # assumptions/decisions log — READ THIS FIRST, IN FULL
outputs/scenario_{A,B,C}/ # cpsat_engine's submissions — 3 CSVs + report.json — COMMITTED, this is Deliverable #1
outputs/scenario_{A,B,C}_greedy_only/ # plain greedy's own result, dev-time comparison only (gitignored)


Not yet created: `app/streamlit_app.py` (Role C's, not ours — `src/api.py`
is what it should import). `src/explain.py` exists (Phase 6).

## What's actually in the data (confirmed empirically, not just from the README)

- `start_location_id`/`end_location_id` in `08_ACTIVITY_DETAILS.csv` are
  already full tunnel-sector location_ids (e.g. `SEC:BET:S15_S16:EB`), not
  bare station ids. See `resolve_activity_path()` in `src/data_model.py`.
- `predecessor_activity_id` exists in the activities file (6/54 rows).
  **Now officially documented** as §2.4 rule 3 (PS1_README.md updated
  2026-09-18, confirming our provisional assumption exactly): finish-to-
  start, zero lag — predecessor must fully complete (all `total_accesses`
  nights) before the successor's first scheduled access night, which must
  fall in a strictly later week. Predecessor cycles are explicitly
  disallowed (new — see `check_predecessor_acyclic` in
  `src/constraints.py`). This rule insertion also shifted §2.4's other
  rule numbers down by one (old rule 3 = buffers is now rule 4, etc.) —
  see `docs/decisions.md`'s "Settled" section.
- Real `LOCATION_SUPPLY` capacities are much tighter at the interchange
  than the brief's illustrative example suggests: `H01`/`H02` platforms and
  the `H01_H02` tunnel sector cap at capacity 1 on each line — the real
  bottleneck in this instance, and a good adversarial case to test
  `constraints.py` against.
- Only 2 activities are `Live` (`A074`, `A075`), both at the interchange —
  small clean test case for the opposite-bound/cross-line mirroring rule.
- Capacity is enforced per `co_share_group` **slot**, not raw activity
  count — confirmed by checking the sample submission (grouping by
  co_share_group resolved 69 apparent capacity "violations" to zero).
- No `trackaccess` CLI tool (the brief mentions `python3 -m trackaccess
  expand` for generating `co_share_group`) was provided — we're writing
  that grouping logic ourselves, as a real scheduling decision inside the
  scheduler(s), not a cosmetic post-processing step.

## Design philosophy (CORRECTED 2026-09-18 — supersedes Project_Framework.md §8.3)

1. **The greedy scheduler must never breach a hard physical constraint,
   even as an MVP.** `PS1_README.md` §1 says hard safety rules "must never
   be breached," and Scenario A hard-fails the whole submission on any
   capacity excess — an "MVP" that ships infeasible output isn't a rough
   draft, it's failing the actual grading criterion. "Greedy" means simple/
   fast/no-backtracking, NOT "ignores the rules." When no legal slot exists
   in the current week, search later weeks (or, under Scenario B, use
   extra access-nights/ECLO) — never force an illegal placement. The
   Oudshoorn paper's own greedy algorithm (§4.2) works this same way: try
   candidates, keep the best legal one, only accept a violation as an
   absolute last resort with nothing else in the whole plan window.
2. **Rules that are hard in EVERY scenario, no exceptions:** Live-rail
   opposite-bound + interchange-crossover mirroring (buffer/exclusion
   zones are NOT in this list anymore: the hard check is always-legal and
   only a soft buffer-proximity hedge remains — see Current status),
   weekly allocation caps (contract+type grain — up to
   `number_of_maximum_access_per_week` distinct nights, up to
   `number_of_workfronts` concurrent activities per night; NOT a per-
   activity one-access-per-week cap — see Current status for the 2026-
   09-19/20/20 back-and-forth on that specific question, settled
   permissive for now), workfronts, the legal-mix packing pattern, 100%
   workload placement, and predecessor precedence (§2.4 rule 3 —
   officially documented as of the 2026-09-18 PS1_README.md update, was
   previously our own provisional addition).
3. **Scenario-dependent flex points (each scenario's one release valve):**
   completion date is hard ONLY in Scenario B; location capacity is hard
   ONLY in Scenario A (soft/unlimited in B, soft-above-1-per-location-week
   in C); ECLO is hard-forbidden ONLY in Scenario A. See `docs/decisions.md`
   for the §1-vs-§2.5 inconsistency this resolves and why.
4. **Build `src/constraints.py` as a shared library FIRST**, before the
   greedy scheduler: pure functions to check buffer/exclusion legality,
   capacity legality (co-share-credit-aware, scenario-parameterized),
   weekly-allocation + workfront legality, Live-mirroring + interchange
   propagation, and predecessor completion. The greedy scheduler, the
   self-check validator, and (informing) the CP-SAT model all reuse this
   ONE module — never reimplement a rule twice.
5. One CP-SAT engine with a scenario config toggle for A/B/C (extending the
   same config object the greedy already needs for its flex points), not
   three separate solvers.
6. CP-SAT with a hard timeout, greedy result as automatic fallback if it
   doesn't converge in time.

## Current status (update this section as phases complete)

- [x] Phase 0 — shared understanding of the problem
- [x] Phase 1 (partial) — CSV loader + path resolver written and validated
      (54/54 activities match sample ground truth)
- [x] `src/constraints.py` — shared hard-rule legality checks (10 per-
      candidate check_* functions + a standalone check_predecessor_acyclic
      + ScheduleState/ScenarioConfig). Validated via
      `tests/test_constraints.py` by replaying the sample submission:
      **zero hard violations on every rule, no exceptions** — the buffer/
      exclusion ("closure") discrepancy mentioned in earlier versions of
      this file is RESOLVED, not just tolerated (see "buffer-sector-
      extension reversal" below and docs/decisions.md's "Settled"
      section). Predecessor precedence logic confirmed correct against
      the 2026-09-18 PS1_README.md update (now official §2.4 rule 3);
      added the newly-required predecessor-cycle check, confirmed zero
      cycles in the real instance.
- [x] **Buffer-sector-extension rule reversed (2026-09-19)** — the
      earlier "keep literal pending organizer clarification" call was
      wrong, not just unresolved, and was overturned once its real cost
      was measured: it was making every scheduler in this project run
      ~26x worse than achievable (835.8 vs. the organizer's own sample's
      32.2 on Scenario A). Root-caused by instrumenting `check_all()`
      during a live greedy run — "closure" rejections outnumbered every
      other rule's rejections COMBINED, 456 vs 164. Fix: the buffer-
      sector-extension (and the now-redundant own-span-vs-own-span check
      it left behind, which duplicated what `check_capacity`/
      `check_legal_mix` already correctly governed) is removed from
      `check_buffer_exclusion` — kept as an explicit always-legal
      function, not deleted, so PS1_README §2.4 rule 4 stays traceable.
      Live-mirroring/interchange-crossover (§2.2) is unaffected — separate,
      independently-evidenced rule. Result: zero violations replaying the
      sample (down from 34), AND greedy's Scenario A score now matches
      the organizer's own reference EXACTLY (32.2) before this scheduler's
      own optimization even kicks in. See docs/decisions.md for the full
      evidence chain (three tested versions, not one guess).
- [x] **"One access-night per activity per week": reversed 2026-09-19,
      reinstated 2026-09-20, RECONSIDERED back to permissive same day** —
      three swings on one question; permissive is what's in the code now,
      and is expected to stay unless the organizer's own answer says
      otherwise. History: reversed 2026-09-19 after tracing Scenario A/C's
      25.2 score to 2 contracts each blocked by one long activity (real
      diagnosis). Reinstated 2026-09-20 during a compliance review that
      read §2.4 rule 10's "an activity gets at most one access-night per
      week" literally — but that review stopped at rule 10 in isolation.
      The user challenged it directly, asking to reconsider given rules
      6/7/8/§2.1/§2.6 all phrase weekly access-night limits at the
      CONTRACT level, never per-activity, and rule 10's premise sits
      entirely inside a rule titled/scoped to Scenario C's ECLO
      continuity — re-reading the full rule set together changed the
      conclusion back to permissive. `ScenarioConfig.
      one_access_per_activity_week` default is now `False` again; `True`
      stays as an explicit opt-in for the strict reading, via
      `check_one_access_per_activity_week` in `constraints.py` (see its
      docstring for the full argument). **Result: all 3 scenarios, both
      engines, fully feasible (0 hard violations), back to the
      fully-optimal 0.0 / 14 / 7.0 (A/B/C)** — beats the organizer's own
      sample (32.2) by the full margin again. The Scenario B fixes found
      while reinstating (`greedy_scheduler._eclo_forced_by_deadline`/
      `_reset_for_relaxed_retry`) are real bugs that are still fixed —
      they just fire less often at the permissive default. See
      docs/decisions.md's "Settled" section (both the superseded
      reinstatement entry and the reconsideration entry) for the full
      argument and evidence.
- [x] **Soft buffer-proximity hedge (2026-09-19, re-measured 2026-09-20
      after the reconsideration above)** — after the user's row-by-row
      review showed the sample neither proves nor refutes a buffer rule
      (docs/decisions.md "EVIDENCE CORRECTION"), the hard buffer check
      stays always-legal but both engines now PREFER not to place two
      buffer-carrying activities with overlapping buffer footprints in
      the same week unless co-share-connected. `greedy_scheduler.
      _sweep(buffer_patience=N)` + a patience ladder, best picked by
      `self_check.score_state` (hard violations, objective, proximity
      pairs — lexicographic) so the hedge never costs objective. At the
      current (permissive) default, it's fully free: measured 0/0/0
      proximity pairs (A/B/C), at zero added objective cost — down from
      the organizer's own sample's 14. `constraints.buffer_proximity_
      pairs` is the metric (reproduces the user's 14 "unknown night" rows
      on the sample exactly); reported in `detail.buffer_proximity_
      pairs`. Never a hard violation.
- [x] Phase 1 remainder — `src/output_writer.py` (ScheduleState -> the 3
      submission CSVs) + `src/self_check.py` (submission CSVs -> replay
      through `constraints.py` -> §2.7 report shape). Round-trips the
      sample submission's own `RESULTS.csv`/`SCHEDULE_ACCESS.csv`/
      `SCHEDULE_OCCUPANCY.csv` exactly (`tests/test_output_writer.py`);
      `self_check.build_report` matches hand-verified values on the real
      sample (`tests/test_self_check.py`; the 34 "closure" discrepancies
      this bullet originally mentioned were resolved by the buffer
      reversal above — zero now). Also closed
      a real gap found along the way: Rule 1 (100% workload conservation)
      had no check anywhere — added `check_workload_conservation`/
      `check_workload_conservation_all` to `constraints.py`. Three scoring/
      output judgment calls made with explicit user sign-off this session
      — see `docs/decisions.md`'s "Settled" section for what and why.
- [x] Phase 2 — `src/greedy_scheduler.py`. One entry point,
      `schedule(instance, scenario) -> ScheduleState`, matching the
      signature `src/cpsat_engine.py` will share (swap engines by
      changing the import, nothing else). Global week-by-week sweep
      (not one-activity-at-a-time — see docs/decisions.md for why that
      first version was a real bug, not just a simplification),
      Earliest-Deadline-First ordering when `scenario.date_hard`, and a
      last-resort second pass that relaxes ONLY the scenario's own hard
      deadline (never buffer/capacity/workfront) if the strict pass
      can't finish everyone — never crashes, per §1 non-negotiable #3.
      Validated via `tests/test_greedy_scheduler.py` on the real
      instance: all three scenarios now come back fully feasible (0 hard
      violations, 192/192 access-nights placed) — Scenario B used to carry
      residual `planned_date` violations before the two 2026-09-19
      reversals above. Also fixed:
      `self_check.build_scheduled_accesses` now asserts (activity_id,
      week) is unique before doing anything else — found and fixed a
      real bug in an early version of this scheduler that violated it
      silently. See docs/decisions.md's "Settled" section for the full
      writeup of all three bugs found and fixed this session.
- [x] Phase 3 — `src/cpsat_engine.py`. Same `schedule(instance,
      scenario) -> ScheduleState` signature as greedy (swap by import),
      plus a hard `time_limit_seconds` on the solver itself (falls back
      to greedy's own result if the solver doesn't return OPTIMAL/
      FEASIBLE in time, per Design philosophy #6). Hybrid design, arrived
      at only after three measured failures of more literal approaches
      (see docs/decisions.md's "Settled" section for the full evidence):
      CP-SAT decides the temporal SHAPE of the schedule (per-activity
      finish week for ordering, ECLO usage pattern) respecting
      predecessor + weekly-cap*workfront exactly and minimizing the real
      §2.5 objective; the actual placement (location, group,
      access_night) is done by `greedy_scheduler._sweep`, UNCHANGED,
      just given a CP-SAT-informed order (tier-priority preserved, only
      tie-broken by CP-SAT's finish_week) and ECLO preference. Buffer/
      legal-mix/capacity are never modeled natively in CP-SAT — reused
      via the same `constraints.is_legal()` oracle everything else uses
      (Design philosophy #4). Because the ordering heuristic itself is a
      mixed bag (helps Scenario B, mildly hurts C), `schedule()` computes
      BOTH engines' results and keeps whichever `self_check.py` actually
      scores better — making "CP-SAT never worse than the safety net"
      true by construction. **Numbers in this paragraph are historical**
      (from before the buffer-rule fix and BOTH sides of the one-access-
      per-week back-and-forth) — kept for the design-journey record only;
      current numbers live in `tests/test_benchmark.py`'s own output and
      the bullets above. At the time this paragraph was originally
      written: Scenario A matched greedy exactly; Scenario B improved
      (12406.8 vs greedy's 14007.0 priority_weighted_score, AND fewer
      hard violations: 19 vs 23); Scenario C tied with greedy (the
      safeguard correctly rejected the hybrid's own slightly-worse
      attempt there). Validated via
      `tests/test_cpsat_engine.py` (safety-net contract) and
      `tests/test_benchmark.py` (score comparison vs. greedy and the
      organizer's sample, asserts no regression). Known limitation,
      documented not hidden: `schedule()`'s total runtime is NOT bounded
      by `time_limit_seconds` alone, since it also runs
      `greedy_scheduler.schedule()` (needed for the hint/fallback/score
      comparison) — Scenario B originally measured ~85s end to end even
      though the CP-SAT solve itself finished in 0.07s. **Root-caused
      and fixed same session, not left as a known issue**: a real
      `constraints.py` performance bug (`is_legal()` rebuilding pandas
      `set_index()`/boolean-mask filters from scratch on every single
      call — 79,093 `set_index()` calls measured for one greedy run —
      instead of caching lookups once per instance). Fixed via
      `constraints._cache_for()`. Post-fix: same Scenario B call runs in
      ~1.5s (a ~55x improvement on that call), and all 3 scenarios ×
      both engines run in under 3 seconds combined. See
      `docs/decisions.md`'s "Settled" section for the full profiling
      writeup (measured with `cProfile`, not guessed) and
      `tests/test_constraints.py` for confirmation the fix changed
      nothing about correctness (same 34 known "closure" discrepancies,
      zero others, before and after).
- [ ] Phase 4 (optional) — local-search polish
- [x] Phase 6 — `src/explain.py` (`python -m src.explain <submission_dir>
      [--all]`). **Rebuilt 2026-09-20 to explain CAUSES, generally** (the
      first version only said what happened, never why — a gap vs.
      Project_Framework §8.7): reconstructs why an activity could not work
      in a week by re-asking `constraints.check_all` (the single rule
      oracle; `LegalityResult` now carries `location`/`blockers`), and
      explains late starts (rule + location + blocking activities), contract
      overruns (structural / contention / pace, with priority-weighted cost),
      ECLO and excess-capacity use, and hard violations of infeasible
      schedules. Output: `events`, `by_activity`, `by_contract`,
      `contract_outcomes`, bounded `summary`; `api.solve()` puts each
      activity's text in `activity_timeline["explanation"]`. See
      docs/decisions.md for the method, limits and findings. Original
      version (below) narrated capacity pressure (binding vs. exactly-full
      location-weeks), scenario soft-cost trade-offs, and per-activity
      placement vs. earliest legal week, reusing `self_check.py`'s data.
      Validated by `tests/test_explain.py`. **Building it exposed and
      fixed a real bug**: `excess_access_nights_total` was overcounted
      (per-candidate replay sum instead of end-state per-location-week
      sum) — corrected scores are B = 14 (was 49), C = 7 (was 14), A
      unchanged at 0; the sample submission is unaffected (still 32.2, zero
      violations). See `docs/decisions.md`. **Run tests/CLIs with
      `.venv\Scripts\python.exe`** — bare `python` on PATH lacks pandas.
- [x] **Pre-submission compliance review + `src/api.py` (2026-09-20)** —
      re-read `PS1_README.md`/`Project_Framework.md` end-to-end against
      the actual code. Flagged §2.4 rule 10 (one-access-per-week) as a
      compliance gap and fixed it that day — **but this review's own
      reading of rule 10 was itself later reconsidered and reversed
      again the same day, see two bullets up.** Everything else in §2.4,
      §2.5's
      scenario objectives, and §2.6's output CSV schemas checked out
      against `constraints.py`/`output_writer.py`/`self_check.py` as
      already implemented. **§2.7 ("The output report") was claimed done
      here but wasn't — corrected same day, see the next bullet.** Built
      `src/api.py`, a single integration facade
      (`load_instance_from_uploads`, `validate_instance`,
      `solve() -> SolveResult`) so Role C's Streamlit app has one module
      to import instead of five — see its own docstring and
      `docs/decisions.md`. Validated via `tests/test_api.py`. Still not
      built (Role C's own scope, not ours): `app/streamlit_app.py` itself.
- [x] **`outputs/` restructured + §2.7 report.json actually written
      (2026-09-20)** — two fixes from the same user question ("why is
      the output split by engine, and where's §2.7's report?"):
      (1) `self_check.build_report()` always computed the exact §2.7
      `{scenario, feasible, hard_violations, soft_scores, detail}` shape,
      but nothing ever wrote it to a file next to the submission it
      describes — added `self_check.write_report(report, out_dir)`,
      called by both scheduler CLIs and `src.api.solve()`. Every
      `outputs/scenario_<X>/` now has `report.json` alongside the 3 CSVs.
      (2) The dev-time `outputs/scenario_<X>/` (greedy) vs.
      `outputs/scenario_<X>_cpsat/` (cpsat) split never matched
      PS1_README/Project_Framework's own template (one directory per
      scenario, no engine suffix) — collapsed to just
      `outputs/scenario_<X>/`, written by `cpsat_engine.main()` (the
      primary engine, which already internally picks whichever of
      {cpsat, greedy} scores better — this IS the single best answer, not
      an arbitrary pick). Plain greedy's own isolated result moved to
      `outputs/scenario_<X>_greedy_only/` and gitignored — useful for
      `tests/test_benchmark.py`'s comparison, not a second deliverable.
      All three `outputs/scenario_{A,B,C}/` regenerated and re-verified
      feasible (0 hard violations) before committing.

## Working style

Claude writes the code; the user reviews it — that's the standing workflow,
not a one-off. Explain judgment calls vs. derived/verified facts clearly in
comments/docstrings so review is fast (see `resolve_activity_path()` in
`src/data_model.py` for the pattern: a "Review points" docstring section
naming what's a judgment call). Validate against concrete test cases
(ideally the sample submission's ground truth) rather than asserting
correctness by eyeballing. Update this file's "Current status" and
`docs/decisions.md` as things change — they're what carries context forward
across sessions/tools, so keep them accurate rather than letting them go
stale.