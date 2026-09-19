"""
Minimal FastAPI service adding a chat assistant on top of the solver's
in-memory facade (src/api.py). Everything the chatbot says traces back to a
real solve() call against the organizer's sample instance -- nothing is
invented or hardcoded. Run from the repo root:

    pip install -r webapp/requirements.txt
    python -m webapp.app

Requires a GEMINI_API_KEY in a .env file at the repo root (see .env.example).
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api import SolveResult, load_sample_instance, solve

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="Maintenance Scheduler Chat Assistant", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://127.0.0.1:3000",  # Vite/CRA-style frontends
        "http://localhost:5173", "http://127.0.0.1:5173",  # Vite default port
        "http://localhost:8501", "http://127.0.0.1:8501",  # Streamlit default port
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Solving is not free (CP-SAT has up to a ~20s time limit per scenario), so we
# solve each scenario once per process and reuse it -- restart the server to
# pick up changes to src/ or the input data.
_solve_cache: dict[str, SolveResult] = {}


def _get_solve(scenario: str) -> SolveResult:
    if scenario not in _solve_cache:
        instance = load_sample_instance()
        _solve_cache[scenario] = solve(instance, scenario=scenario)
    return _solve_cache[scenario]


def _build_schedule_context() -> str:
    """
    Plain-text summary of a real solve() run across Scenarios A, B and C --
    the ONLY source of truth handed to the chatbot. Built straight from
    SolveResult: report (feasibility/score), results (contract completion),
    activity_timeline (per-activity weeks), and the solver's own
    plain-English explanation of why the schedule looks the way it does.
    """
    instance = load_sample_instance()
    contracts = instance.contracts.set_index("contract_number")

    lines = [
        f"Planning horizon: {instance.parameters['horizon_weeks']} weeks, starting {instance.parameters['horizon_start']}.",
        "Schedule source: live solve() against the organizer's sample instance (CP-SAT hybrid engine, "
        "falls back to greedy on timeout).",
        "",
        "CONTRACTS:",
    ]
    for contract_number, c in contracts.iterrows():
        lines.append(
            f"- {contract_number}: {c['contract_description']} | priority P{c['contract_priority']} | "
            f"{c['nature_of_activity']} | planned completion {c['planned_completion_date']}"
        )

    for sc in ("A", "B", "C"):
        result = _get_solve(sc)
        lines.append(
            f"\n=== SCENARIO {sc} (feasible={result.feasible}, objective_score={result.objective_score}, "
            f"engine={result.engine_used}) ==="
        )

        lines.append("Contract completion:")
        for row in result.results.itertuples():
            lines.append(f"  {row.contract_number}: completes {row.simulated_completion_date}, overrun {row.overrun_days} days")

        lines.append("Activity timeline:")
        for row in result.activity_timeline.itertuples():
            lines.append(
                f"  {row.activity_id} (contract {row.contract_number}, {row.access_type}): "
                f"weeks {row.first_week}-{row.last_week}, {row.accesses} access-nights"
                + (f", {row.eclo_nights} ECLO" if row.eclo_nights else "")
                + (" -- LATE vs planned completion" if row.late else "")
            )

        occ_by_location: dict = {}
        for row in result.schedule_occupancy.itertuples():
            occ_by_location.setdefault(row.location_id, {}).setdefault(row.week, []).append(row.activity_id)
        lines.append("Location occupancy (location: week -> activities there that week):")
        for loc, weeks in sorted(occ_by_location.items()):
            week_strs = [f"wk{w}:{','.join(sorted(set(acts)))}" for w, acts in sorted(weeks.items())]
            lines.append(f"  {loc}: " + "; ".join(week_strs))

        summary = result.explanation.get("summary", [])
        if summary:
            lines.append("Solver's own explanation (headline points):")
            for line in summary[:15]:
                lines.append(f"  - {line}")

    return "\n".join(lines)


class ChatMessage(BaseModel):
    role: str  # "user" or "model"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


@app.post("/api/chat")
async def chat(req: ChatRequest):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return JSONResponse(
            status_code=500,
            content={"error": "GEMINI_API_KEY is not set. Copy webapp/.env.example to .env (repo root) and fill it in."},
        )

    from google import genai
    from google.genai import types

    context = _build_schedule_context()
    system_instruction = (
        "You are a planning assistant for a railway track access maintenance scheduler, used by a "
        "works controller. Answer questions using ONLY the real schedule data below -- never invent "
        "activity IDs, contract numbers, weeks, or scores that aren't in it. If the data doesn't answer "
        "the question, say so plainly. Be concise and cite exact IDs/weeks/numbers from the data. "
        "Reply in PLAIN TEXT ONLY -- no Markdown (no **, no #, no backticks). For lists, use a plain "
        "dash and a line break, nothing else.\n\n"
        f"{context}"
    )

    contents = [
        types.Content(role=("model" if m.role == "model" else "user"), parts=[types.Part(text=m.content)])
        for m in req.history
    ]
    contents.append(types.Content(role="user", parts=[types.Part(text=req.message)]))

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_instruction),
        )
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Gemini request failed: {e}"})

    return {"reply": response.text}


@app.get("/api/health")
async def health():
    return {"status": "ok", "cached_scenarios": sorted(_solve_cache.keys())}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp.app:app", host="0.0.0.0", port=8001, reload=True)
