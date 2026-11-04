# ContentMaker — BUILD_PLAN.md

**Version:** 1.0 · **Date:** 2026-11-04 · **Implements:** PROMPT.md v1.2 · **Status:** awaiting user sign-off (spec §11 gate — no code before approval)

**Goal:** A local web app that turns a user-provided script into platform-ready social media content — per-beat TTS + visuals via the user's own cloud API keys (BYOK), assembled locally with ffmpeg into MP4/PNG deliverables.

**Architecture:** FastAPI server (background job engine on a worker thread, SQLite persistence, SSE live progress) + server-rendered htmx/vanilla-JS UI (no build step). Every engine (TTS / image / video) sits behind a provider interface with a registry; BYOK keys plug into the registry; mock providers power a zero-network dry-run mode. Assembly and encoding run locally via ffmpeg/ffprobe, driven exclusively by the timing manifest.

**Tech stack:** Python 3.11+, FastAPI, Uvicorn, httpx, Jinja2, htmx, stdlib `sqlite3`, pytest (full TDD), ruff. ffmpeg + ffprobe are named system dependencies.

**Spec:** `PROMPT.md` (v1.2). Decision history: `DECISIONS.md`. Session log: `PROJECT_LOG.md`.

---

## 0. First API key — recommendation (resolved for the user)

User has no keys yet and asked for one recommendation, preferring a single key covering audio **and** video.

**Recommendation: Google Gemini API (AI Studio key).** One key covers all three engines:
- **TTS** — Gemini TTS (voice, pace, tone control) · cheap; free tier exists for light testing
- **Images** — Imagen / Gemini Flash Image · ~$0.02–0.10 per image by variant
- **Video** — Veo 3.1 · ~$0.40/s standard, cheaper Fast/Lite variants (~$0.10–0.30/s)

**Why not OpenAI:** the Sora 2 Videos API was **shut down 2026-09-24** and gpt-image-1 is being superseded — no one-key audio+video story anymore. Replicate (many models, one key) and ElevenLabs (premium TTS) remain addable later behind the provider abstraction.

Model IDs and exact prices are **config data** (`app/data/prices.json`), verified by a research spike at the start of M2 — not hardcoded, because this market moves fast.

