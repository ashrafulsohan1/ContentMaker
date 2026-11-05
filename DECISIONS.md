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

| D11 | 2026-11-05 | **Operating rules (user-mandated):** (1) task granularity — as many tasks as needed, none so small they don't make sense alone; (2) loop discipline — ≤10 loops per task unless mandatory, validate each loop's necessity before running it; (3) context economy — after a first pass, prefer compact/handoff over re-sending the whole session, reasoning compact-vs-full each time; durable on-disk state (DECISIONS/PROJECT_LOG/ledger) makes compaction safe | User message 2026-11-05 |
| D12 | 2026-11-05 | **Single-agent rule: no spawned subagents** for this project (user works other projects concurrently). Workflow runs are parked, not deleted: `dwfrun-98de313d-2572-4559-acd6-0be1a550924b` stopped at the M1 review step on quota; resume **only** if the user reverses this rule. Build continues inline (executing-plans style) by the one session agent; reviews become disclosed self-reviews until subagents are allowed again | User message 2026-11-05 |
| D12-r | 2026-11-05 | **D12 lifted by user** with the mandate: never resume inline — the build resumes **only via the dynamic workflow**. Executed as AmendWorkflow on the parked run (imports ~55M-token cache) with D11 token-economy rules added to worker/reviewer personas; the parked M1 reviewer restarts fresh | User message 2026-11-05 |
| D13 | 2026-11-06 | **Image text overlay stays on ffmpeg `drawtext`; Pillow rejected** (task 3.2 ruling). 3.0 spike data: overlay text must be drawn locally, never baked into the generation prompt (keeps text pixel-exact, editable, $0 to change); "either local tool works". Windows font probe (Pillow 13.0.0, this machine): `ImageFont.truetype("arial.ttf", 64)` resolves via bare name to `C:\Windows\fonts\arial.ttf` and `load_default(size=64)` serves a scalable bundled font — Pillow's font handling is fine, so the ruling rests on cost, not capability: drawtext needs zero new dependencies (ffmpeg is already a named system dep, D5), keeps scale/pad/JPEG export in one ffmpeg pass (§1.2 all media I/O via FFmpeg), passes user text via a UTF-8 `textfile=` sidecar so it can never break filter escaping, and has been real-ffmpeg-pinned since 1.12. Pillow's one nicety (bare-name font lookup) can be added to `find_font()` later without the dependency | research/gemini-image.md §3/§4.4 + probe 2026-11-06 |

## Token-consumption analysis (2026-11-05, recorded per user request)

- **Measured:** 55,491,991 tokens over 18h37m of workflow runtime (≈830 tok/s average, higher bursts). Per-session breakdown: worker-1.12 ≈25.2M, worker-1.11 ≈9.7M, worker-1.10 ≈6.8M — three sessions ≈75% of the total.
- **Root cause:** every subagent tool call re-sends that subagent's entire conversation; a large task (1.12 = assembly + export + UI + 50 tests) means hundreds of calls against a growing context — compounding cost. Secondary: workers ran the full suite 3–5× per task for flake "stability theater".
- **Fixes applied:** subagent swarms retired (D12 — the dominant cause); inline agent relies on automatic context compaction backed by durable on-disk state; targeted reviews of risk files instead of whole-diff reads; max 2 full-suite runs per task; batched tool calls; oversized tasks split only where a session would otherwise balloon (per D11.1).

## Pending decisions

- **Hooks approval** — formatter-on-edit hook proposed in BUILD_PLAN.md §7; awaiting user decision at plan sign-off.
- **git init** — Phase 0 proposes initializing a git repo (keys/outputs gitignored); user can veto at sign-off.

## Current status (2026-11-05)

- **Done:** Phase 0 + **all of Milestone 1** (tasks 1.1–1.13) — committed on `milestone/1-skeleton`, ~250 tests green, ruff clean. M1 smoke gate fixed to a 30-min timeout after a 5-min `world.run` timeout killed the first attempt.
- **In progress:** dynamic workflow run `dwfrun-98de313d-2572-4559-acd6-0be1a550924b` (successor of `dwfrun-27f8f31f-fe7d-48a3-a0e4-0c45e2f414fd`) executing milestone-1 gate → M2 → M3 → M4 → GitHub release, per D10. It pauses automatically when the user's daily tokens exhaust.
- **Pause/recovery protocol:** stopped(`provider`) → `ResumeWorkflowRun("dwfrun-98de313d-2572-4559-acd6-0be1a550924b")`; errored → edit `.zcode/workflow-drafts/Take-stock-of-the-build.dwf.ts` and `AmendWorkflow(path)` — never re-create from scratch (the journal cache replays ~55M tokens of finished work at zero cost). Detailed task history: `.superpowers/sdd/BUILD_PLAN/progress.md` ledger.
- **Next:** M2 research spike + Gemini client/TTS (offline contract tests; real-provider smoke needs the user's Gemini key, still not obtained). GitHub release step confirms repo name/visibility with the user (private recommended).
