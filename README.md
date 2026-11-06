# ContentMaker

A local web app that turns a finished script into platform-ready social media
content. You paste a script, pick a platform/format, and ContentMaker splits it
into beats, generates per-beat narration (TTS) and visuals with **your own**
cloud API keys (BYOK), then assembles everything locally with **ffmpeg** into
MP4 / PNG / JPEG deliverables.

- **BYOK (Bring Your Own Key)** — keys stay on your machine, are sent only to
  the provider they belong to, and are never committed anywhere.
- **Cost guardrails** — a price estimate is shown and must be confirmed before
  any paid call; running spend is live during the job; an optional hard
  per-job USD cap stops paid calls mid-job.
- **Dry-run mode** — zero-network mock engines drive the whole flow, assembly
  included, at $0.00. Great for trying the app before you have any key.
- **Resumable jobs** — every state transition is persisted (SQLite WAL), so a
  closed tab or a killed server does not lose completed work; reopening the UI
  reattaches to the live job.

Tech: Python 3.11+ / FastAPI / Uvicorn, server-rendered htmx UI (no build
step), stdlib SQLite, local ffmpeg/ffprobe. Single-user, runs on your machine,
listens on localhost only.

---

## 1. Requirements (Windows 10/11)

| Requirement | Notes |
|---|---|
| **Python 3.11 or newer** | `python --version` to check. |
| **ffmpeg + ffprobe on PATH** | Used for all audio/video/image encoding and duration probing. |
| A browser | Any modern browser; the UI is served locally. |

No API key is required to start — the app runs fully in dry-run mode without
one. A key is only needed when you switch engines from mocks to a real
provider (see §4).

## 2. Setup (one time)

Open **PowerShell** (or Command Prompt) and run:

```powershell
# 1. Install ffmpeg (includes ffprobe). A new terminal is needed afterwards so it lands on PATH.
winget install Gyan.FFmpeg

# 2. Get the code and enter the folder
cd path\to\ContentMaker

# 3. Create a virtual environment
python -m venv .venv

# 4. Activate it (PowerShell)
.venv\Scripts\Activate.ps1
#    Command Prompt:  .venv\Scripts\activate.bat
#    Git Bash:        source .venv/Scripts/activate

# 5. Install the dependencies
pip install -r requirements.txt

# 6. Verify the environment — everything must print [ok]
python scripts/check_env.py
```

`scripts/check_env.py` checks Python ≥ 3.11, `ffmpeg` and `ffprobe`, and prints
a setup hint if something is missing. Expected output looks like:

```
[ok] python — 3.14.0 (min 3.11)
[ok] ffmpeg — ffmpeg version 8.0.1-full_build-www.gyan.dev ...
[ok] ffprobe — ffprobe version 8.0.1-full_build-www.gyan.dev ...
```

If PowerShell refuses `Activate.ps1` (execution policy), either run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use the Command
Prompt activation script above.

## 3. Run the app

From the project folder with the venv activated:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Then open **http://127.0.0.1:8765/** in your browser.

The server binds to `127.0.0.1` (localhost only) — it is not reachable from
other machines. Change the port by editing the `--port` flag if 8765 is taken.

### Using the app

The UI walks you through four steps:

1. **New script** — paste your finished script and name the project.
2. **Review beats** — the script is split into beats (blank lines); edit,
   merge, or split beats as needed.
3. **Options** — pick platform and format (seven platforms: YouTube, TikTok,
   Instagram, LinkedIn, X, Facebook, Pinterest — defined in
   `app/data/platforms.json`), a style preset, TTS/visual providers, the
   optional **hard spend cap**, and the **dry-run** checkbox.
4. **Estimate & confirm** — a USD estimate is shown; generating starts only
   after you confirm. Then the job monitor shows per-asset progress, live
   spend, and retry controls. Deliverables land in `projects/<name>/output/`.

The pipeline per beat: TTS audio → ffprobe the real duration → a **timing
manifest** (the single source of truth for assembly) → visuals → local ffmpeg
assembly into platform-correct deliverables (H.264/AAC MP4 or PNG/JPEG sets,
including carousels). If you pick image visuals on an MP4 format, the video is
composed from the stills automatically — Ken Burns pan/zoom motion with
crossfades — so a video deliverable does not require a video-capable key.

## 4. Dry-run mode (no key needed)

On the options step, the **“Dry run — mock engines, zero network, zero spend”**
checkbox is ticked by default. In dry-run:

- TTS produces silent MP3s of the requested duration; images are labeled PNGs;
  video beats are color clips — all generated locally, with real ffmpeg doing
  the final assembly. The whole flow (beats, manifest, job monitor, export)
  works exactly as with real providers.
- Nothing leaves your machine and nothing costs anything. The confirm page
  tells you so, and the estimate is $0.00.

Use dry-run to learn the app and verify your setup; untick it once you have
added a real key.

## 5. API keys (BYOK guide)

