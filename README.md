# Railway Track Access Optimiser

# Live web app: https://maintenance-scheduler-136023052892.us-central1.run.app/

Given the demand book for a dual-line rail network (8 CSV files), this tool
produces a nightly possession schedule for **Scenarios A, B and C** of
[Problem Statement 1](docs/PS1_README.md), checks it against every hard
safety rule, scores it, and explains it in plain English.

- **Input:** the 8 instance CSVs (`01_LINES.csv` … `08_ACTIVITY_DETAILS.csv`).
- **Output, per scenario:** `SCHEDULE_ACCESS.csv`, `SCHEDULE_OCCUPANCY.csv`,
  `RESULTS.csv` (PS1_README §2.6) and `report.json` (the §2.7 output report).
- **Result on the provided dataset:** all three scenarios feasible (0 hard
  violations, 100% of workload placed); penalty **0.0 / 14 / 7.0** for A / B / C,
  versus **32.2** for the organizer's own Scenario A sample.

## 1. Setup

Requires Python 3.10+ (developed on 3.13).

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt     # pandas, numpy, ortools (CP-SAT), protobuf
```

## 2. Generate a schedule from your own input data

Put the 8 instance CSVs in one folder, named as in the sample
(`data/sample_instance/01_data/` shows the format), then run one command
from the repo root:

```powershell
python -m src.api path\to\your_data all
```

`all` solves Scenarios A, B and C. Results are written to
`outputs/<your folder name>/scenario_A/`, `scenario_B/`, `scenario_C/`, each
containing the four files above, and a plain-English summary is printed per
scenario. It never overwrites the committed sample results.

| Option | Meaning |
|---|---|
| `A`, `B` or `C` instead of `all` | solve one scenario only |
| `--out some\folder` | write to `some\folder\scenario_<X>\` instead |
| `--engine greedy` | fast greedy scheduler only (default `cpsat` = best of CP-SAT and greedy) |
| `--time-limit 60` | CP-SAT time limit in seconds (default 20) |

Exit code: `0` all requested scenarios feasible · `1` a scenario infeasible ·
`2` invalid input (the message names the missing file/column, dangling
reference or predecessor cycle). Typical run time on the sample: 1–3 s per scenario.

To regenerate the committed results for the provided dataset:

```powershell
python -m src.cpsat_engine A     # likewise B, C  ->  outputs/scenario_<X>/
```

## 3. Use it as the solver behind a web app

`src/api.py` is a single in-memory entry point (no file paths, no global
state), so an upload-and-run web app is a thin wrapper around it:

```python
from src.api import load_instance_from_uploads, validate_instance, solve, SolveError

# uploaded_files: the 8 files from the upload widget (bytes, paths or file-like objects)
instance = load_instance_from_uploads({f.name: f for f in uploaded_files})

problems = validate_instance(instance)         # .errors block a run, .warnings don't
if not problems.ok:
    show_error("\n".join(problems.errors))

try:
    result = solve(instance, scenario="A")     # "A" | "B" | "C"; engine="cpsat" by default
except SolveError as e:
    show_error(str(e))                         # user-presentable message
```

`SolveResult` contains everything a UI needs:

| Field | Contents |
|---|---|
| `feasible`, `objective_score` | headline result (score only when feasible) |
| `metrics` | flat dict for KPI tiles: hard-violation count, overrun days, excess access-nights, ECLO nights, nights scheduled |
| `report` | the PS1_README §2.7 report (`hard_violations`, `soft_scores`, `detail`) |
| `explanation["summary"]` | headline plain-English lines, most important first (feasibility, violations, overruns, ECLO/excess nights, biggest delays) |
| `explanation["events"]`, `["by_activity"]`, `["by_contract"]` | every explained event with a plain sentence; sentences grouped per activity / contract for display next to them |
| `activity_timeline` | one row per activity (start/end date, contract, late flag, `explanation` text) — feed to `plotly.express.timeline` |
| `schedule_access`, `schedule_occupancy`, `results` | the three submission files as DataFrames |
| `to_zip_bytes()`, `write(dir)` | downloadable zip / write to disk (3 CSVs + `report.json`) |
| `warnings` | anything the user should know (e.g. engine fallback) |

`load_instance_from_uploads` matches filenames loosely (case, prefix) and
raises `SolveError` with a readable message for a missing or malformed file.
`solve()` never raises for a merely poor score, only for an invalid instance
or when no legal schedule exists at all.

## 4. What it does — program flow

```
8 CSVs ──► load + validate ──► scheduling engine ──► write 3 CSVs ──► self-check ──► score + explain
           (src/api.py,        (greedy safety net    (src/output_    (replays every  (report.json,
            data_model.py)      + CP-SAT hybrid)      writer.py)      access through  plain-English
                                                                       constraints.py) summary)
