# ContentMaker — Project Build Prompt (v1.2)

## 1. Mission

Build **ContentMaker**: an AI-powered agent that turns a user-provided script into finished, ready-to-post content for major social media platforms. It generates the required **audio** (voiceover) and/or **visuals** (images or video) using **cloud-based AI APIs**, then assembles everything into platform-ready deliverables — shipped as a **local web app**.

## 2. Glossary (terms used throughout this spec)

- **Script** — the polished text the user provides. Input, never output.
- **Beat / Scene** — the smallest unit of the script that maps to one visual and one audio segment. A script is segmented into beats (auto-segmented or user-annotated).
- **Timing manifest** — a JSON file produced during generation that records, per beat: its text, audio file, start time, and duration. It is the single source of truth for video assembly.
- **Engine** — a capability class: TTS, image generation, video generation.
- **Provider** — a concrete cloud service implementing an engine (e.g., some TTS API). Selected via the user's own API keys.
- **BYOK** — "bring your own key." The user supplies cloud API keys; configured keys determine which providers/engines are available.
- **Preset** — a named bundle of style options (visual style, voice settings) selectable in the UI.
- **Job** — one end-to-end generation run (script → assets → assembled deliverables), executed in the background.
- **Asset** — a single generated artifact (one scene's audio, one image, one video clip).
- **Deliverable** — a final, upload-ready file in `output/` (MP4 video or platform-sized image set).

## 3. Core concept

- The user supplies a **polished script**. ContentMaker does *not* write or rewrite the script (in this version) — it consumes it.
- ContentMaker is **BYOK**: the user adds their own cloud API keys in a settings screen. The keys configured determine which engines (TTS, image, video) are available — nothing is spendable without them.
- Through ContentMaker's UI, the user chooses: target platform and format, style/theme options (audio voice & tone, visual style), whether audio is needed, and visual type (images, video, or both).
- ContentMaker generates each required asset via the configured cloud AI services and produces final output files, organized for easy upload.

## 4. Core user flow

1. **First run:** user opens the local web app and adds API keys in Settings. ContentMaker validates each key and enables the corresponding engines.
2. User pastes (or uploads) the script.
3. User selects target platform and format (e.g., YouTube Short 9:16, Instagram carousel, LinkedIn image post).
4. User picks style options from available presets — options shown reflect the engines enabled by configured keys.
5. User confirms settings and starts the generation job. ContentMaker shows a **cost estimate for the paid API calls before it spends anything**, and the user confirms.
6. The job runs in the background: per-beat TTS (if needed) → per-beat visuals → local assembly. The user watches live per-asset progress and can keep using the app or close the tab.
7. User previews the result, regenerates individual assets if desired, and exports the final files.

## 5. Supported platforms (target list for v1)

| Platform | Formats to support |
|---|---|
| YouTube | Long-form 16:9 video, Shorts 9:16 |
| TikTok | 9:16 video |
| Instagram | Reels 9:16, feed posts 1:1 / 4:5, carousels, Stories |
| X / Twitter | Video, single & multi-image posts |
| Facebook | Video, image posts |
| LinkedIn | Video, image posts, carousels |
| Pinterest | Standard pins 2:3 |

Each platform entry carries its constraints: aspect ratio, max duration, recommended resolution, file format. Store platform specs as **configuration data** (one editable table/file), not scattered hardcoded values — this list will grow.

## 6. Functional requirements

### 6.1 Script input
- Accept pasted text or uploaded plain-text/Markdown files.
- Define a lightweight, documented annotation convention for structure, e.g.:
  - Blank-line-separated paragraphs = default beat boundaries.
  - Optional explicit markers: `[scene: short-label]` on its own line starts a named beat.
  - `<!-- img: description -->` comments (optional) let the user hint at what a beat's visual should show; ignored if absent.
- When the user doesn't annotate, **auto-segment** into beats by paragraph, splitting overlong paragraphs by sentence. Show the resulting beat list in the UI for review/editing before generation starts.
- Long scripts must be handled without choking the UI (virtualized lists or pagination if needed).

### 6.2 Audio generation (conditional)
- Triggered only when the selected content type requires audio.
- TTS via the configured cloud provider, with user-selectable voice, speaking pace, and tone exposed in the UI.
- **TTS is generated per beat** (not one whole-take file) so every beat gets an exact audio duration.
- Output: clean audio files (MP3/WAV) per beat, plus a **timing manifest** — the assembly source of truth:
  ```json
  {
    "job_id": "job_20261104_093012",
    "fps_target": 30,
    "total_duration_s": 41.8,
    "beats": [
      { "index": 1, "label": "hook", "text": "…", "audio_file": "audio/beat_01.mp3", "start_s": 0.0, "duration_s": 4.2 },
      { "index": 2, "label": "problem", "text": "…", "audio_file": "audio/beat_02.mp3", "start_s": 4.2, "duration_s": 9.7 }
    ]
  }
  ```

### 6.3 Visual generation (conditional)
- **Images:** generated via the configured cloud image API, with style presets (photorealistic, illustration, 3D render, minimal/typographic, etc.) at platform-correct dimensions.
- **Video:** generated via the configured cloud video API, or composed from generated images with motion (pan/zoom) and transitions.
- **Visual consistency is a hard requirement:** all visuals within one piece must share a coherent style (and character appearance, where applicable). Use style anchoring, reference images, seeds, or whatever mechanism the chosen provider supports — independent, uncoordinated generation calls are not acceptable output.
- Each beat's visual duration and ordering are driven by the timing manifest (§6.2). If a beat has no audio, assign it a sensible default duration and record it in the manifest.

### 6.4 Assembly & export
- **Video content:** stitch beat visuals + per-beat audio into the final video with the correct aspect ratio, resolution, and frame rate. Timing comes exclusively from the timing manifest.
- **Image content:** export at platform-correct dimensions; optional text overlay per image.
- Export formats: MP4 (H.264/AAC) for video; PNG/JPEG for images. One deliverable per selected platform format.
- Captions/subtitles and background music are **out of scope for v1** (§9).

### 6.5 Settings & key management (BYOK)
- Settings screen to add, remove, and **test/validate** API keys per provider.
- Keys are stored locally only (local config store / env file), never committed to version control, never sent anywhere except the provider they belong to.
- The set of configured keys determines which engines appear as available in the UI. When multiple providers serve the same engine, the user selects the default.
- If a job requests an engine with no configured key, the UI explains exactly what's missing instead of failing cryptically.

### 6.6 Job system & spend guardrails (background processing)
- Generation runs as a **background job**, decoupled from the web request/UI thread. Closing the browser tab must not kill it; reopening must reattach to it.
- Job state is **persisted to disk** (SQLite or JSON state files under the project's `state/` folder) so a server restart or page refresh resumes rather than loses the job.
- Job lifecycle: `draft → estimating → awaiting_confirmation → running → (completed | partial | failed | cancelled)`. Each asset inside a job has its own status: `pending | generating | done | failed | skipped`.
- **Partial failure must not kill the job:** a failed beat image or TTS segment is retried (bounded retries with backoff), then skippable/regenerable individually, without redoing already-paid assets.
- **Cost guardrails:** before any paid call batch, show a cost estimate and require explicit confirmation; track and display running spend per job; support a hard per-job spend cap the user can set.
- The user can cancel a running job; already-completed assets are kept.

### 6.7 Maintenance tasks (background automation, in-app)
- Routine housekeeping — cleaning temp/scratch files, sweeping orphaned job artifacts from cancelled runs — must run as a low-priority scheduled task inside the app (e.g., on startup plus a periodic interval), never blocking generation jobs.
- If OS-level scheduling (Windows Task Scheduler) or any external automation turns out to be genuinely required, propose it to the user with exact purpose and schedule before implementing — do not silently install background automation on the user's machine.

## 7. Architecture & technical guidance

- **Generation engines are cloud AI APIs** (user's own keys, §6.5) — no local AI model downloads.
- **Local vs. cloud split:** generation happens in the cloud; **assembly, stitching, and encoding run locally via ffmpeg**. Treat ffmpeg as a named system dependency — verify presence at startup with a clear setup message if missing. No cloud encoding APIs.
- **Provider abstraction layer:** every engine sits behind an internal interface with a provider registry. Adding a provider = implementing the interface + registering it; nothing else changes. BYOK keys plug into this registry.
- **Language:** English-only in v1. Keep all text handling UTF-8 and i18n-friendly (no English assumptions baked into layout, string handling, or file naming) so multilingual support can be added later.
- **Environment:** target machine is **Windows 10/11**. Paths, scripts, and setup instructions must work on Windows (Git Bash acceptable). The app is a local web server + browser UI.
- **Project/output layout:**
  ```
  projects/<project-name>/
    script/      # original + annotated script versions
    plan/        # beat segmentation + timing manifest
    audio/       # per-beat TTS files
    visuals/     # per-beat generated images / video clips
    output/      # final platform deliverables
    state/       # job state, spend log (internal, machine-readable)
  ```
- **Cost awareness:** estimates before paid calls, per-job spend totals (§6.6), and a preview/dry-run mode using mocked engines (no network, no spend) that exercises the full flow.

## 8. UI requirements

- Local web app with a clean, guided interface covering the full flow: input → options → generate → preview → export.
- **Settings screen** for BYOK key management (§6.5): add/remove/test keys, see enabled engines, pick defaults when multiple providers serve one engine.
- **Beat review step:** after segmentation, the user sees the beat list and can merge/split/edit before generating.
- **Job monitor view:** per-asset live status, overall progress, running spend, cancel button, and per-asset retry after partial failure.
- Style options presented as selectable presets, with advanced overrides available.
- Preview before export; regenerate individual assets (one image, one TTS segment) without redoing the whole job.

## 9. Explicit non-goals for this version (v1)

- Writing, editing, or improving the script itself (the user provides a polished script).
- Captions/subtitles and background music — deferred to a later version.
- Multilingual support — English only in v1 (but keep i18n-friendly, §7).
- Auto-posting or scheduling to social platforms — this version exports files only.
- Team accounts, collaboration, or analytics.
- Offline/local AI models.
- Brand kit support (saved logo/colors/fonts) — deferred.

## 10. Resolved decisions (do not re-litigate)

1. **UI form:** local web app (local server + browser UI). Resolved 2026-11-04.
2. **Provider model:** BYOK — user provides API keys; configured keys determine available engines. No hardcoded provider requirement; abstraction layer required. Resolved 2026-11-04.
3. **Languages:** English-only for v1, i18n-friendly internals. Resolved 2026-11-04.
4. **Captions & music:** neither in v1; deferred. Resolved 2026-11-04.

## 11. Planning-stage requirements (complete BEFORE writing code)

Before any implementation, produce a written **`BUILD_PLAN.md`** in the project root and get the user's sign-off on it. The plan must cover, explicitly:

1. **Build workflow** — the phases of work (planning → skeleton → engine wiring → assembly → polish), what each phase delivers, and the verification gate that must pass before moving on. Keep the milestone structure from §13 as the backbone and refine it into concrete task lists.
2. **Division of workload** — what the lead agent does inline (architecture, core code, integration, anything touching shared files) versus what is delegated to subagents (research spikes such as provider API docs, UI test sweeps, code review, independent module drafting). Give a one-line rationale per delegation decision — delegate for good reasons, not by default.
3. **Subagent & concurrency plan** — the host enforces a practical **subagent concurrency limit**; assume it is low. Rules:
   - Cap parallel subagents at **2–3 at most**; prefer fewer. If the host throttles or rejects spawns, fall back to sequential execution without stalling.
   - **Never parallelize write access to the same files.** One writer of record per file/module at a time; parallel agents only for read-only work (research, review, testing).
   - Have subagents return conclusions and short reports, not raw dumps, to keep context lean.
   - If heavier fan-out/parallel orchestration ever seems needed, remember that structured workflow runs require the user's explicit request — propose it, don't improvise it.
4. **Plugins & skills plan** — inventory the plugins and skills actually available in the working environment and state which will be used, at which phase, and why. Expected candidates (verify against the environment): browser-automation tooling for black-box GUI testing of the web app; TDD skill if test-first is chosen; verification-before-completion before any "done" claim; project-log for session continuity; find-skills if a capability gap appears. Do not list tools for show — unused tools stay unplanned.
5. **Hooks plan** — decide whether hooks add value (e.g., a post-edit hook running lint/format, a pre-completion hook running the test suite) and propose concrete hook definitions during planning. If hooks aren't justified for this build, say why in one line rather than skipping the topic.
6. **Background & automation plan** —
   - **Build-time:** long-running commands (dependency installs, test suites, dev servers, ffmpeg encodes during testing) run as **background tasks**, not blocking waits.
   - **App-time:** generation jobs and maintenance tasks follow §6.6–6.7.
   - **Scheduled automations** (cron-like recurring work) and idle-time deferred tasks are created **only when the user explicitly asks** for scheduled/off-peak execution — never proactively. If the build genuinely needs one, propose it to the user first with exact schedule and purpose.
7. **Risk list** — the plan must name the top technical risks (e.g., provider API quirks, ffmpeg edge cases, consistency across beats) and how each will be de-risked early.

`BUILD_PLAN.md` is a living document: update it as reality diverges, and record significant deviations in `DECISIONS.md` with dates.

## 12. Cross-chat continuity

- **PROMPT.md is the spec of record.** Maintain **`DECISIONS.md`** in the project root: append every decision made during the build (dated), plus a short current-status note (what's done, what's in progress, what's next) so a future chat in this project can resume cleanly without re-deciding resolved questions or re-asking §10.
- If the environment supports a project-log mechanism, use it for session records; `DECISIONS.md` remains the canonical decision history either way.

## 13. How to work on this (instructions for the building agent)

- Plan first per §11 — no implementation before `BUILD_PLAN.md` is approved by the user.
- Build a clean, extensible foundation; avoid hard-coded assumptions about providers, formats, or the platform list.
- Ask clarifying questions when a requirement is ambiguous rather than guessing on costly or hard-to-reverse choices.
- Deliver in milestones, each ending **runnable with smoke checks that pass** — not just code:
  1. **Skeleton + full UI flow on mocked engines** — done when the whole flow (input → options → generate → preview → export) is clickable end-to-end with fake assets, the `projects/` folder structure is created correctly, and the dry-run mode works with zero network calls.
  2. **BYOK settings + real TTS** — done when keys can be added/validated in Settings, and a script produces per-beat audio plus a valid timing manifest.
  3. **Image generation + local assembly/export** — done when platform-correct PNG sets and MP4 videos are assembled via ffmpeg from generated images + audio, timing-driven by the manifest.
  4. **Video-gen provider + polish** — done when a cloud video provider is wired in, and job resume, per-asset retry, spend caps, and cost confirmations work.
- Build and verify on Windows; the final app must run on the user's machine with only documented setup steps (deps + ffmpeg).

## 14. Changelog

- **v1.0** — Initial draft: mission, flow, platforms, engines, UI, non-goals, open decisions, milestones.
- **v1.1** — Resolved decisions locked (local web app, BYOK providers, English-only, no captions/music). Added visual-consistency requirement, per-beat TTS + timing manifest, local-vs-cloud split (ffmpeg), job resilience & spend guardrails, Windows environment, milestone definitions of done, cross-chat continuity.
- **v1.2** — Added §11 planning-stage requirements (build workflow, workload division, subagent/concurrency plan, plugins/skills plan, hooks plan, background/automation plan, risk list), §2 glossary, expanded functional detail (annotation convention, job lifecycle & persistence, maintenance tasks), `BUILD_PLAN.md` sign-off gate.
