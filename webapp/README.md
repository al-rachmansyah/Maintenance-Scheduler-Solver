# Chat Assistant (optional add-on)

A small FastAPI service exposing a `POST /api/chat` endpoint: a natural-language
Q&A assistant grounded in a real `solve()` run of the organizer's sample
instance. It does not touch or depend on any change to `src/` — it's a thin
layer on top of the existing `src/api.py` facade described in the main
[README](../README.md#3-use-it-as-the-solver-behind-a-web-app).

## Why this exists

Every answer traces back to a real, freshly-solved schedule (Scenarios A, B
and C), including the solver's own plain-English `explain.py` output — not a
canned or hallucinated response. If the data doesn't answer a question, the
model is instructed to say so rather than guess.

## Setup

1. Get a free Gemini API key (no billing setup needed):
   https://aistudio.google.com/apikey
2. Copy `.env.example` (repo root) to `.env` and fill in `GEMINI_API_KEY`.
3. From the repo root:
   ```powershell
   pip install -r requirements.txt          # the solver's own deps
   pip install -r webapp/requirements.txt   # fastapi, uvicorn, google-genai, python-dotenv
   python -m webapp.app
   ```
   Serves on `http://localhost:8001`.

## Endpoints

- `POST /api/chat` — `{"message": str, "history": [{"role": "user"|"model", "content": str}]}` → `{"reply": str}`
- `GET /api/health` — `{"status": "ok", "cached_scenarios": [...]}`

## Notes

- The first chat message triggers a `solve()` for whichever scenario hasn't
  been solved yet in this process (CP-SAT has up to a ~20s time limit per
  scenario per `src/cpsat_engine.py`); results are cached in memory after
  that. Restart the server to pick up changes to `src/` or the input data.
- CORS is open to `localhost:3000`/`5173`/`8501` (common React/Vite/Streamlit
  dev ports) — adjust `allow_origins` in `app.py` for your actual frontend.
