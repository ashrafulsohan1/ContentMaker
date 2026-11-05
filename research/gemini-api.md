# Gemini API research — BUILD_PLAN task 2.0 (research spike)

**Date of research:** 2026-11-06
**Method:** read-only web research against official Google documentation (primary sources),
fetched with the web reader tool. No live API calls were made (no key exists yet, and the
spike is mandated to be read-only / zero-spend). Every fact below cites the page it came from,
with that page's own "Last updated" stamp where shown. Anything I could not verify is stated
as such in §9 — nothing in this file is a guess.

**Why this spike exists:** BUILD_PLAN §0 line 26: "Model IDs and exact prices are config data
(`app/data/prices.json`), verified by a research spike at the start of M2 — not hardcoded,
because this market moves fast." The market did move: two of the three models recommended in
BUILD_PLAN §0 no longer exist (Veo 3.1 Preview and the old TTS/image model IDs). §1–§4 record
what replaced them.

---

## TL;DR — provider design decisions for tasks 2.2 / 2.3 / 3.1 / 4.1

| Engine | Recommended model ID | Why | Price basis | Free tier |
|---|---|---|---|---|
| TTS | `gemini-3.8-flash-tts` | Docs steer long-form narration to Flash TTS ("long-form narrations requiring rock-solid voice and room-tone stability"); Flash-Lite TTS is the cheap bulk alternative | ~$0.02 per 1k chars (derived, see §2.4) | Yes |
| Image | `gemini-nano-banana-2.1` | Current workhorse ("primary high-efficiency workhorse model"), 1K/2K/4K, all aspect ratios we need, best multi-turn consistency | $0.0504 per 2K image (app targets ≥1080px) | No — paid only |
| Video | `gemini-omni-1.1-flash` | Veo 3.1 Preview was **shut down 2026-10-22**; Omni Flash is its replacement, GA on the paid tier | ~$0.10/s at 720p (5,792 output tokens/s × $17.50/1M) | No — paid only |

The one-key story from BUILD_PLAN §0 survives: one Google AI Studio key still covers
TTS + image + video, but the video leg is now Omni Flash, not Veo, and image/video require a
**paid** (billing-enabled) project while TTS alone works on the free tier.

All three engines now live on the same **Interactions API** surface (`POST
https://generativelanguage.googleapis.com/v1beta/interactions`) with the same auth header and
the same response envelope — a single `providers/gemini/client.py` (task 2.2) can serve all
three engines.

---

## 1. API surface — Interactions API (recommended) [source A, B, C]

