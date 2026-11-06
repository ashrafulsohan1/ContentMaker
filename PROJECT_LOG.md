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
Paused by user token quota — run healthy at last check; restart protocol above. (Superseded: the run resumed and executed M2 through M4 to completion — see next session.)

## Session — 2026-11-06 → 2026-11-07
**Goal:** Resume `dwfrun-b1dd41e3` from cache and drive the build to release — M2 remainder → M3 → M4 → v1.0.0 release prep.

### Changes
- **M2 completed:** Gemini TTS provider wired + ffprobe-probed manifest durations (`bb14485`); confirm-before-spend + hard caps pinned end-to-end on the real provider (`b4cd1cc`); smoke_m2 gate (`999005c`); review hardening — transport-proof key probe, no-JS error banner, pre-swap key-store chmod, loud WAV-splice refusal (`5a118d6`).
- **M3 completed:** image research spike (`c7b3890`); Gemini image provider with style anchoring (`5cdbb4f`); ffmpeg drawtext overlay + D13 ruling (`916470b`); assembly hardening with real-asset pins (`a38544a`); carousel min/max contract + export ZIP bundle, D14 (`441818f`); smoke_m3 gate (`91ef994`); review fixes (`3e09ccd`).
- **M4 completed:** Veo spike (`e325dc7`); Gemini video provider — two-level async polling, per-second pricing, prices.json flipped to `verified: true` (`ac11a1c`); Ken Burns stills→video fallback, reconciling WIP `9bceebb` (`315a8b1`); real process-kill resume proof (`129a489`); per-asset retry JSON API (`698e7ed`); spend guardrail polish (`bf3e2a1`); README (`03b3419`); smoke_m4 gate (`8e3c4a3`); review fixes (`a4fcef1`); UI sweep ui-f1..f3 (`3b3c689`).
- **Release prep (2026-11-07):** README completeness pass for a new user (platform list corrected to the seven shipped platforms — Facebook/Instagram/LinkedIn/Pinterest/TikTok/X/YouTube; Ken Burns fallback documented); DECISIONS.md + PROJECT_LOG.md status refresh; annotated tag `v1.0.0` cut on the release-prep commit. Nothing pushed.
- Suite at **638 passed + 1 skipped** (pre-existing Windows chmod skip), ruff clean.

### Decisions & notes
- WIP `9bceebb` was reconciled into task 4.2 as planned (`315a8b1` marks the task boundary).
- Ken Burns composition means an MP4 deliverable needs no video-capable key; the Veo ~3s-clip limitation stays documented in README §5 as a design deferral.

### Next steps
- [ ] Merge `milestone/1-skeleton` → `main` at release (main sits at Phase 0, 49 commits behind)
- [ ] GitHub release: confirm repo name + visibility with the user (private recommended); push branch + tag
- [ ] Real-provider smokes once the user adds a Gemini key (`state/` holds no `keys.json` as of 2026-11-07)

### Status
**RELEASED 2026-11-07 — ContentMaker v1.0.0**: https://github.com/0xSpecters/ContentMaker/releases/tag/v1.0.0 (repo `0xSpecters/ContentMaker`, private; branch history pushed as `main`, tag at `57c8059`). All 20 tasks gated; smokes 14/14 + m2/m3/m4 exit 0; UI sweep performed, medium/high fixed and re-gated; suite **638 passed + 1 skipped**. **Owner backlog — 6 low findings declined by fixers as product-scope (all fact-verified):** m2-f3 options UI lacks per-engine provider selector (Settings defaults + confirm-page disclosure are the §6.5 mechanism); m3-f2/ui-f5 carousel ZIP bundle has no UI affordance (API `GET /api/jobs/{id}/export` works); m4-f2 video clips planned at flat 3s vs probed beat length (documented limitation in `flow.py:450` + README §5; honest fix = two-phase estimation); ui-f4 no-JS monitor notices drop; ui-f6 repeated POST /options leaves unconfirmed draft jobs. Remaining user actions: Gemini key for real-provider smokes; flip visibility to public if wanted.

## Session — 2026-11-07 (v1.0.1 packaging)
**Goal:** Implement the v1.0.1 packaging feature set (user: "go with the recommended option") — a packaged Windows exe — TDD-style, on top of the released v1.0.0.

### Changes
- `app/config.py` — frozen-aware writable base: new `app_home()` (CONTENTMAKER_HOME env → exe dir when `sys.frozen` → repo root, dev byte-identical) + `is_frozen()`; `projects_root()`/`app_state_dir()` default through it; bundled read-only content keeps resolving inside `_MEIPASS`.
- `app/core/binaries.py` (new) — 3-tier ffmpeg/ffprobe resolver: `CONTENTMAKER_FFMPEG_DIR` → bundled `sys._MEIPASS/ffmpeg_bin` → PATH; loud `PackagingError` naming all three tiers; `required=False` variant backs `/healthz`.
- `app/assembly/ffmpeg.py` — `FFmpeg` defaults resolve through the resolver (explicit bins still win); `app/engines/mocks.py` — mock video provider resolves its binary the same way (dry-run works in the exe).
- `app/main.py` — `GET /healthz` returning `{ffmpeg, ffprobe, frozen}` (nulls when unresolvable).
- `app/launcher.py` (new) — exe/dev entry: `--port` (default 8765) / `--no-browser`, browser opens after uvicorn binds (delayed thread), clean Ctrl+C.
- `scripts/build_exe.py` (new) + checked-in `ContentMaker.spec` — one-file `dist/ContentMaker.exe`; `--bundle-ffmpeg` stages resolved binaries into `build_staging/ffmpeg_bin` (spec includes it only when staged; plain build clears stale staging); `dist/`/`build/`/`build_staging/` gitignored.
- Docs: README §10 Packaging (build command, exe usage, where files live, 3-tier lookup, size + GPL licensing note for bundled gyan.dev builds), BUILD_PLAN §12 addendum (P1–P6), DECISIONS.md D15, this log.
- TDD: red tests first (6 config failures + 4 import errors), then green; tests cover frozen base-dir resolution, CONTENTMAKER_HOME, resolver tier order + PackagingError, launcher args/behavior, build-script args/staging — no builds inside tests.

### Decisions & notes
- D15 recorded (packaging decision incl. GPL stance for bundled ffmpeg). PyInstaller was NOT in the venv at implementation time — build verification status recorded honestly in the ledger.

### Status
Code complete on `milestone/1-skeleton`; suite green + ruff clean at commit; real `--bundle-ffmpeg` build + exe smoke owned by the packaging run's verification step.
