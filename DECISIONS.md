# DECISIONS.md — ContentMaker

Canonical, dated decision history. PROMPT.md is the spec of record; resolved decisions (§10 of the spec) are re-listed here and must not be re-litigated.

## Resolved decisions

| # | Date | Decision | Rationale |
|---|------|----------|-----------|
| D1 | 2026-11-04 | **Local web app** (local server + browser UI) as UI form | Spec §10.1 |
| D2 | 2026-11-04 | **BYOK provider model** — user supplies API keys; configured keys determine available engines; provider abstraction layer mandatory | Spec §10.2 |
| D3 | 2026-11-04 | **English-only in v1**, internals i18n-friendly (UTF-8, no English assumptions in layout/strings/filenames) | Spec §10.3 |
| D4 | 2026-11-04 | **No captions/subtitles and no background music in v1** | Spec §10.4 |
| D5 | 2026-11-04 | **Stack: Python 3.11+ / FastAPI / Uvicorn; server-rendered HTML + htmx/vanilla-JS UI (no build step); SQLite (stdlib) for job state; ffmpeg/ffprobe as named system dependencies** | User chose "Python FastAPI + light JS UI" from offered options; fewest Windows install headaches, no frontend toolchain, simple ffmpeg subprocess ergonomics |
| D6 | 2026-11-04 | **First provider key: Google Gemini API (AI Studio).** One key covers all three engines: Gemini TTS, Imagen (images), Veo (video). OpenAI rejected as primary because the Sora 2 Videos API was shut down 2026-09-24. Replicate / ElevenLabs / OpenAI remain addable later via the provider abstraction | User had no keys and asked for one recommendation, preferring one key covering audio + video. Pricing sources: [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [Sora 2 shutdown](https://costgoat.com/pricing/sora) |
| D7 | 2026-11-04 | **Full TDD everywhere** — test-first for all features, including HTTP provider adapters (contract tests against recorded fixtures) and UI plumbing (API-level tests); real-provider smoke tests at milestone gates | User chose "Full TDD everywhere" from offered options |
| D8 | 2026-11-04 | **Planning docs live in project root** — `BUILD_PLAN.md` (approved planning artifact, per spec §11) instead of the writing-plans skill default `docs/superpowers/plans/` location; `PROJECT_LOG.md` for session continuity alongside this file | Spec §11/§12 mandate project-root files; spec location preference overrides skill default |
| D9 | 2026-11-04 | **Step-level TDD task breakdowns are produced at the start of each milestone**, not all up front. BUILD_PLAN.md locks scope, file structure, interfaces, and verification gates per milestone; the fine-grained red-green-refactor task list for the active milestone is written then (via tdd/writing-plans skills) | Keeps BUILD_PLAN.md reviewable; avoids a mile-long plan that predates reality |

| D10 | 2026-11-04 | **Dynamic workflow drives the build to release.** User explicitly requested a manager/worker workflow that continues without stopping until the product is finished, tested, and released via GitHub, asking (not stalling) when user input is needed. Release = GitHub repo + v1.0.0 release; repo visibility to be confirmed with the user at release time (private recommended). Amends BUILD_PLAN §5's "only on explicit request" — this is that explicit request | User message 2026-11-04; run `dwfrun-27f8f31f-fe7d-48a3-a0e4-0c45e2f414fd` |

## Pending decisions

- **Hooks approval** — formatter-on-edit hook proposed in BUILD_PLAN.md §7; awaiting user decision at plan sign-off.
- **git init** — Phase 0 proposes initializing a git repo (keys/outputs gitignored); user can veto at sign-off.

## Current status (2026-11-04)

- **Done:** PROMPT.md v1.2 spec; stack, provider, and testing decisions; BUILD_PLAN.md v1.0 drafted (incl. subagent/concurrency + background/automation plans per §11).
- **In progress:** awaiting user sign-off on BUILD_PLAN.md (spec §11 gate — no code before approval).
- **Next:** on approval → Phase 0 (env/scaffolding) then Milestone 1 (skeleton + full UI flow on mocked engines). User action item: obtain a Google Gemini API key before Milestone 2 starts (not needed for M1, which runs fully mocked).