The docs now present the **Interactions API** as the recommended surface ("Interactions API
(Recommended)" toggle on every guide page). All examples below are the REST shapes the docs
show.

### 1.1 Endpoint, auth, envelope
- `POST https://generativelanguage.googleapis.com/v1beta/interactions` [A, B, C]
- Auth: `x-goog-api-key: $GEMINI_API_KEY` header (all TTS/image examples) or `?key=$API_KEY`
  query param (several Omni curl examples). See §6 for key rules. [A, B, C]
- Response envelope (unary): an `interaction` object with `id` (`v1_...`), `status`
  (`"completed"`), `model`, `object: "interaction"`, and `steps: [...]`. Model output is in
  `steps[]` entries with `type: "model_output"` and `content[]` typed blocks
  (`text`, `audio`, `image`, `video`). The SDK convenience fields `interaction.output_audio`,
  `output_image`, `output_video`, `output_text` are **SDK-only** — direct REST must walk
  `steps[]`. [A, B, C]
- Streaming: `stream: true` yields SSE events; audio/video deltas arrive as
  `event.event_type == "step.delta"` with `event.delta.type == "audio"` (base64 in
  `delta.data`). Errors over SSE arrive as `event_type: "error"` (§7). [A, B]
- Follow-up turns (conversational editing) pass `previous_interaction_id: <id>` instead of
  rebuilding context — documented for image editing, video editing, and video extension. [B, C]
- A `GET /v1beta/interactions/{id}` fetch exists (the Omni page notes it currently returns the
  video as inline base64 even for URI deliveries). [B]

### 1.2 generateContent API (legacy)
Every guide page has a "generateContent API" toggle, i.e. the legacy surface still works, and
the TTS migration guide references `AUDIO_L16` in generateContent. I did **not** fetch the
generateContent variant of the TTS/image guides (docs toggle is client-side; the Interactions
REST shapes above are complete for our purposes). Task 2.2 should build on the Interactions
API; if a generateContent fallback is ever needed, its exact schema is unverified here.

---

## 2. TTS [source A + pricing source D]

### 2.1 Current model IDs
From the speech-generation guide (last updated 2026-10-12):
- **`gemini-3.8-flash-tts`** — Gemini 3.8 Flash TTS: single- + multi-speaker, voice design,
  voice replication. "Studio-grade voice fidelity, expressive acting, and long-form stability."
- **`gemini-3.8-flash-lite-tts`** — Gemini 3.8 Flash-Lite TTS: same API schema, "fast,
  cost-efficient workhorse replacement for `gemini-3.1-flash-tts-preview`", high-volume
  production.
- Still listed as supported but older: `gemini-3.1-flash-tts-preview`, Gemini 2.5
  Pro/Flash Preview TTS. The 2.5 TTS models are deprecated and, per the pricing page, all
  `gemini-2.5-*` text models route to newer models from 2026-12-01 — do not build on 2.5 TTS.
- **`gemini-2.5-flash-preview-tts` (the ID currently in prices.json) is legacy** — the pricing
  page still lists it ($0.50 in / $10.00 audio out per 1M tokens) but the docs steer all new
  work to 3.8 TTS and the 2.5 family is in deprecation flow.

SDK note: `google-genai >= 2.25.0` (Python) required for 3.8 TTS; we use raw httpx, so this
only matters as a signal that the Interactions schema is recent. TTS models accept **text-only
input** and produce **audio-only output**. Language is auto-detected (3.8 Flash TTS supports
130+ languages; the app is English-only v1). [A]

### 2.2 Request shape (single speaker — our per-beat case) [A]
```json
POST /v1beta/interactions
{
  "model": "gemini-3.8-flash-tts",
  "input": [{
    "type": "user_input",
    "content": [{
      "type": "text",
      "text": "Have a wonderful day!",
      "annotations": [{"type": "speech_metadata", "style": "cheerful and friendly"}]
    }]
  }],
  "response_format": {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000},
  "generation_config": {"speech_config": [{"voice": "Kore"}]}
}
```
Multi-speaker (up to 2 speakers with prebuilt voices) uses
`generation_config.speech_config: {"speakers": [{"speaker": "Joe", "voice": "Puck"}, ...]}`
and per-turn `speech_metadata.speaker`. We don't need it (per-beat single-speaker per spec),
but it's the same endpoint. A REST-only `"mode": "conversational"` key exists inside
`speech_config`. [A]

### 2.3 Voice / pace / tone parameters — the important design fact
There are **no numeric rate/pitch/volume parameters**. Control is natural-language, split by
scope [A]:
- **Voice selection** (identity): `generation_config.speech_config[].voice` — one of
  - 30 prebuilt studio voices (Zephyr—Bright, Puck—Upbeat, Kore—Firm, Fenrir—Excitable, …
    Sulafat—Warm; full list on the page),
  - the Extended Voice Library via `GET /v1beta/voices` (hundreds more, filterable by
    language/accent/gender/pitch/persona/context — documented REST shape on the page),
  - a Voice-design custom voice (`POST /v1beta/voices`, `type="prompted"` → persistent
    `voice_...` ID; 200 stored voices/project, 1-year TTL),
  - a replicated voice (`voice_...` / stateless `voicekey_...`, 7-day TTL).
- **Pace/tone/prosody (turn-level)**: free-text `speech_metadata.style`, e.g.
  `"speaking slowly"`, `"speaking rapidly"`, `"high pitch, cheerful and excited inflection"`,
  `"monotone and flat"`, `"whispered urgently"`. This is the app's pace/tone knob: presets map
  to short style strings, not numbers.
- **Point-in-time events (inline tags in the transcript)**: `<short pause>`, `<long pause>`,
  `<laugh>`, `<sigh>`, `<breath>`, … (large table on the page). Punctuation/ellipses give
  natural hesitation; CAPS give emphasis.
- The text field is a **verbatim transcript** — stage directions must live in
  `speech_metadata.style`, never in the text. Docs warn against long "Audio Profile" prompt
  blocks (cause of voice drift); recommended workflow = pick/design a voice, empty or one
  short constant `style` string.

### 2.4 Output formats and pricing
- Unary default: complete **WAV** (`audio/wav`, RIFF header) — 24 kHz, mono, 16-bit LE PCM;
  write bytes straight to `.wav`. Streaming default: headerless raw PCM `audio/l16`.
  Also available: `audio/mulaw`, `audio/alaw`, and `sample_rate` (24000/16000/8000 shown).
  For our pipeline: unary WAV per beat, ffprobe the file — no header surgery needed. [A]
- Pricing (Standard tier, per 1M tokens) [D, raw pricing markdown]:
  - `gemini-3.8-flash-tts`: input $0.50 (text) / output $9.00 (audio) **through 2026-12-31**;
    $1.00 / $18.00 from **2027-01-01**. Page-equivalent: **$0.00225 per 10 s of audio**
    (through 2026-12-31). Free tier: free of charge.
  - `gemini-3.8-flash-lite-tts`: $0.50 / $6.00 → **$0.0015 per 10 s**; doubles 2027-01-01.
    Free tier: free of charge.
  - `gemini-3.1-flash-tts-preview`: $1.00 / $20.00.
  - Footnote: "Audio tokens correspond to **25 tokens per second** of audio."
- **Conversion to the app's `usd_per_1k_chars` unit** (billing is time-based on output audio,
  so chars→seconds is an assumption I must state): English narration at a conservative
  **120 wpm** ≈ 2 words/s ≈ **11.4 chars/s** (5.7 chars/word incl. space) ⇒ 1,000 chars ≈
  87.7 s ≈ 2,193 audio tokens ⇒ flash-tts 2,193 × $9.00/1M ≈ **$0.0197/1k chars → shipped
  0.02**; lite 2,193 × $6.00/1M ≈ **$0.0132 → shipped alt 0.014**. At a mid-rate 14.25 chars/s
  the same math gives $0.016 / $0.011 — the shipped numbers err high on purpose (BUILD_PLAN
  cost guardrails are conservative; ffprobe durations at task 2.4 reconcile actual spend).
  **Re-verify after 2026-12-31** (prices double).

