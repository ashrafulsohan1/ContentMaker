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
In progress — awaiting BUILD_PLAN.md sign-off. (Superseded 2026-11-05: approved via user's "continue"; see next session.)

## Session — 2026-11-05
**Goal:** Autonomous workflow build (user-requested, D10) from M1 task 1.4 through GitHub release; survive daily quota pauses without losing work.

### Changes
- Dynamic workflow executed M1 tasks 1.4–1.13 end-to-end: every task TDD'd, gated (full pytest + ruff after each), committed on `milestone/1-skeleton` — commits `2b9c403`, `9de2887`, `808c7ab`, `2e3e935`, `47494b6`, `43cec03`, `8ebcfa8`, `905a3a7`, `bca8e47`; suite ~250 tests green, ruff clean.
- Run `dwfrun-27f8f31f-fe7d-48a3-a0e4-0c45e2f414fd`: stopped once on provider quota (code 1005) → resumed; then errored on the milestone-1 smoke gate (`world.run` 5-min timeout). Amended in place (smoke timeout → 30 min; recorded gate calls untouched so they replay from cache) → successor run `dwfrun-98de313d-2572-4559-acd6-0be1a550924b` continues from the milestone-1 smoke gate.
- Recorded D10 in `DECISIONS.md`; live progress board artifact exists in the run UI.

### Decisions & notes
- Quota pauses manifest as workflow `provider` stops — resumable with `ResumeWorkflowRun`; script errors need `AmendWorkflow` from `.zcode/workflow-drafts/Take-stock-of-the-build.dwf.ts`. Never rebuild from scratch: the journal cache holds ~55M tokens of finished work.
- Cache rule (learned the hard way): never change the opts of `world.run` calls that precede the failure point — records replay by call identity, and a changed opts cascades live re-runs of everything downstream.
- Workers honored the honesty persona: one worker explicitly declined to sweep the orchestrator's uncommitted DECISIONS.md edit into its task commit.

### Next steps
- [ ] When tokens are available again: `ResumeWorkflowRun("dwfrun-98de313d-2572-4559-acd6-0be1a550924b")` if it paused on quota
- [ ] Milestone-1 gate chain: smoke_m1 → independent review → browser sweep
- [ ] M2–M4 execution; user action: obtain Gemini API key before real-provider smoke tests (offline contract tests cover the code until then)
- [ ] At GitHub release: confirm repo name + visibility with user (private recommended)

### Status
In progress — run `dwfrun-98de313d` live; will pause automatically when the user's daily tokens exhaust. (Superseded 2026-11-05: workflow parked per D12 — see below.)

### Update — 2026-11-05 (mode change)
- User mandated D11 (granularity/loop/context rules) and D12 (**no spawned subagents** — one agent per project). Token analysis recorded in DECISIONS.md: 55.5M tokens, 75% burned by three long subagent sessions (per-tool-call full-context re-sends).
- Workflow `dwfrun-98de313d` PARKED at M1 review step (quota stop). Build continues inline by the session agent.
- **M1 verified end-to-end inline:** 300 tests passed (138s), ruff clean, smoke gate 14/14 PASS (zero-network socket guard; H.264 1080×1920+AAC deliverable matches manifest; 1080×1080 JPEGs; state audit).
- gh CLI authenticated (0xSpecters) — release unblocked pending repo name/visibility.

### Next steps (inline mode)
- [ ] Targeted M1 self-review (risk files: jobs.py, cost.py) — subagent review unavailable per D12
- [ ] M2 inline: research spike → settings/keys → Gemini client/TTS (offline contract tests)
- [ ] Browser sweep deferred (token economy) — revisit at release polish

(Superseded 2026-11-05: D12 lifted by user — build resumes only via the dynamic workflow.)

## Session — 2026-11-06
**Goal:** Keep the workflow build running across daily token pauses; audit and harden the workflow script after two cache-frontier incidents and one gate timeout.

### Changes
- M1 fully closed: independent review found 5 defects (fixed TDD-style, commits `3457371`, `1e8d8b3`); M2 progressed through spike (`3c6eb29`), BYOK settings (`717a9ce`), Gemini client (`c2be757`); suite grew to **455 tests**.
- Fresh-eyes audit of the workflow script: 19 findings (3 high, 8 medium, 8 low) — all adopted via amendment or documented as deviations. High: unguarded `gh` spawn, missing gate timeouts (the 455-test/387s suite exceeded the 300s default and killed a run), unguarded ask results.
- `alreadyDone` semantics finalized: skip entirely (no ask, no gate) for 1.5–2.2 — their proof is deterministic (455-test suite, smoke 14/14, per-task gates in prior journals). `spike:true` read-only variant for research tasks. Release phase: deterministic keys gate, current-branch verification, tag reuse.
- Uncommitted edits from an interrupted worker session preserved as WIP commit `9bceebb` before this pause.

### Decisions & notes
- **Restart protocol (prevents incident recurrence):** quota pause (`provider` stop) → `ResumeWorkflowRun("dwfrun-b1dd41e3-4a24-4e2c-8e16-5e01ade7c8c1")` — never CreateWorkflow, never amend on a clean pause. Errored → edit `.zcode/workflow-drafts/Take-stock-of-the-build.dwf.ts` + `AmendWorkflow(path)` under the cache rules: no persona/ask-byte changes for tasks with chain records; no live `world.run`/workspace write before the cached prefix; `alreadyDone` flips only when the task has no chain record. After any amend: verify `cached_steps > 0`.
- "No agent working" during a gate is normal — deterministic `world.run` compute with frozen token spend.

### Next steps
- [ ] On restart: `ResumeWorkflowRun("dwfrun-b1dd41e3-4a24-4e2c-8e16-5e01ade7c8c1")`
- [ ] worker-2.3 reconciles WIP `9bceebb` + partial TTS work, then 2.4 → M2 gate → M3 → M4 → release
- [ ] prices.json verified:false flip check after 2.3; merge milestone/1-skeleton → main at M2 completion
- [ ] Release captain escalates for repo name/visibility (private recommended); user Gemini key for real-provider smokes

### Status
Paused by user token quota — run healthy at last check; restart protocol above.
