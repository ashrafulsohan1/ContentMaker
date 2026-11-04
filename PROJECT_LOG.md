# PROJECT_LOG — ContentMaker

## Project overview
ContentMaker turns a user-provided polished script into platform-ready social media content. BYOK: the user's cloud API keys (Gemini recommended) drive TTS / image / video generation; assembly and encoding run locally via ffmpeg. Python FastAPI backend + server-rendered htmx UI (no build step), SQLite job state, background job engine with cost guardrails. Run with `uvicorn` after `pip install -r requirements.txt`; requires ffmpeg on PATH (checked by `scripts/check_env.py`). Spec of record: `PROMPT.md` (v1.2). Plan: `BUILD_PLAN.md`. Decision history: `DECISIONS.md`.

## Key decisions
- 2026-11-04 — Spec v1.2 resolved: local web app, BYOK providers, English-only, no captions/music (PROMPT.md §10).
- 2026-11-04 — Stack: Python FastAPI + light JS UI, SQLite, ffmpeg local (D5).
- 2026-11-04 — First provider key: Google Gemini API (one key = TTS + Imagen + Veo); OpenAI Sora 2 API shut down 2026-09-24 (D6).
- 2026-11-04 — Full TDD everywhere (D7).
- 2026-11-04 — No code before BUILD_PLAN.md user sign-off (spec §11 gate).

## Sessions

## Session — 2026-11-04
**Goal:** Execute PROMPT.md planning stage — clarify open decisions, draft BUILD_PLAN.md, get sign-off.

### Changes
- Asked user to resolve stack (→ FastAPI + light JS UI), provider keys (→ none yet; Gemini recommended), testing depth (→ full TDD).
- Researched one-key audio+video providers; confirmed OpenAI Sora 2 API shutdown; selected Gemini API as first key (sources in DECISIONS.md D6).
- Created `DECISIONS.md` (decision history D1–D9 + status), `PROJECT_LOG.md` (this file).
- Drafted `BUILD_PLAN.md` v1.0 covering spec §11 items 1–7.

### Decisions & notes
- BUILD_PLAN.md is a living document; step-level TDD task breakdown happens per-milestone at its start (D9), not up front.
- M1 runs fully on mocked engines (zero network), so the user having no API key yet does not block it.

### Next steps
- [ ] User sign-off on BUILD_PLAN.md (incl. formatter-hook proposal and git init in Phase 0)
- [ ] Phase 0: git init, venv, requirements, `scripts/check_env.py` (ffmpeg check), app boots
- [ ] Milestone 1: skeleton + full UI flow on mocked engines
- [ ] User: obtain Google Gemini API key before Milestone 2

### Status
In progress — awaiting BUILD_PLAN.md sign-off.