---

## 3. Images — Nano Banana [source C + pricing source D]

### 3.1 Current model IDs (guide last updated 2026-10-09)
| Model ID | Name | Free tier | Notes |
|---|---|---|---|
| `gemini-nano-banana-2.1` | Nano Banana 2.1 | No (paid only) | "primary high-efficiency workhorse"; 1K/2K/4K; text rendering; multi-turn consistency; web+image search grounding |
| `gemini-3.1-flash-image` | Nano Banana 2 | No | previous workhorse; adds 512px tier |
| `gemini-3.1-flash-lite-image` | Nano Banana 2 Lite | No | fastest/cheapest; **1K only**; not optimized for multi-reference/multi-turn |
| `gemini-3-pro-image` | Nano Banana Pro | No | premium, best text/brand consistency |
| `gemini-2.5-flash-image` | Nano Banana (legacy) | — | **deprecated, shut down 2027-03-15** (the ID currently in prices.json) |
| Imagen (`imagen-*`) | — | — | **"Imagen models are shut down. Use Nano Banana models for all image generation tasks."** |

### 3.2 Request shape [C]
```json
POST /v1beta/interactions
{
  "model": "gemini-nano-banana-2.1",
  "input": [
    {"type": "text", "text": "..."},
    {"type": "image", "mime_type": "image/png", "data": "<BASE64>"}   // optional refs
  ],
  "tools": [{"type": "google_search"}],                                // optional grounding
  "generation_config": {"thinking_level": "high"},                     // optional
  "response_format": {
    "type": "image",
    "mime_type": "image/jpeg",          // or image/png
    "aspect_ratio": "16:9",             // see dims table
    "image_size": "2K"                  // uppercase K mandatory; "1k" is rejected
  }
}
```
Response: `steps[] → model_output → content[]` blocks with `type: "image"` and base64 `data`
(convenience `interaction.output_image` is SDK-only). Thinking is always on and billed;
`thinking_level` ∈ minimal/medium/high (2.1 default `medium`). Multi-turn editing via
`previous_interaction_id` is "the recommended way to iterate". All outputs carry a SynthID
watermark. Up to **14 reference images** (10 objects + 4 characters high-fidelity on 2.1) —
this is the style-consistency mechanism for task 3.1 (task 3.0's spike should evaluate
reference-image + fixed style-prompt anchoring; also note 2.1's `search_types` image grounding
is not needed for us).