Sources: [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) · [OpenAI pricing](https://developers.openai.com/api/docs/pricing) · [Sora 2 shutdown report](https://costgoat.com/pricing/sora)

## 1. Architecture & file structure

### 1.1 Repo layout

```
ContentMaker/
  app/
    main.py               # FastAPI app factory; startup: env check, maintenance task, job worker
    config.py             # paths, server settings
    api/                  # routes: projects, jobs, settings(keys), assets, export; sse.py (live progress)
    core/
      keys.py             # BYOK key store: add/remove/test per provider; local file, gitignored
      cost.py             # estimates, spend log, per-job caps
      jobs.py             # job engine: lifecycle, worker thread, SQLite persistence, retries
      maintenance.py      # startup + periodic temp/orphan sweep (in-app only)
    scripting/
      parse.py            # annotation convention: blank lines, [scene: label], <!-- img: desc -->
      segment.py          # auto-segmentation: paragraph beats, sentence-split overlong paragraphs
      manifest.py         # timing manifest (§6.2 schema) — single source of truth
    engines/
      base.py             # TTSProvider / ImageProvider / VideoProvider protocols + price metadata
      registry.py         # provider registry; BYOK plugs in here
      mocks.py            # zero-network mock providers (dry-run mode)
    providers/
      gemini/             # client.py (httpx, auth, error mapping) + tts.py / image.py / video.py
    assembly/
      ffmpeg.py           # subprocess wrapper + ffprobe duration probe
      video.py            # manifest-driven stitch: H.264/AAC MP4, fps/resolution/aspect normalization
      images.py           # platform-correct PNG/JPEG export, optional text overlay
    web/
      templates/  static/ # htmx + vanilla JS; SSE client for live job progress
    data/                 # EDITABLE CONFIG (spec §5): platforms.json, presets.json, prices.json
  projects/<name>/        # per spec §7: script/ plan/ audio/ visuals/ output/ state/
  state/                  # app-level: keys.json (gitignored)
  scripts/
    check_env.py          # python ≥3.11, ffmpeg + ffprobe present, clear setup message if not
    smoke_m{1..4}.py      # per-milestone smoke checks
  tests/                  # pytest — full TDD
  .gitignore  requirements.txt  README.md
  PROMPT.md  BUILD_PLAN.md  DECISIONS.md  PROJECT_LOG.md
```

### 1.2 Key design decisions

| Topic | Decision |
|---|---|
| Job execution | **Single background worker thread** (local single-user app); jobs queue; tab close cannot kill it; every state transition persisted to `projects/<name>/state/job.db` (SQLite, WAL mode — OneDrive-safe) + `spend_log.jsonl` |
| Live progress | SSE endpoint; UI reattaches to running job on reload (§6.6) |
| Dry-run | Job flag routes every engine call to `engines/mocks.py` — zero network, zero spend, full flow incl. assembly |
| Mocks with real ffmpeg | Mock TTS = silent mp3 of requested duration, mock image = labeled PNG (ffmpeg-generated) — so **assembly is real ffmpeg from M1**, de-risking it early |
| Durations | Never trust API-reported durations; ffprobe every downloaded audio/clip; manifest written from probed values |
| Keys | `state/keys.json` (gitignored, chmod-local); sent only to the provider they belong to; per-engine default selectable when multiple providers configured |
| Cost guardrails | `prices.json` unit table → estimate before any paid batch → `awaiting_confirmation` gate → running spend per job → hard per-job cap enforced before each paid call |
| Failure model | Per-asset status machine; bounded retry with backoff; failure is skippable/regenerable without redoing paid assets; cancel keeps completed assets |
| Maintenance | In-app periodic task (startup + interval), low priority, never blocking jobs (§6.7). **No OS Task Scheduler, no external automation** — would be proposed first if ever genuinely required |
| i18n | English-only strings, but no English baked into slugs, file naming, or layout |

## 2. Global constraints (from spec — every task inherits these)

- English-only v1; UTF-8, i18n-friendly internals (§7)
- Target Windows 10/11; paths/scripts/setup must work there; Git Bash acceptable (§7)
- BYOK: keys local only, never committed, never sent anywhere except their provider (§6.5)
- Cloud AI only for generation — no local model downloads; ffmpeg local for assembly; no cloud encoding (§7)
- Per-beat TTS; timing manifest is the **single source of truth** for assembly (§6.2–6.4)
- Visual consistency is a hard requirement across all visuals in one piece (§6.3)
- Cost estimate + explicit confirmation before any paid call batch; running spend display; per-job hard cap (§6.6)
- Partial failure must not kill a job; bounded retries; per-asset regen (§6.6)
- Platform specs stored as editable config data, one table/file (§5)
- Captions, music, script-writing, auto-posting, multilingual, brand kits, offline models: **out of scope** (§9)

## 3. Build workflow (§11.1)

Backbone = spec §13 milestones. Each milestone ends **runnable with passing smoke checks**. Step-level TDD breakdowns (red-green-refactor task lists) are written at the start of each milestone (D9) — this section locks scope, files, interfaces, gates.

### Phase 0 — Environment & scaffolding
**Deliverables:** git repo initialized (`.gitignore`: keys, `projects/`, `state/`, venv, caches); venv + `requirements.txt`; `scripts/check_env.py` (ffmpeg/ffprobe/python check with clear setup message); app skeleton boots and serves a placeholder page; pytest + ruff wired.
**Gate:** `python scripts/check_env.py` passes; `uvicorn app.main:app` boots; `pytest` green; committed.

### Milestone 1 — Skeleton + full UI flow on mocked engines
| # | Task | Files (create unless noted) |
|---|------|------------------------------|
| 1.1 | Platform specs loader + data (`platforms.json` = spec §5 table: aspect, max duration, resolution, format) | `app/data/platforms.json`, `app/api/` lookup |
| 1.2 | Script parser — blank-line paragraphs, `[scene: label]` markers, `<!-- img: desc -->` hints; degrade gracefully on malformed input | `app/scripting/parse.py` |
| 1.3 | Auto-segmentation — paragraph beats; sentence-split overlong paragraphs | `app/scripting/segment.py` |
| 1.4 | Engine protocols + registry + **mock providers** (silent mp3 / labeled PNG / color clip) | `app/engines/base.py`, `registry.py`, `mocks.py` |
| 1.5 | Job engine — lifecycle `draft→estimating→awaiting_confirmation→running→completed\|partial\|failed\|cancelled`, per-asset statuses, SQLite persistence, worker thread, retry w/ backoff, cancel keeps done assets | `app/core/jobs.py` |
| 1.6 | Cost module — estimates from `prices.json`, spend log, caps (mock jobs cost 0) | `app/core/cost.py`, `app/data/prices.json` |
| 1.7 | Timing manifest read/write (exact §6.2 schema; default duration for silent beats) | `app/scripting/manifest.py` |
| 1.8 | Real ffmpeg wrapper + ffprobe (absolute paths, Windows-safe) | `app/assembly/ffmpeg.py` |
| 1.9 | Project scaffolder — creates `projects/<name>/{script,plan,audio,visuals,output,state}` | `app/core/projects.py` |
| 1.10 | UI: input → segmentation review (edit/merge/split; paginated for long scripts) → options (platform/format, presets, audio/visual choices, availability from registry) → estimate+confirm → generate | `app/web/`, `app/api/` |
| 1.11 | SSE progress + job monitor UI (per-asset status, overall %, spend, cancel, per-asset retry) | `app/api/sse.py`, `app/web/` |
| 1.12 | Assembly pipeline (real ffmpeg on mock assets) + preview/export UI; regenerate single asset | `app/assembly/video.py`, `images.py` |
| 1.13 | Maintenance task (startup + interval sweep of temp/orphans) | `app/core/maintenance.py` |

**Gate (spec §13.1):** whole flow clickable end-to-end with fake assets; `projects/` structure correct; dry-run makes zero network calls; `pytest` green; `scripts/smoke_m1.py` passes; GUI sweep by tester agent passes; review-agent findings addressed.

### Milestone 2 — BYOK settings + real TTS
| # | Task | Files |
|---|------|-------|
| 2.0 | **Research spike (delegated, read-only):** current Gemini model IDs, TTS params (voice/pace/tone), pricing table, error shapes → conclusions into `prices.json` + provider design | research notes |
| 2.1 | Settings screen — add/remove/**test** keys, engine availability from keys, per-engine default | `app/api/` settings routes, `app/core/keys.py`, UI |
| 2.2 | Gemini client — httpx, auth, error mapping to "exactly what's missing" messages (§6.5) | `app/providers/gemini/client.py` |
| 2.3 | Gemini TTS provider — per-beat synthesis, voice/pace/tone, contract tests vs recorded fixtures | `app/providers/gemini/tts.py` |
| 2.4 | ffprobe durations → timing manifest with real values | `app/scripting/manifest.py` |
| 2.5 | Confirm-before-spend flow wired to `awaiting_confirmation`; running spend; hard per-job cap enforcement | `app/core/cost.py`, `jobs.py` |

**Gate (§13.2):** keys addable/validatable in Settings; script → per-beat audio + valid manifest; estimate shown and confirmed before any spend; no network call without confirmation. *(Requires Gemini key.)*

### Milestone 3 — Image generation + local assembly/export
| # | Task | Files |
|---|------|-------|
| 3.0 | **Research spike (delegated):** Gemini image models, dimension control, **style-consistency mechanisms** (seed / reference image / style anchoring) | research notes |
| 3.1 | Gemini image provider w/ style anchoring (fixed style block + seed/reference as supported); platform-correct dimensions | `app/providers/gemini/image.py` |
| 3.2 | Image export — PNG/JPEG at platform dims; optional per-image text overlay (ffmpeg drawtext vs Pillow — decided on spike data) | `app/assembly/images.py` |
| 3.3 | Video assembly — manifest-driven concat + per-beat audio, H.264/AAC, fps/res/aspect normalization | `app/assembly/video.py` |
| 3.4 | Carousel / multi-image deliverables (IG, LinkedIn, X) | `app/assembly/`, `app/api/export` |
| 3.5 | Consistency check — 3-beat sample run, style regression reviewed visually | test fixture |

**Gate (§13.3):** platform-correct PNG sets + MP4 assembled via ffmpeg from generated images + audio, timing from manifest; visual-consistency check passes; tests green.

### Milestone 4 — Video-gen provider + polish
| # | Task | Files |
|---|------|-------|
| 4.0 | **Research spike (delegated):** Veo API (async job polling, download, per-second pricing, safety/latency) | research notes |
| 4.1 | Veo provider — async polling, download, cost model (per-second estimate) | `app/providers/gemini/video.py` |
| 4.2 | Ken Burns fallback — pan/zoom + transitions from stills (video without a video key, §6.3) | `app/assembly/video.py` |
| 4.3 | Job resume after server restart (kill-mid-job test) | `app/core/jobs.py` |
| 4.4 | Per-asset retry/regen on real providers without redoing paid assets | `app/api/`, UI |
| 4.5 | Spend-cap + confirmation polish; running-spend display accuracy | `app/core/cost.py` |
| 4.6 | README + Windows setup docs (python, ffmpeg install, steps); full verification on user machine | `README.md` |

**Gate (§13.4):** cloud video provider wired; resume, per-asset retry, spend caps, cost confirmations all work; full flow on real keys; smoke_m4 passes.

## 4. Division of workload (§11.2)

**Lead agent inline:** architecture; all shared/cross-cutting files (`core/`, `engines/`, `assembly/`, `scripting/`, app config); all integration; TDD for core logic; milestone gates. Rationale: spec requires one writer of record for shared files, and interfaces here are tightly coupled.

**Delegated (read-only or isolated outputs, with rationale):**
1. **Provider research spikes (2.0 / 3.0 / 4.0) → Explore agent.** Rationale: self-contained read-only research; parallelizes cleanly; returns conclusions, keeping lead context lean.
2. **Black-box GUI sweeps at each milestone gate → web-gui-tester agent.** Rationale: fresh-eyes testing catches what the implementer's own assumptions blind them to.
3. **Defect-first code review before each "done" claim → review-agent.** Rationale: independent review; complements TDD which proves features, not absence of defects.

**Not delegated:** implementation tasks (TDD red-green lives with the writer of record), test-writing (tests define the implementation), anything touching shared files.

## 5. Subagent & concurrency plan (§11.3)

- **Cap: 2 concurrent subagents** (3 only for genuinely parallel provider research); prefer fewer; **fall back to sequential** on any throttle/reject without stalling.
- **Never parallelize writes to the same file.** One writer of record per file/module at a time; delegated work is read-only (research, review, GUI testing) or confined to files the lead isn't touching.
- Subagents return **conclusions and short reports**, never raw dumps.
- No structured workflow runs unless the user explicitly requests one — propose first, never improvise.

## 6. Plugins & skills plan (§11.4)

| Skill / tool | Phase | Why |
|---|---|---|
| writing-plans | Each milestone start | Step-level TDD task breakdowns (D9) |
| tdd | Every task | User-mandated full TDD (D7) |
| executing-plans | M1–M4 execution | Inline execution of milestone task lists |
| verification-before-completion | Before every "done"/gate claim | Evidence before assertions |
| browser-use:web-gui-tester + control-browser | Milestone gates | Black-box GUI testing of the local web app |
| review-agent / requesting-code-review | Milestone gates | Independent defect-first review |
| project-log | Ongoing | `PROJECT_LOG.md` session continuity (spec §12) |
| diagnosing-bugs | As bugs surface | Integration failures (ffmpeg, provider APIs) |
| find-skills | Only on a capability gap | Contingency |

**Explicitly not planned:** computer-use (browser-use covers the web app), dynamic-workflows/jev (no user request), imagegen (assets come from the user's cloud providers, not local generation), cron/off-peak tools (§8 below), pdf/visualize/anet/autoresearch/github-* (out of scope; repo is local-only unless user asks).

## 7. Hooks plan (§11.5) — proposal, pending user approval at sign-off

**Proposed:** one PostToolUse hook — on every `Edit`/`Write` to a `.py` file, run `ruff format <file> && ruff check --fix <file>`. Cheap, keeps style uniform under heavy TDD churn.

**Rejected:** a hook running the test suite per edit — full suite + assembly tests are slow; multiplying that across hundreds of edits wastes far more than it catches. Test runs belong to task steps and milestone gates (verification-before-completion discipline).

If the user declines the formatter hook, that's the whole story: no hooks; ruff runs at task commits and gates instead.

## 8. Background & automation plan (§11.6)

- **Build-time:** dependency installs, dev server, ffmpeg test encodes, long pytest runs → **background tasks** in the harness, never blocking waits.
- **App-time:** generation jobs on the background worker thread + in-app periodic maintenance task (startup + interval) per §6.6–6.7.
- **Scheduled automations:** **none created** — cron-like or idle-time automations only on explicit user request (spec §11.6). Nothing in this build legitimately needs OS-level scheduling; if that changes, propose with exact schedule and purpose first.

## 9. Risk list (§11.7)

| # | Risk | De-risk |
|---|------|---------|
| 1 | **Provider API drift** (models renamed/retired — Sora 2 API shut down Sep 2026; Gemini lineup moves) | Model IDs/prices as config data; research spike re-verifies before wiring each provider; adapters isolate drift |
| 2 | **Visual consistency across beats** (hard requirement) | Style anchoring (fixed style block + seed/reference per spike findings); 3-beat consistency test early in M3; fallback: first image as reference for the rest if provider supports it |
| 3 | **ffmpeg on Windows** (paths with spaces/CJK, drawtext fonts, fps/sync drift) | Absolute paths everywhere; ffprobe-verified durations; real-ffmpeg assembly exercised from M1 (mock assets); fps normalization step |
| 4 | **TTS duration mismatch** (API-reported durations wrong) | Never trust API durations; ffprobe every file; manifest from probed values |
| 5 | **Silent spend / estimate inaccuracy** | Conservative rounding (round up), price table in config, estimate-confirm gate, running spend, hard per-job cap, dry-run default |
| 6 | **Job resilience** (restart mid-job) | State persisted per transition from M1 (resume logic tested on mocks); real kill-mid-job test in M4 |
| 7 | **OneDrive sync on project folder** (file locks on SQLite/state files) | SQLite WAL mode + lock retry; runtime scratch kept out of sync churn where possible; escalate to user (move runtime dir) if it bites |
| 8 | **UI choke on long scripts** | Paginated beat list; 100+ beat synthetic script test in M1 |
| 9 | **Full-TDD pace vs integration surfaces** (ffmpeg, real APIs) | TDD for pure logic; HTTP contract tests vs recorded fixtures; thin untestable shells kept minimal; real smoke at gates |
| 10 | **No API key yet** (user action) | M1 is fully mocked — not blocked; Gemini key needed by M2 start |

## 10. Review focus (spec-implied inputs no task's test should miss)

1. **Script edge cases** — empty input, CRLF + UTF-8 BOM, one enormous paragraph, malformed `<!-- img: -->`: parser must degrade gracefully, never crash the UI. (Pinned by 1.2 tests)
2. **Beat with no audio** in a video flow — gets a sensible default duration recorded in the manifest. (Pinned by 1.7 tests)
3. **Key invalid or quota-exhausted mid-job** — per-asset clear failure message naming what's missing, retry/skip path, job continues. (Pinned by 2.2/2.3 tests)
4. **Cap exceeded mid-job after estimate approval** — paid calls stop, completed assets kept, spend displayed, job marked partial. (Pinned by 1.5/1.6 tests)
5. **Tab closed / server restarted mid-job** — job keeps running or resumes; reopening reattaches to live status. (Pinned by 1.5 mocked, 4.3 real)

## 11. At sign-off

1. Update `DECISIONS.md` status; log approval in `PROJECT_LOG.md`.
2. Phase 0 → M1, inline (executing-plans + tdd), background tasks for long commands, verification-before-completion at every claim, review-agent + GUI sweep at each gate.
3. `BUILD_PLAN.md` is living — deviations recorded here and in `DECISIONS.md` with dates.
