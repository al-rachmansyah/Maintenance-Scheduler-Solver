#!/usr/bin/env python3
"""
Local web server for the Track Access Console (Role C).

Serves app/static/index.html and a small JSON API that solves the CURRENT
contents of the instance data, pulled from one of two sources:

  --source github (default)  the 8 CSVs are fetched over HTTP from a GitHub
                              repo/branch/path (raw.githubusercontent.com) —
                              so editing and pushing a CSV on GitHub is what
                              drives a reschedule, not this machine's disk.
  --source local              the 8 CSVs are read from --data-dir on this
                               machine (default: data/sample_instance/01_data).

A third source, "upload", is entered via POST /api/upload (see the UI's
"Upload dataset" button) — a user-picked set of CSVs read client-side and
validated through the same load_instance_from_uploads() path as GitHub mode.
POST /api/source {"source": "github"|"local"} reverts back to a live source.

POST /api/chat (optional — needs GEMINI_API_KEY, see .env.example) answers
questions grounded in a live solve() of whichever source is currently active,
via the same solve_scenario() cache as the rest of the app. Ported from
webapp/app.py's standalone FastAPI service into this process directly, so
there's one server/port and the chatbot always reflects the real live data.

Either way: a background thread re-checks the source periodically (3s for
local disk — a cheap stat(); throttled to at most once every
GITHUB_MIN_POLL_SECONDS for GitHub, since that's a real network call) and
recomputes the affected scenario if it changed, so the frontend's own status
poll picks up new data with no restart. The "Reschedule" button calls the
force-recompute endpoint directly, bypassing the change check.

Run from the repo root (after `pip install -r requirements.txt`):
    .venv/bin/python app/server.py [--port 8934]
    .venv/bin/python app/server.py --source local --data-dir PATH
    .venv/bin/python app/server.py --github-repo owner/repo --github-branch main --github-path data/sample_instance/01_data
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.api import REQUIRED_INSTANCE_FILES, SolveError, load_instance_from_uploads, solve, validate_instance  # noqa: E402
from src.data_model import DATA_DIR, Instance, load_instance  # noqa: E402
from src.self_check import _json_default  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")  # optional — GEMINI_API_KEY can also just be a real env var (e.g. on Render)
except ImportError:
    pass

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8MB — 8 small CSVs, generous headroom
CHAT_MODEL = "gemini-3.6-flash"
GCS_BUCKET = os.environ.get("GCS_BUCKET")  # optional — persists an uploaded dataset across restarts/redeploys
GCS_PREFIX = "dataset/"

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}
SCENARIOS = ("A", "B", "C")
TIME_LIMIT_SECONDS = 8.0
WATCH_INTERVAL_SECONDS = 3.0
GITHUB_MIN_POLL_SECONDS = 10.0  # floor between real network fetches, regardless of how often callers ask
GITHUB_FETCH_TIMEOUT = 10.0

DEFAULT_GITHUB_REPO = "al-rachmansyah/Maintenance-Scheduler-Solver"
DEFAULT_GITHUB_BRANCH = "main"
DEFAULT_GITHUB_PATH = "data/sample_instance/01_data"

_lock = threading.RLock()
_state = {
    "source": "github",
    "data_dir": DATA_DIR,
    "github_repo": DEFAULT_GITHUB_REPO,
    "github_branch": DEFAULT_GITHUB_BRANCH,
    "github_path": DEFAULT_GITHUB_PATH,
    "last_github_fetch_at": 0.0,
    "source_error": None,
    "instance": None,
    "instance_hash": None,
    "scenarios": {},  # scenario -> {"hash": str, "payload": dict}
    "last_scenario": "A",
    "upload_meta": None,  # {"filenames": [...], "uploaded_at": iso str} — set on a successful /api/upload
    "gcs_restore_attempted": False,  # ensure_instance() only tries GCS once per "none" cold start / reset
}


# ----------------------------------------------------------------------
# Instance loading / change detection
# ----------------------------------------------------------------------


def data_hash(data_dir: Path) -> str:
    """Cheap fingerprint of a local instance folder — stat() only, no file reads."""
    h = hashlib.sha256()
    for name in REQUIRED_INSTANCE_FILES:
        p = data_dir / name
        try:
            st = p.stat()
            h.update(f"{name}:{st.st_mtime_ns}:{st.st_size}".encode())
        except FileNotFoundError:
            h.update(f"{name}:MISSING".encode())
    return h.hexdigest()[:16]


def fetch_github_files() -> dict[str, bytes]:
    """Download the 8 instance CSVs from raw.githubusercontent.com. Raises on any failure —
    callers decide whether to fall back to the last-known-good cached instance."""
    repo = _state["github_repo"]
    branch = _state["github_branch"]
    path = _state["github_path"].strip("/")
    files: dict[str, bytes] = {}
    for name in REQUIRED_INSTANCE_FILES:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}/{name}"
        req = urllib.request.Request(url, headers={"User-Agent": "track-access-console"})
        try:
            with urllib.request.urlopen(req, timeout=GITHUB_FETCH_TIMEOUT) as resp:
                files[name] = resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitHub returned {exc.code} for {name} ({url})") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach GitHub for {name}: {exc.reason}") from exc
    return files


def data_hash_github(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in REQUIRED_INSTANCE_FILES:
        h.update(name.encode())
        h.update(files[name])
    return h.hexdigest()[:16]


def _upload_hash(file_bytes: dict[str, bytes]) -> str:
    return hashlib.sha256(b"".join(file_bytes[name] for name in REQUIRED_INSTANCE_FILES)).hexdigest()[:16]


# ----------------------------------------------------------------------
# GCS persistence for an uploaded dataset (optional — only active when
# GCS_BUCKET is set). This is what lets "upload" survive a Cloud Run
# redeploy/restart instead of forcing every visitor to re-upload: the judge
# link stays populated with whatever was last uploaded, by anyone, until
# someone clicks Reset.
# ----------------------------------------------------------------------


def _gcs_bucket():
    if not GCS_BUCKET:
        return None
    try:
        from google.cloud import storage

        return storage.Client().bucket(GCS_BUCKET)
    except Exception:
        traceback.print_exc()
        return None  # no credentials / package missing / bucket unreachable — degrade to in-memory only


def gcs_save_dataset(file_bytes: dict[str, bytes]) -> None:
    bucket = _gcs_bucket()
    if not bucket:
        return
    try:
        for name in REQUIRED_INSTANCE_FILES:
            bucket.blob(GCS_PREFIX + name).upload_from_string(file_bytes[name], content_type="text/csv")
    except Exception:
        traceback.print_exc()  # best-effort — an upload still works locally even if persistence fails


def gcs_load_dataset() -> dict[str, bytes] | None:
    bucket = _gcs_bucket()
    if not bucket:
        return None
    try:
        files: dict[str, bytes] = {}
        for name in REQUIRED_INSTANCE_FILES:
            blob = bucket.blob(GCS_PREFIX + name)
            if not blob.exists():
                return None  # incomplete set — treat as nothing persisted
            files[name] = blob.download_as_bytes()
        return files
    except Exception:
        traceback.print_exc()
        return None


def gcs_clear_dataset() -> None:
    bucket = _gcs_bucket()
    if not bucket:
        return
    try:
        for name in REQUIRED_INSTANCE_FILES:
            blob = bucket.blob(GCS_PREFIX + name)
            if blob.exists():
                blob.delete()
    except Exception:
        traceback.print_exc()


def ensure_instance(force: bool = False):
    """
    Refresh the cached instance from its source if needed. Returns (instance, hash, changed).

    Local: checked on every call (a stat() is cheap). GitHub: a real network call, so it's
    throttled to at most once every GITHUB_MIN_POLL_SECONDS unless force=True — callers (the
    frontend's status poll, the background watcher) can ask as often as they like without
    that turning into a GitHub request storm. Upload: nothing to poll — an uploaded dataset only
    changes when POST /api/upload replaces it (that handler sets instance/instance_hash itself).
    None: the judge-facing "nothing loaded yet" state — no dataset until someone uploads one or
    loads the GitHub sample; on first check, tries to restore a previously-uploaded dataset from
    GCS (if configured) so a redeploy doesn't wipe out what a judge already uploaded.
    """
    if _state["source"] == "none":
        with _lock:
            if _state["instance"] is None and not _state["gcs_restore_attempted"]:
                _state["gcs_restore_attempted"] = True
                files = gcs_load_dataset()
                if files:
                    try:
                        instance = load_instance_from_uploads(files)
                        check = validate_instance(instance)
                    except Exception:
                        instance, check = None, None
                    if instance is not None and check is not None and check.ok:
                        _state["instance"] = instance
                        _state["instance_hash"] = _upload_hash(files)
                        _state["source"] = "upload"
                        _state["upload_meta"] = {"filenames": sorted(files.keys()), "uploaded_at": "restored from storage"}
            return _state["instance"], _state["instance_hash"], False

    if _state["source"] == "upload":
        with _lock:
            return _state["instance"], _state["instance_hash"], False

    if _state["source"] == "local":
        h = data_hash(_state["data_dir"])
        with _lock:
            changed = force or h != _state["instance_hash"]
            if changed:
                _state["instance"] = load_instance(_state["data_dir"])
                _state["instance_hash"] = h
                _state["scenarios"] = {}
                _state["source_error"] = None
            return _state["instance"], _state["instance_hash"], changed

    # github
    now = time.time()
    with _lock:
        due = force or _state["instance"] is None or (now - _state["last_github_fetch_at"] >= GITHUB_MIN_POLL_SECONDS)
    if not due:
        with _lock:
            return _state["instance"], _state["instance_hash"], False

    try:
        files = fetch_github_files()
        h = data_hash_github(files)
    except Exception as exc:
        with _lock:
            _state["last_github_fetch_at"] = now
            _state["source_error"] = str(exc)
            if _state["instance"] is None:
                raise  # nothing to fall back to — let the caller surface this as an error
            return _state["instance"], _state["instance_hash"], False

    with _lock:
        _state["last_github_fetch_at"] = now
        _state["source_error"] = None
        changed = force or h != _state["instance_hash"]
        if changed:
            _state["instance"] = load_instance_from_uploads(files)
            _state["instance_hash"] = h
            _state["scenarios"] = {}
        return _state["instance"], _state["instance_hash"], changed


def _source_label() -> str:
    src = _state["source"]
    if src == "github":
        return f"{_state['github_repo']}@{_state['github_branch']}/{_state['github_path']}"
    if src == "local":
        return str(_state["data_dir"])
    if src == "none":
        return "No dataset loaded yet"
    meta = _state["upload_meta"]
    return f"Uploaded dataset ({len(meta['filenames'])} files)" if meta else "Uploaded dataset"


# ----------------------------------------------------------------------
# Instance -> JSON (static network/contract/activity data, independent of scenario)
# ----------------------------------------------------------------------


def static_payload(instance: Instance) -> dict:
    horizon_start = str(instance.parameters["horizon_start"])
    horizon_weeks = int(instance.parameters["horizon_weeks"])

    lines = [{"code": r.line_code, "name": r.line_name} for r in instance.lines.itertuples()]
    stations = [
        {"id": r.station_id, "line": r.line_code, "seq": int(r.seq), "interchange": bool(int(r.is_interchange))}
        for r in instance.stations.itertuples()
    ]
    sectors = [
        {"id": r.sector_id, "line": r.line_code, "from": r.from_station_id, "to": r.to_station_id, "seq": int(r.seq)}
        for r in instance.sectors.itertuples()
    ]
    location_supply = {
        r.location_id: {"kind": r.location_kind, "line": r.line_code, "bound": r.bound, "capacity": int(r.supply_capacity)}
        for r in instance.location_supply.itertuples()
    }
    contracts = {}
    for r in instance.contracts.itertuples():
        contracts[r.contract_number] = {
            "contract_number": r.contract_number,
            "description": r.contract_description,
            "award_date": str(r.contract_award_date),
            "activity_type": r.activity_type,
            "nature": r.nature_of_activity,
            "priority": int(r.contract_priority),
            "completion_date": str(r.contract_completion_date),
            "planned_completion_date": str(r.planned_completion_date),
            "workfronts": int(r.number_of_workfronts),
            "access_type": r.access_type,
            "max_access_per_week": int(r.number_of_maximum_access_per_week),
        }
    activities = {}
    for r in instance.activities.itertuples():
        start_loc = r.start_location_id
        pred = r.predecessor_activity_id
        activities[r.activity_id] = {
            "activity_id": r.activity_id,
            "contract_number": r.contract_number,
            "activity_type": r.activity_type,
            "start_location_id": start_loc,
            "end_location_id": r.end_location_id,
            "line": start_loc.split(":")[1],
            "total_accesses": int(r.total_accesses),
            "planned_start_date": str(r.planned_start_date),
            "predecessor_activity_id": None if pd.isna(pred) else pred,
            "activity_priority": int(r.activity_priority),
        }
    return {
        "params": {"horizon_start": horizon_start, "horizon_weeks": horizon_weeks},
        "lines": lines,
        "stations": stations,
        "sectors": sectors,
        "location_supply": location_supply,
        "contracts": contracts,
        "activities": activities,
    }


# ----------------------------------------------------------------------
# SolveResult -> JSON (per-scenario schedule)
# ----------------------------------------------------------------------


def scenario_payload(instance: Instance, result) -> dict:
    horizon_start = date.fromisoformat(str(instance.parameters["horizon_start"]))

    def week_to_date(week: int, offset: int = 0) -> str:
        return (horizon_start + timedelta(days=(week - 1) * 7 + offset)).isoformat()

    results = {
        r.contract_number: {
            "simulated_completion_date": r.simulated_completion_date,
            "overrun_days": int(r.overrun_days),
        }
        for r in result.results.itertuples()
    }

    per_activity_access: dict[str, list[dict]] = {}
    for r in result.schedule_access.itertuples():
        per_activity_access.setdefault(r.activity_id, []).append(
            {"seq": int(r.access_seq), "week": int(r.week), "eclo": bool(int(r.eclo)), "night": int(r.access_night)}
        )

    per_activity_occ: dict[str, set] = {}
    weekly_location_map: dict[str, dict[str, list[str]]] = {}
    for r in result.schedule_occupancy.itertuples():
        wk = int(r.week)
        per_activity_occ.setdefault(r.activity_id, set()).add(r.location_id)
        bucket = weekly_location_map.setdefault(str(wk), {}).setdefault(r.location_id, [])
        if r.activity_id not in bucket:
            bucket.append(r.activity_id)

    activities_scen = {}
    for aid, accs in per_activity_access.items():
        weeks = sorted(a["week"] for a in accs)
        start_week, end_week = weeks[0], weeks[-1]
        activities_scen[aid] = {
            "weeks": weeks,
            "accesses": sorted(accs, key=lambda a: a["seq"]),
            "start_week": start_week,
            "end_week": end_week,
            "start_date": week_to_date(start_week),
            "end_date": week_to_date(end_week, 6),
            "eclo_nights": sum(1 for a in accs if a["eclo"]),
            "occupied_locations": sorted(per_activity_occ.get(aid, [])),
        }

    return {
        "results": results,
        "activities": activities_scen,
        "weekly_location_map": weekly_location_map,
        "report": result.report,
        "explanation": result.explanation,
    }


NO_DATASET_MESSAGE = 'No dataset has been loaded yet. Click "Upload dataset" (or "Load sample data") to get started.'


def solve_scenario(scenario: str, force: bool) -> dict:
    instance, h, _ = ensure_instance()
    if instance is None:
        raise SolveError(NO_DATASET_MESSAGE)
    if not force:
        with _lock:
            cached = _state["scenarios"].get(scenario)
            if cached and cached["hash"] == h:
                return cached["payload"]

    t0 = time.perf_counter()
    result = solve(instance, scenario, time_limit_seconds=TIME_LIMIT_SECONDS)
    elapsed = time.perf_counter() - t0

    payload = {
        "scenario": scenario,
        **scenario_payload(instance, result),
        "engine_used": result.engine_used,
        "elapsed_seconds": round(elapsed, 2),
        "solved_at": datetime.now(timezone.utc).isoformat(),
        "data_hash": h,
        "warnings": result.warnings,
    }
    with _lock:
        # A concurrent edit could have moved the hash again mid-solve — only cache if still current.
        if _state["instance_hash"] == h:
            # `result` (the raw SolveResult, with its files()/to_zip_bytes() export helpers) is
            # cached alongside the JSON payload so GET /api/export can reuse whatever's already
            # been solved instead of re-solving — the export then matches exactly what's on screen.
            _state["scenarios"][scenario] = {"hash": h, "payload": payload, "result": result}
        _state["last_scenario"] = scenario
    return payload


def build_export_zip() -> bytes:
    """
    All three scenarios' outputs, zipped as scenario_A/, scenario_B/, scenario_C/ —
    the same SCHEDULE_ACCESS.csv / SCHEDULE_OCCUPANCY.csv / RESULTS.csv / report.json
    per scenario that `python -m src.cpsat_engine <X>` writes to outputs/scenario_<X>/
    (see SolveResult.files() in src/api.py), bundled for "download everything" in one click.
    """
    instance, h, _ = ensure_instance()
    if instance is None:
        raise SolveError(NO_DATASET_MESSAGE)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for scenario in SCENARIOS:
            solve_scenario(scenario, force=False)
            with _lock:
                cached = _state["scenarios"].get(scenario)
            if not cached or cached["hash"] != h:
                # Data changed under us mid-export (rare) — resolve once more for this scenario.
                solve_scenario(scenario, force=True)
                with _lock:
                    cached = _state["scenarios"].get(scenario)
            result = cached["result"]
            for name, data in result.files().items():
                zf.writestr(f"scenario_{scenario}/{name}", data)
    return buf.getvalue()


# ----------------------------------------------------------------------
# Chat assistant — ported from webapp/app.py (originally a standalone FastAPI
# service against the hardcoded sample instance) into this server directly,
# so it runs as one process/port and is grounded in whatever source is
# ACTUALLY active right now — GitHub, local disk, or an uploaded dataset —
# via the same solve_scenario() cache everything else uses, not a separate
# one. See webapp/README.md for the original design and setup notes.
# ----------------------------------------------------------------------


def build_chat_context() -> str:
    """Plain-text account of a live solve() across all 3 scenarios — the ONLY source of
    truth handed to the model. Built from solve_scenario()'s own cached payloads (report,
    results, activities, weekly_location_map, explanation) — nothing here is re-derived
    or invented."""
    instance, h, _ = ensure_instance()
    if instance is None:
        raise SolveError(NO_DATASET_MESSAGE + " There's nothing to chat about yet.")
    contracts = instance.contracts.set_index("contract_number")
    activities_idx = instance.activities.set_index("activity_id")

    lines = [
        f"Planning horizon: {instance.parameters['horizon_weeks']} weeks, starting {instance.parameters['horizon_start']}.",
        f"Schedule source: live solve() against {_source_label()} (CP-SAT hybrid engine, falls back to greedy on timeout).",
        "",
        "CONTRACTS:",
    ]
    for contract_number, c in contracts.iterrows():
        lines.append(
            f"- {contract_number}: {c['contract_description']} | priority P{c['contract_priority']} | "
            f"{c['nature_of_activity']} | planned completion {c['planned_completion_date']}"
        )

    for sc in SCENARIOS:
        try:
            payload = solve_scenario(sc, force=False)
        except SolveError as exc:
            lines.append(f"\n=== SCENARIO {sc}: could not be solved ({exc}) ===")
            continue

        report = payload["report"]
        lines.append(
            f"\n=== SCENARIO {sc} (feasible={report['feasible']}, "
            f"objective_score={report.get('objective_score')}, engine={payload['engine_used']}) ==="
        )

        lines.append("Contract completion:")
        for cid, r in payload["results"].items():
            lines.append(f"  {cid}: completes {r['simulated_completion_date']}, overrun {r['overrun_days']} days")

        lines.append("Activity timeline:")
        for aid, sched in payload["activities"].items():
            activity = activities_idx.loc[aid]
            lines.append(
                f"  {aid} (contract {activity['contract_number']}, {activity['activity_type']}): "
                f"weeks {sched['start_week']}-{sched['end_week']}, {len(sched['accesses'])} access-nights"
                + (f", {sched['eclo_nights']} ECLO" if sched["eclo_nights"] else "")
            )

        occ_by_location: dict[str, dict[int, list[str]]] = {}
        for wk_str, locs in payload["weekly_location_map"].items():
            for loc, acts in locs.items():
                occ_by_location.setdefault(loc, {})[int(wk_str)] = acts
        lines.append("Location occupancy (location: week -> activities there that week):")
        for loc in sorted(occ_by_location):
            weeks = occ_by_location[loc]
            week_strs = [f"wk{w}:{','.join(sorted(set(weeks[w])))}" for w in sorted(weeks)]
            lines.append(f"  {loc}: " + "; ".join(week_strs))

        summary = payload.get("explanation", {}).get("summary", [])
        if summary:
            lines.append("Solver's own explanation (headline points):")
            for line in summary[:15]:
                lines.append(f"  - {line}")

    return "\n".join(lines)


CHAT_SYSTEM_PROMPT = (
    "You are a planning assistant for a railway track access maintenance scheduler, used by a "
    "works controller. Answer questions using ONLY the real schedule data below -- never invent "
    "activity IDs, contract numbers, weeks, or scores that aren't in it. If the data doesn't answer "
    "the question, say so plainly. Be concise and cite exact IDs/weeks/numbers from the data. "
    "Reply in PLAIN TEXT ONLY -- no Markdown (no **, no #, no backticks). For lists, use a plain "
    "dash and a line break, nothing else.\n\n"
)


def chat_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def run_chat(message: str, history: list[dict]) -> str:
    if not chat_available():
        raise SolveError(
            "GEMINI_API_KEY is not set on the server. Get a free key at "
            "https://aistudio.google.com/apikey, then either put it in a .env file at the repo "
            "root (copy .env.example) or set it as a real environment variable, and restart the server."
        )
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise SolveError("google-genai isn't installed — run: pip install -r requirements.txt") from exc

    context = build_chat_context()
    contents = [
        types.Content(role=("model" if m.get("role") == "model" else "user"), parts=[types.Part(text=str(m.get("content", "")))])
        for m in history
        if str(m.get("content", "")).strip()
    ]
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    try:
        response = client.models.generate_content(
            model=CHAT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=CHAT_SYSTEM_PROMPT + context),
        )
    except Exception as exc:  # the Gemini SDK's own error types aren't worth importing just to catch
        raise SolveError(f"Gemini request failed: {exc}") from exc
    return response.text


# ----------------------------------------------------------------------
# Background watcher — keeps the last-viewed scenario warm after an edit
# ----------------------------------------------------------------------


def _watch_loop():
    while True:
        time.sleep(WATCH_INTERVAL_SECONDS)
        try:
            _, _, changed = ensure_instance()
            if changed:
                target = _state.get("last_scenario") or "A"
                try:
                    solve_scenario(target, force=False)
                except SolveError:
                    pass  # surfaced to the client on its own next request instead
        except Exception:
            traceback.print_exc()


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "TrackAccessConsole/1.0"

    def log_message(self, fmt, *args):
        pass  # the CLI banner is enough; per-request noise isn't useful here

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_download(self, data: bytes, content_type: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str):
        """Serve a file under STATIC_DIR by its URL path (e.g. "/app.js" -> static/app.js)."""
        rel = path.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            return self._send_json({"error": "Not found"}, 404)
        if not candidate.is_file():
            return self._send_json({"error": "Not found"}, 404)
        content_type = STATIC_CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        return self._send_file(candidate, content_type)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        if length > MAX_UPLOAD_BYTES:
            raise SolveError(f"Upload too large ({length} bytes, max {MAX_UPLOAD_BYTES}).")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception as exc:
            raise SolveError(f"Malformed JSON body: {exc}") from exc

    def _handle_upload(self):
        """
        POST /api/upload — body: {"files": {filename: text_content, ...}}. The frontend reads
        the user's picked files as text client-side (they're all small CSVs) so this server
        never needs multipart parsing. Reuses load_instance_from_uploads — the exact same path
        GitHub mode already goes through — so an uploaded dataset gets identical validation and
        drives every feature (Gantt, map, weekly summary, reschedule) exactly like GitHub data.
        """
        body = self._read_json_body()
        files_in = body.get("files")
        if not isinstance(files_in, dict) or not files_in:
            return self._send_json({"error": "No files were received."}, 400)

        file_bytes = {}
        for name, content in files_in.items():
            if not isinstance(content, str):
                return self._send_json({"error": f"{name}: expected text content."}, 400)
            file_bytes[name] = content.encode("utf-8")

        instance = load_instance_from_uploads(file_bytes)  # raises SolveError, listing exactly what's missing/malformed
        check = validate_instance(instance)
        if not check.ok:
            return self._send_json({"error": "Uploaded dataset is invalid:\n" + "\n".join(f"- {e}" for e in check.errors)}, 422)

        h = _upload_hash(file_bytes)
        with _lock:
            _state["source"] = "upload"
            _state["instance"] = instance
            _state["instance_hash"] = h
            _state["scenarios"] = {}
            _state["source_error"] = None
            _state["upload_meta"] = {
                "filenames": sorted(files_in.keys()),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        gcs_save_dataset(file_bytes)  # best-effort — so this dataset survives a restart/redeploy (see module docstring)
        return self._send_json({
            "ok": True,
            "data_hash": h,
            "source": "upload",
            "source_label": _source_label(),
            "warnings": check.warnings,
            "persisted": bool(GCS_BUCKET),
        })

    def _handle_source_switch(self):
        """POST /api/source — body: {"source": "github"|"local"} — reverts an uploaded dataset
        back to the live, pollable source (point 4: upload is additive, never a dead end)."""
        body = self._read_json_body()
        src = body.get("source")
        if src not in ("github", "local"):
            return self._send_json({"error": "source must be 'github' or 'local'."}, 400)
        with _lock:
            _state["source"] = src
            _state["instance"] = None
            _state["instance_hash"] = None
            _state["scenarios"] = {}
            _state["last_github_fetch_at"] = 0.0
            _state["upload_meta"] = None
        ensure_instance(force=True)
        return self._send_json({"ok": True, "source": src, "source_label": _source_label()})

    def _handle_reset(self):
        """POST /api/reset — wipes the current dataset (and its GCS copy, if any) back to
        "nothing loaded" so the next visitor sees the upload/sample-data onboarding screen.
        Does not touch the GitHub/local source config — reset always lands on "none"; from
        there, upload or 'load sample data' start fresh."""
        with _lock:
            _state["source"] = "none"
            _state["instance"] = None
            _state["instance_hash"] = None
            _state["scenarios"] = {}
            _state["upload_meta"] = None
            _state["source_error"] = None
            _state["gcs_restore_attempted"] = True  # don't immediately re-restore what we're about to delete
        gcs_clear_dataset()
        return self._send_json({"ok": True, "source": "none", "source_label": _source_label()})

    def _handle_export(self):
        """GET /api/export — a zip of all three scenarios' SCHEDULE_ACCESS.csv,
        SCHEDULE_OCCUPANCY.csv, RESULTS.csv and report.json (solving any that aren't
        already cached for the current data)."""
        data = build_export_zip()
        filename = f"maintenance_schedule_export_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
        return self._send_download(data, "application/zip", filename)

    def _handle_chat(self):
        """POST /api/chat — body: {"message": str, "history": [{"role","content"}, ...]}."""
        body = self._read_json_body()
        message = str(body.get("message") or "").strip()
        if not message:
            return self._send_json({"error": "message is required."}, 400)
        history = body.get("history") or []
        if not isinstance(history, list):
            return self._send_json({"error": "history must be a list."}, 400)
        reply = run_chat(message, history)  # raises SolveError for anything the UI should show directly
        return self._send_json({"reply": reply})

    def _dispatch(self, method: str):
        path = urlparse(self.path).path
        try:
            if method == "GET" and path == "/":
                return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

            if method == "GET" and not path.startswith("/api/") and "." in path.rsplit("/", 1)[-1]:
                return self._serve_static(path)

            if method == "GET" and path == "/api/meta":
                instance, h, _ = ensure_instance()
                if instance is None:
                    with _lock:
                        return self._send_json({"unconfigured": True, "source": _state["source"], "source_label": _source_label()})
                payload = static_payload(instance)
                payload["data_hash"] = h
                with _lock:
                    payload["source"] = _state["source"]
                    payload["source_label"] = _source_label()
                    payload["upload_meta"] = _state["upload_meta"]
                return self._send_json(payload)

            if method == "GET" and path == "/api/status":
                instance, h, _ = ensure_instance()
                with _lock:
                    scen = {
                        s: {"cached": s in _state["scenarios"], "hash": _state["scenarios"].get(s, {}).get("hash")}
                        for s in SCENARIOS
                    }
                    source_info = {
                        "source": _state["source"],
                        "source_label": _source_label(),
                        "source_error": _state["source_error"],
                        "github_repo": _state["github_repo"] if _state["source"] == "github" else None,
                        "github_branch": _state["github_branch"] if _state["source"] == "github" else None,
                        "github_path": _state["github_path"] if _state["source"] == "github" else None,
                    }
                return self._send_json({
                    "data_hash": h,
                    "scenarios": scen,
                    "chat_available": chat_available(),
                    "unconfigured": instance is None,
                    **source_info,
                })

            if path.startswith("/api/scenario/"):
                scen = path.rsplit("/", 1)[-1].upper()
                if scen not in SCENARIOS:
                    return self._send_json({"error": f"Unknown scenario '{scen}'."}, 404)
                if method not in ("GET",):
                    return self._send_json({"error": "Method not allowed"}, 405)
                payload = solve_scenario(scen, force=False)
                return self._send_json(payload)

            if path.startswith("/api/reschedule/"):
                scen = path.rsplit("/", 1)[-1].upper()
                if scen not in SCENARIOS:
                    return self._send_json({"error": f"Unknown scenario '{scen}'."}, 404)
                if method != "POST":
                    return self._send_json({"error": "Method not allowed"}, 405)
                payload = solve_scenario(scen, force=True)
                return self._send_json(payload)

            if path == "/api/upload" and method == "POST":
                return self._handle_upload()

            if path == "/api/source" and method == "POST":
                return self._handle_source_switch()

            if path == "/api/reset" and method == "POST":
                return self._handle_reset()

            if path == "/api/export" and method == "GET":
                return self._handle_export()

            if path == "/api/chat" and method == "POST":
                return self._handle_chat()

            return self._send_json({"error": "Not found"}, 404)
        except SolveError as exc:
            return self._send_json({"error": str(exc)}, 422)
        except Exception as exc:  # keep the server alive; surface the error to the UI instead
            traceback.print_exc()
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # CLI flags win; each also falls back to an env var of the same name (upper-cased, no
    # leading --) so a host like Render can be reconfigured from its dashboard alone, with
    # no redeploy needed to point at a different fork/branch/path.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8934)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="use 0.0.0.0 to accept connections from other machines (e.g. behind a tunnel or on a cloud host)")
    parser.add_argument("--source", choices=("github", "local", "none"), default=os.environ.get("SOURCE", "github"))
    parser.add_argument("--github-repo", default=os.environ.get("GITHUB_REPO", DEFAULT_GITHUB_REPO), help="owner/repo")
    parser.add_argument("--github-branch", default=os.environ.get("GITHUB_BRANCH", DEFAULT_GITHUB_BRANCH))
    parser.add_argument("--github-path", default=os.environ.get("GITHUB_PATH", DEFAULT_GITHUB_PATH), help="path to the folder holding the 8 instance CSVs")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else None, help="local instance folder — implies --source local")
    args = parser.parse_args()

    _state["source"] = "local" if args.data_dir else args.source
    _state["github_repo"] = args.github_repo
    _state["github_branch"] = args.github_branch
    _state["github_path"] = args.github_path
    if args.data_dir:
        _state["data_dir"] = args.data_dir.resolve()

    source_desc = {
        "github": f"GitHub: {_state['github_repo']}@{_state['github_branch']}/{_state['github_path']}",
        "local": f"local disk: {_state['data_dir']}",
        "none": "none (waiting for a dataset upload)",
    }[_state["source"]]

    try:
        ensure_instance(force=True)  # fail fast if the instance is broken, before opening the port
    except Exception as exc:
        print(f"ERROR: could not load instance from {source_desc}: {exc}")
        return 2

    threading.Thread(target=_watch_loop, daemon=True).start()

    def _warm_up() -> None:
        # OR-tools' first solve in a fresh process pays a one-time native-library warm-up
        # cost (~20-30s here vs ~1-2s after) — kick it off now, in the background, so it's
        # usually already done by the time a browser finishes loading the page and asks for
        # Scenario A. Skipped (silently) when there's no dataset loaded yet.
        try:
            solve_scenario("A", force=False)
        except SolveError:
            pass

    threading.Thread(target=_warm_up, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Track Access Console — http://{args.host}:{args.port}")
    print(f"Instance data source: {source_desc}")
    if _state["source"] == "none":
        print("No dataset loaded — upload one from the web UI to get started.")
    else:
        print("Warming up the solver in the background (first Scenario A load may still take up to ~30s)...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