### 3.3 Aspect ratios and pixel sizes (Nano Banana 2.1, from guide table) [C]
1K / 2K pixel dims per ratio: 1:1 → 1024²/2048² · 9:16 → 768×1376 / 1536×2752 · 16:9 →
1376×768 / 2752×1536 · 4:5 → 928×1152 / 1856×2304 · 3:4, 4:3, 2:3, 3:2, 21:9, 1:4, 4:1, 8:1,
1:8 also supported. Tokens per image: 1K = 1120, 2K = 1680, 4K = 2520–3780 depending on model.
**Fit check vs our `platforms.json` targets (1080×1920, 1080×1080, 1080×1350, 1920×1080):
1K is undersized on every ratio (e.g. 9:16@1K = 768×1376 < 1080×1920), so the app should
request `image_size: "2K"` and let assembly downscale to platform dims** — that is why the
shipped per-image price is the 2K figure.

### 3.4 Pricing (Standard tier) [D]
- `gemini-nano-banana-2.1`: input $1.50/1M; output $30.00/1M image tokens ⇒ **$0.0336 per 1K
  image, $0.0504 per 2K image, $0.113 per 4K image**. Batch = half.
- `gemini-3.1-flash-image`: $60/1M ⇒ $0.045 (0.5K) / $0.067 (1K) / $0.101 (2K) / $0.151 (4K).
- `gemini-3.1-flash-lite-image`: $30/1M ⇒ **$0.0336 per 1K image** (only 1K).
- `gemini-3-pro-image`: $120/1M ⇒ $0.134 (1K/2K), $0.24 (4K).
- No free tier on any current image model — **paid/billing-enabled project required**.

---

## 4. Video — Gemini Omni Flash (Veo is gone) [source B + pricing source D]

### 4.1 Model status
Video guide (fetched 2026-11-06): "**The Veo 3.1 Preview in the Gemini API has been shut
down**" — specifically `veo-3.1-generate-preview`, `veo-3.1-fast-generate-preview`,
`veo-3.1-lite-generate-preview` were **shut down on October 22, 2026**; migrate to Gemini Omni
Flash (`gemini-omni-1.1-flash`) or Gemini Enterprise Agent Platform models. The image guide's
"Other image generation modes" likewise says Imagen is shut down. **The `veo-3.1-fast-
generate-preview` ID currently in prices.json is dead** — estimates/keys testing against it
would 404 (`model_not_found`).

### 4.2 Request shape [B]
```json
POST /v1beta/interactions
{
  "model": "gemini-omni-1.1-flash",
  "input": "A drone shot of a mountain landscape at sunrise.",
  // or input: [{"type": "image", "data": "<B64>", "mime_type": "image/jpeg"},
  //            {"type": "text", "text": "..."}]                      // image-to-video
  "response_format": {
    "type": "video",
    "aspect_ratio": "9:16",             // "16:9" default | "9:16"
    "resolution": "1080p",              // 360p | 720p (default) | 1080p (upscaled) | 4k (upscaled)
    "delivery": "uri"                   // required for clips > 4 MB
  },
  "generation_config": {"video_config": {"task": "image_to_video"}}
  // task: text_to_video | image_to_video | reference_to_video | edit | extend
}
```
Response: `steps[] → model_output → content[]` block `{"type": "video", "mime_type":
"video/mp4", "data": "<base64>"}` (inline) or `{"type": "video", "mime_type": "video/mp4",
"uri": "https://generativelanguage.googleapis.com/v1beta/files/...:download?alt=media"}`
(URI delivery). **URI flow**: poll `GET /v1beta/files/{id}` until `state == "ACTIVE"`
(`FAILED` aborts), then download the URI. Docs recommend URI delivery for anything > 4 MB
(720p+ clips) and `background=false, store=false, stream=false` for fast synchronous unary
generation. Conversational editing via `previous_interaction_id`; extension adds 3–10 s per
turn, up to 40 s total. Not supported: system instructions, temperature, top_p, negative
prompts (put negatives in the prompt text), YouTube input, audio-reference upload. SynthID
watermark on all output; input+output safety filters; uploading/editing videos is unavailable
in EEA/CH/UK. Generated video includes a generated audio track (describe it in the prompt).

### 4.3 Duration and pricing
- Per-clip duration is **not stated** on the pages I read (the extension math — +3–10 s per
  turn, 40 s max total — implies short base clips, but I found no documented base duration).
  **Unresolved for task 4.1; probe with a real key.**