Open **Settings** in the top navigation (or http://127.0.0.1:8765/settings).

**Google Gemini is the recommended first key** — one AI Studio key covers all
three engines (TTS, images, video):

1. Create a key at https://aistudio.google.com/apikey (Google account required).
   Tip shown in the app: new keys are “authorization keys” — keep (or set) a
   **Gemini API restriction** on them; unrestricted standard keys are rejected
   by the API.
2. In Settings → *Google Gemini*, paste the key and click **Add**. Then click
   **Test** — the app validates the key against the Gemini API with a
   zero/min-spend probe and reports exactly what is wrong if it is not valid.
3. Back on the options step, untick dry-run and pick the Gemini provider for
   the engines you want. Engines without a usable key stay dry-run-only.

**Video clip length (known limitation):** a video beat is currently generated
as a ~3-second clip regardless of how long its narration is; final assembly
then loop-pads the clip (footage repeats on screen) to fill the beat's full
audio slot, and the cost estimate prices those ~3 seconds per beat — exactly
what is generated and billed, not what a full beat-length clip would cost.
Aligning clip length with the probed narration length is a recorded design
deferral, not yet implemented.

Key handling rules (enforced by the app, not just convention):

- Keys are stored in **`state/keys.json`**, which `.gitignore` excludes and
  which is written atomically with owner-only permissions where the OS honors
  it (advisory on Windows).
- The UI only ever shows a masked form (`••••last4`); the raw key is used only
  by the code path that calls that provider.
- A key is sent **only to the provider it belongs to** — nowhere else.
- If you configure several providers for one engine, Settings lets you pick a
  per-engine default; removing a key also drops any default pointing at it.

## 6. Writing your script

Beats are the unit everything works on (one narration clip + one visual per
beat). The plain-text convention:

- **Blank lines** separate beats — each paragraph is one beat.
- `[scene: short-label]` on its own line names the *next* beat (nice for the
  review list and filenames-adjacent labels).
- `<!-- img: description -->` before a paragraph hints what that beat's visual
  should show.

Example:

```
[scene: intro]
Ninety percent of creators quit before their tenth video.
<!-- img: a lone desk lamp over an empty editor timeline -->

Here is the part nobody tells you about consistency.

[scene: payoff]
It was never about motivation — it was about the checklist.
```

Malformed markers degrade gracefully: an unmatched `[scene:` stays as plain
text, an unclosed `<!-- img:` still yields a hint, and nothing crashes.

## 7. Where files live

```
ContentMaker/
  app/                     # application code
    data/                  # EDITABLE config tables: platforms.json, presets.json, prices.json
    web/templates, static/ # server-rendered htmx UI (no build step)
  projects/                # one folder per project (created by the app)
    <project name>/
      script/              # the pasted script (script.txt)
      plan/                # timing manifest per job — single source of truth for assembly
      audio/               # per-beat TTS audio
      visuals/             # per-beat images / clips
      output/              # final deliverables (MP4 / PNG / JPEG, export ZIP)
      state/               # job.db (SQLite, WAL mode) + spend_log.jsonl (spend audit trail)
  state/                   # app-level state: keys.json (BYOK keys) — gitignored
  scripts/                 # check_env.py + per-milestone smoke checks
```

Everything under `projects/` and `state/` is user content and runtime state —
never commit it (the repo's `.gitignore` already excludes both). All job state
is SQLite in WAL mode, designed to tolerate OneDrive-style sync on the folder;
if you ever see file-lock errors during heavy sync, moving the repo out of the
synced folder is the reliable fix.

Platform specs, style presets, and prices are **editable config data** in
`app/data/*.json` — add a platform or adjust prices there, not in code.

## 8. Verifying your installation

```powershell
python scripts/check_env.py     # python ≥ 3.11, ffmpeg, ffprobe — all [ok]
pytest -q                       # full test suite (offline; no keys needed)
python scripts/smoke_m1.py      # end-to-end flow on mock engines, zero network
python scripts/smoke_m2.py      # keys/estimate/cap flow (fake probe, no real API)
python scripts/smoke_m3.py      # platform-exact image/carousel + MP4 assembly
python scripts/smoke_m4.py      # resume-mid-job + per-asset retry + spend cap
```

All smoke checks drive the real HTTP app on the zero-network mock engines and
exit 0 when green. They use throwaway temp project folders and leave the repo's
`projects/` untouched. The test suite and smokes require no API keys.

## 9. Troubleshooting

- **`ffmpeg ... not found on PATH`** from `check_env.py` — install with
  `winget install Gyan.FFmpeg` (or download from
  https://www.gyan.dev/ffmpeg/builds/), then **open a new terminal** so PATH
  updates apply, and re-run the check.
- **Port already in use** — pick another: `--port 8766`.
- **Key rejected at Test** — the error names exactly what's wrong. The common
  case for Gemini: the key is an unrestricted “standard” key — add a Gemini
  API restriction in AI Studio, or create an “authorization key” with one.
- **Job interrupted (server killed, tab closed)** — just restart the server
  and reopen the job's monitor page: completed assets are kept, and an
  in-flight beat resumes without redoing paid work.
- **Text overlay font** — optional per-image text overlays use a system font
  discovered automatically; set the `CONTENTMAKER_FONT` environment variable
  to a specific `.ttf`/`.otf` path to override it.
- **uvicorn not found** — the venv is not activated; re-run the activation
  line from §2 step 4.