```

1. **Load and validate.** Parses the instance, resolves each activity's
   start→end location into the full list of tunnel and platform sectors it
   books, and rejects malformed input with a precise message.
2. **Schedule.** Two engines share one interface and one rule library:
   - *Greedy scheduler* (`greedy_scheduler.py`): sweeps week by week in
     priority order (earliest-deadline-first under Scenario B), placing each
     access only if it passes every hard rule; picks co-share groups, uses ECLO
     only when a hard deadline would otherwise be missed, and never emits an
     illegal placement. It always terminates with a complete schedule.
   - *CP-SAT hybrid* (`cpsat_engine.py`, the default): a CP-SAT model
     (OR-Tools, hard time limit) plans each activity's timing, predecessor
     order, weekly-allocation × workfront capacity and ECLO usage while
     minimising the real §2.5 objective; the greedy placer then sites every
     access under that plan. Both results are scored and the **better one is
     returned, so CP-SAT can never be worse than greedy**; on a timeout it falls
     back to greedy.
3. **Write.** `output_writer.py` emits the three submission CSVs in the exact
   §2.6 schemas.
4. **Self-check.** `self_check.py` re-reads the written CSVs and replays every
   access through the same legality functions the scheduler used
   (`constraints.py`), so scheduler and validator cannot disagree about what is
   legal. It recomputes every score independently of the scheduler.
5. **Score and explain.** `report.json` follows §2.7 (`feasible`,
   `hard_violations`, `soft_scores`, `objective_score`, capacity hotspots).
   `explain.py` explains *why*, for any schedule (ours, the organizer's, or an
   infeasible one), with the same rule library that produced it:
   - **Late starts:** for each week an activity could have started but did not,
     it re-checks the final schedule and names the blocking rule, the location,
     and the activities in the way (e.g. "wk15: platform PLAT:BET:H02:EB was at
     full capacity (1 of 1 slots), held by A040 (C007, priority 1)"), or says
     when nothing blocked it and it was simply scheduled later.
   - **Contract overruns:** classified as *unavoidable* (even the best
     contention-free pace finishes after the planned date under the stated rules),
     *contention* (blocked stretches, with causes) or *pace* (nights were free but
     the schedule used fewer), with the priority-weighted cost.
   - **Scenario levers:** each ECLO night and each excess access-night, its cost,
     and whether the deadline arithmetic forced it.
   - **Infeasible schedules:** hard violations grouped by rule, with examples.

   `python -m src.explain outputs/scenario_A --all` prints everything.

## 5. Scenarios and rules implemented

| | A — strict supply | B — strict schedule | C — balanced |
|---|---|---|---|
| Location capacity | hard | soft (extra nights scored) | soft, ≤1 excess per location-week |
| Completion date | soft (priority-weighted overrun) | hard | soft (priority-weighted overrun) |
| ECLO | forbidden | allowed | allowed, ≤2-week window per line |
| Penalty | overrun | 7×excess nights + 5×ECLO | overrun + both |

Hard rules, in every scenario (PS1_README §2.4): 100% workload placed
(standard night 1.0, ECLO 1.5); no start before the planned start week;
predecessor finish-to-start (and cycle detection); legal possession mix per
location-week (one PM alone, or 1 PC + ≤3 C, or ≤4 C) with co-share groups as
one possession; weekly allocation cap and workfront limit per contract;
`Live` opposite-bound mirroring and the `Live`-only interchange crossover.
The legality library is `src/constraints.py`.

## 6. Verification

```powershell
python tests\test_greedy_scheduler.py     # any tests\test_*.py; each is a plain script
python tests\test_benchmark.py            # penalty scores: organizer sample vs greedy vs CP-SAT, all scenarios
```

Nine fast regression tests (~40 s in total) cover: the input data, path
resolution against the organizer's sample, every rule replayed against the
organizer's feasible sample submission (zero violations), output writer
round-trip, self-check values, both engines on all three scenarios, the
explanation numbers, and the `src/api.py` contract (upload loading, validation
errors, all scenarios × both engines, and the CLI run on a separate data folder). `test_benchmark.py` confirms CP-SAT never scores worse than
greedy.

## 7. Assumptions and simplifications (stated openly)

Two readings of PS1_README were judgment calls; both are documented with their
evidence in [`docs/decisions.md`](docs/decisions.md):

- **Buffers (§2.4 rule 4) are not a hard check.** Enforcing the literal
  buffer-extension reading contradicted the organizer's own feasible sample
  (34 violations) and made schedules ~26× worse; it was removed on that
  evidence. As a hedge, both engines *prefer* not to place two buffer-carrying
  activities with overlapping footprints in the same week unless co-shared
  (0 such pairs on the provided data, versus 14 in the sample); reported as
  `detail.buffer_proximity_pairs`. `Live` mirroring and interchange crossover
  remain fully hard.
- **"At most one access-night per week" (§2.4 rule 10) is read as belonging to
  the ECLO-window rule**, not as a cap on every activity — weekly limits are
  otherwise stated per contract (rules 6–8, §2.1, §2.6). An activity may
  therefore take several of its contract's granted nights in a week. The strict
  reading is available: `solve(..., strict_one_access_per_week=True)` or
  `--strict-one-access-per-week` (penalty on the sample: 25.2 / 44 / 32.2, still
  feasible).

## Repository layout

```
src/api.py               entry point for CLI and web app
src/constraints.py       every legality rule, implemented once
src/greedy_scheduler.py  safety-net engine
src/cpsat_engine.py      CP-SAT hybrid engine (default)
src/output_writer.py     the three submission CSVs
src/self_check.py        independent validator, §2.7 report, scoring
src/explain.py           plain-English explanation
src/data_model.py        CSV loading, sector-path resolution
tests/                   regression tests + benchmark
data/sample_instance/    provided dataset and the organizer's sample submission
outputs/scenario_{A,B,C}/  our results on the provided dataset (Public Test Results)
docs/                    problem statement, design plan, decisions log
```