- Pricing (Standard, `gemini-omni-1.1-flash`, GA paid tier) [D]: input $1.50/1M; output
  $9.00/1M text + **$17.50/1M video**; footnote: "Billing is based on total output token
  consumption, calculated at a rate of **5,792 tokens per second of 720p video**. Under
  Standard pricing, this equates to an effective price of approximately **$0.10 per second**."
  Exact: 5,792 × $17.50/1M = **$0.10136/s** → shipped 0.102 (rounded up). Billing at
  1080p/4K (upscaled output) is **not documented** on the pricing page I read — verify at task
  4.1 before quoting higher-res costs. Free tier: not available (paid only). A 10 s 720p clip
  ≈ $1.01 + negligible input tokens.

---

## 5. Rate limits & billing tiers [source E]

- Limits are per **project** (not per key), across RPM / TPM / RPD; RPD resets midnight
  Pacific. Image generation has an "images per minute" (IPM) analogue for Nano Banana models.
- The docs no longer publish a static per-model RPM/RPD table — active limits are shown in
  AI Studio's rate-limit dashboard. Free tier exists for the TTS models (§2.4); **image and
  video models are paid-tier only**.
- **Spend-based rate limits** (rolling 10 min): Tier 1 = $10, Tier 2/3 = $200; breach returns
  `429 RESOURCE_EXHAUSTED`. Tiers: Free (no billing) → Tier 1 (billing linked; billing-spend
  cap $250) → Tier 2 ($100 cumulative spend + 3 days; cap $2,000) → Tier 3 ($1,000 + 30 days).
  Implication for our cost caps: a fresh BYOK project sits in Tier 1 — a per-job cap above
  ~$10 can hit Google's own spend limiter mid-job; the app's hard cap (spec §6.6) should
  expect 429s below its own cap and treat them as retryable.

## 6. Auth & keys [source F]

- Header `x-goog-api-key` (or `?key=` query param). SDKs read env vars `GEMINI_API_KEY` /
  `GOOGLE_API_KEY`; a BYOK app passes the key explicitly per request — never via env.
- **Key types**: "standard" keys (project-bound) vs "authorization (auth) keys"
  (service-account-bound). Since **2026-05-28** all new AI Studio keys are auth keys.
- **Unrestricted standard keys are rejected by the API**; standard keys must carry explicit
  restrictions (restrict-to-Gemini-API is the documented path). Unrestricted keys dormant
  since 2026-05-07 are blocked. First-key UX (task 2.1) must surface this: a valid-but-
  misconfigured key can 401/403 with "unrestricted" as the root cause.
- Key test probe options for task 2.1/2.2 (both documented shapes): `GET /v1beta/voices` with
  the key header (documented on [A], zero-spend, 401 `authentication` on a bad key) or a
  minimal free-tier `gemini-3.8-flash-tts` text call. A documented `GET /v1beta/models` list
  endpoint did not appear on any page I fetched — do not assume it.

## 7. Error shapes [source G] — the mapping task 2.2 must implement

All errors are `{"error": {"code": "<snake_case>", "message": "..."}}` with the HTTP status
set. Streaming requests deliver the same object as an SSE event `{"event_type": "error",
"error": {...}}`.

Retry policy for our per-asset retry model (jobs.py bounded retry w/ backoff):

| Code | HTTP | Class for us |
|---|---|---|
| `authentication` | 401 | **permanent** — "key missing/invalid/expired" → key-test / fail asset with key message |
| `permission_denied` | 403 | permanent — key lacks access (incl. unrestricted-key rejection) |
| `not_found`, `model_not_found` | 404 | permanent — wrong/dead model ID (e.g. any leftover Veo ID) |
| `invalid_request`, `parameter_unknown`, `failed_precondition`, `out_of_range` | 400/416 | permanent — our request bug or disabled billing |
| `rate_limit_exceeded`, `too_many_requests` | 429 | **retryable** (per-minute/second limits) — backoff |
| `quota_exceeded` | 429 | retryable-but-slow (daily quota) — long backoff; surface to user |
| `api_error` | 500 | **retryable** |
| `service_unavailable` | 503 | **retryable** |
| `deadline_exceeded` | 504 | retryable |
| `cancelled` | 499 | client disconnect — treat like our cancel path |

Generation **blocked** codes (policy/safety — permanent, message the user, do not retry):
`safety`, `recitation`, `language`, `prohibited_content`, `spii`, `blocklist`,
`image_safety`, `image_prohibited_content`, `image_recitation`, `image_other`,
`content_blocked`. Generation **error** codes (structural): `malformed_function_call`,
`malformed_tool_call`, `unexpected_tool_call`, `no_image`, `too_many_tool_calls`,
`missing_thought_signature`.
Caveat: the rate-limits page still names the gRPC-style string (`429 RESOURCE_EXHAUSTED`),
which suggests legacy surfaces can return different code spellings — task 2.2 should branch
on **HTTP status first**, treat `error.code` as advisory text for the message.

## 8. What went into `app/data/prices.json` (and why)

| Engine | model | unit | price | Derivation / source |
|---|---|---|---|---|
| tts | `gemini-3.8-flash-tts` | usd_per_1k_chars | 0.02 | §2.4: 25 tok/s × $9/1M audio tokens; 1k chars at conservative 120 wpm (11.4 chars/s) ⇒ ~2,193 audio tokens ⇒ $0.0197, rounded up |
| image | `gemini-nano-banana-2.1` | usd_per_image | 0.0504 | §3.4: page-stated 2K equivalent ($30/1M × 1,680 tokens); 2K chosen because platforms.json targets ≥1080 px which 1K cannot cover |
| video | `gemini-omni-1.1-flash` | usd_per_second | 0.102 | §4.3: 5,792 tok/s × $17.50/1M = $0.10136/s at 720p, rounded up |

Notes shipped alongside: TTS doubles 2027-01-01; image/video paid-only; Lite TTS alternative
(0.014/1k chars) and other image tiers documented in the file's `_meta`/`note` fields; **the
`verified` flag on the gemini provider is intentionally left `false`** — tests/test_cost.py:131
asserts a non-mock placeholder must exist "until the M2 spike" and test-writing is outside the
spike's mandate (BUILD_PLAN §4), so flipping it to `true` (and relaxing that assertion) is a
one-line follow-up for the lead at task 2.1/2.2. The prices themselves are spike-verified as
of 2026-11-06; `_meta.warning` in the file says exactly that.

## 9. Not verified / open questions (do not assume)

1. **Base clip duration for Omni Flash** — not documented on any page I read; probe with a
   real key at task 4.1. (Extension limits are documented: +3–10 s/turn, 40 s max total.)
2. **1080p/4K video billing** — resolutions exist ("upscaled") but no per-second price is
   published on the pricing page I fetched; 720p's 5,792 tok/s is the only rate given.
3. **`gemini-omni-1.2-flash`** — a web-fetch summarizer pass claimed a 1.2 model with
   $12.00/1M video output routing from 2026-12-03, but the **raw pricing markdown** I then
   fetched lists only `gemini-omni-1.1-flash`. I treat 1.2 as unconfirmed; 1.1 is what ships.
   Re-check around 2026-12-03.
4. **generateContent variant schemas** for TTS/image (docs toggle not fetched) — build on
   Interactions API.
5. `GET /v1beta/models` endpoint for key testing — not seen in fetched docs; use
   `GET /v1beta/voices` (documented) instead.
6. Free-tier **rate limits** per model (RPM/RPD numbers) — no longer published as a static
   table; only visible per-account in AI Studio.

## 10. Sources

All fetched 2026-11-06 via the web reader tool (raw page content, not summaries):

- **[A]** Text-to-speech (TTS) generation — https://ai.google.dev/gemini-api/docs/speech-generation — page-stated "Last updated 2026-10-12 UTC"
- **[B]** Video generation with Gemini Omni Flash — https://ai.google.dev/gemini-api/docs/omni — page-stated "Last updated 2026-09-23 UTC"; video overview https://ai.google.dev/gemini-api/docs/video (Veo shutdown notice)
- **[C]** Nano Banana image generation — https://ai.google.dev/gemini-api/docs/image-generation — page-stated "Last updated 2026-10-09 UTC"
- **[D]** Gemini Developer API pricing — raw markdown https://aistudio.google.com/docs/pricing.md (canonical https://ai.google.dev/gemini-api/docs/pricing, 301 → aistudio.google.com/docs/pricing); no page date shown; fetched 2026-11-06
- **[E]** Rate limits — raw markdown https://aistudio.google.com/docs/rate-limits.md; no page date shown
- **[F]** API keys / auth — https://ai.google.dev/gemini-api/docs/api-key (canonical aistudio.google.com/docs/api-key); no page date shown
- **[G]** API errors — https://ai.google.dev/gemini-api/docs/api-errors — page-stated "Last updated 2026-08-26 UTC"
