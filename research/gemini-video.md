# Gemini video research — BUILD_PLAN task 4.0 (research spike)

**Date of research:** 2026-11-06
**Method:** read-only web research against official Google documentation (primary sources),
fetched with the web reader tool plus `curl` against the raw-markdown `.md` endpoints the
docs expose (`https://aistudio.google.com/docs/<page>.md`). No live API calls were made (no
video-capable spend authorized, and the spike is mandated read-only / zero-spend). Every fact
below cites the page it came from, with that page's own "Last updated" stamp where shown;
raw-markdown endpoints carry no stamp, so they are cited by fetch date. Anything I could not
verify is stated as such in §8 — nothing in this file is a guess.

**Relation to task 2.0:** `research/gemini-api.md` (task 2.0 spike) established the
Interactions API surface, auth, error taxonomy, and a first video pass (`gemini-omni-1.1-flash`,
~$0.10/s at 720p, URI delivery polled via `GET /v1beta/files/{id}`). It left three open
questions (its §9): base clip duration, 1080p/4K billing, and whether `gemini-omni-1.2-flash`
exists. This spike resolves **all three** and goes deep on exactly the four things task 4.0
names: async job polling (§3), file download (§4), per-second pricing (§5), safety/latency
limits (§6).

---

## TL;DR — decisions for task 4.1

| Question | Conclusion |
|---|---|
| Model for 4.1 | **`gemini-omni-1.2-flash`** — next-generation video model, GA on the paid tier, up to 15s clips, **$12.00/1M video output → ~$0.07/s at 720p**. The 1.1 recommendation from task 2.0 is superseded: 1.1 is "previous generation" and **auto-routes to 1.2 with 1.2 billing from 2026-12-03** (three weeks out) [C, E]. `prices.json` still carries 1.1 @ $0.102 — flipping that row belongs to 4.1 (test_cost.py pins it; this spike is read-only) |
| API shape | One stateless `POST /v1beta/interactions` per beat, `response_format: {"type":"video","aspect_ratio":"9:16"|"16:9","resolution":"720p","delivery":"uri"}` — same surface/auth/errors as TTS+image; the one 2.2 `GeminiClient` serves video too [A] |
| Async polling | Two documented levels: (1) the interaction itself — unary POST blocks until `"status":"completed"`, and Google warns plain HTTP "typically close after 60 seconds", with `background:true` as the documented async alternative (poll `GET /v1beta/interactions/{id}`); (2) the generated file — `delivery:"uri"` returns a URI whose file must be polled at `GET /v1beta/files/{id}` until `state == "ACTIVE"` (docs' examples poll every 5 s) [A, F] |
| Download | `GET /v1beta/files/{id}:download?alt=media` on the Gemini host (key header/query), or SDK `files.download`. Files expire after **48 hours**. Gotcha: `GET /v1beta/interactions/{id}` currently returns the video as inline base64 even for URI deliveries — **capture the URI from the creation response**, never rely on a refetch [A, G, I] |
| Per-second price | 720p = 5,792 output tokens/s × $12/1M = **$0.0695/s** (docs: "~$0.07/s; ~$0.70 for a 10s clip"); 360p $0.023/s, 1080p (upscaled) $0.104/s, 4K (upscaled) $0.209/s [C]. All video models are **paid-tier only** (Free Tier: "Not available") [C] |
| Aspect ratios | **`16:9` and `9:16` only** [A] — and every `kind:"video"` format in `app/data/platforms.json` is exactly 16:9 (1920×1080) or 9:16 (1080×1920), so no platform format needs padding/cropping. Output is **24 FPS**; assembly already normalizes fps (1.12) [A, E, L] |
| Safety | SynthID watermark on every generated video; content filters on **input AND output** (region-varying), policy-violating prompts blocked; thresholds **not configurable** for video; editing/extending *uploaded* videos unavailable in EEA/CH/UK; English fully supported [A, K] |
| Latency | **No per-clip generation-time numbers are documented anywhere** — "Video generation times vary based on duration, resolution, and current API load" [A]. Hard limits that bound the design instead: Tier 1 = **8 requests/min and 40 requests/day** for Omni, $10-spend-per-10-min limiter [D]; clips 3–15 s (1.2) [E] |
| Clip duration | 1.2: **3–15 s** (3–10 s for first/last-frame interpolation and extension; extension chains "up to 30 s total" per the model page, "up to a total length of 40 s" per the guide — discrepancy flagged §7.4) [A, E]. Task 2.0's open question resolved |

---

## 1. Model lineup — Veo is gone, Omni 1.2 is current [B, C, E]

Source [B] = video overview (`ai.google.dev/gemini-api/docs/video` / canonical
`aistudio.google.com/docs/video`, fetched 2026-11-06); [C] = pricing raw markdown; [E] = model
page raw markdown (`aistudio.google.com/docs/models/gemini-omni-flash.md`, page-stamped
"Latest update: November 2026" for 1.2 / "August 2026" for 1.1).

| Model ID | Status | Output video | Note |
|---|---|---|---|
| `gemini-omni-1.2-flash` | **Stable, GA on paid tier** | **3s–15s**, 360p/720p/1080p/4K, **24 FPS** | "Our next-generation video generation and editing model supporting up to 15 seconds of video generation and editing with native audio, now generally available to developers on the paid tier" [C]. Input token limit 131,072; output 65,536 [E] |
| `gemini-omni-1.1-flash` | Stable, **previous generation** | 3s–10s, same resolutions, 24 FPS | "Effective December 3, 2026, all API requests to `gemini-omni-1.1-flash` automatically route to `gemini-omni-1.2-flash` and are billed at `gemini-omni-1.2-flash` rates." [C, E] |
| `gemini-omni-flash-preview` | **Shut down** | — | listed as "Shut down" in the versions table [E] |
| `veo-3.1-generate-preview`, `veo-3.1-fast-generate-preview`, `veo-3.1-lite-generate-preview` | **Shut down 2026-10-22** | — | "The Veo 3.1 Preview in the Gemini API has been shut down… migrate to Gemini Omni Flash (`gemini-omni-1.1-flash`) or the GA models available through the Gemini Enterprise Agent Platform" [B]. Reconfirms task 2.0 |

Task 2.0 recorded (its §9) that a fetcher-summary claim of a 1.2 model with $12.00/1M routing
from 2026-12-03 could **not** be confirmed in the raw pricing markdown it fetched that day.
Today the raw pricing markdown **does** list `gemini-omni-1.2-flash` with exactly those
numbers (§5 below) — the summarizer's claim is confirmed by the primary source as of this
fetch, and 2.0's caution is resolved in its favor.

## 2. Request surface (video on the Interactions API) [A]

All from the Omni guide [A] ("Last updated 2026-09-23 UTC"), which matches the shapes task 2.0
recorded for TTS/image — same endpoint, same auth, same envelope.

- `POST https://generativelanguage.googleapis.com/v1beta/interactions`, model
  `gemini-omni-1.2-flash`, `input` = plain string (text-to-video) or a list of typed blocks
  (`image`/`video`/`text`) for media-guided tasks. Auth `x-goog-api-key` header (or `?key=` —
  several Omni curl examples use the query param; the 2.2 client's header-only rule stays the
  safer default).
- Response: `steps[] → model_output → content[]` with a block
  `{"type":"video","mime_type":"video/mp4","data":"<base64>"}` (inline) or
  `{"type":"video","mime_type":"video/mp4","uri":"https://generativelanguage.googleapis.com/v1beta/files/...:download?alt=media"}`
  (URI). `interaction.output_video` is **SDK-only** — REST walks `steps[]` (task 2.0 §1.1
  already pinned this for audio/image; identical here).
- **`response_format`** for video: `{"type":"video", "aspect_ratio": "16:9"|"9:16",
  "resolution": "360p"|"720p"|"1080p"|"4k", "delivery": "uri"}`. Docs: `type` is optional;
  aspect_ratio supported values are **only "9:16" and "16:9"** (16:9 default); `resolution`
  default **720p**, with 1080p and 4k marked **"(upscaled)"** in the docs' own table.
- **`generation_config.video_config.task`** — `text_to_video | image_to_video |
  reference_to_video | edit | extend`. Docs recommend relying on prompting first and using
  `task` only "when prompting doesn't work… as using the `task` field adds some constraints."
  For our per-beat text-to-video: send no `task`.
- Media-guided modes that matter downstream: image-to-video (reference image + text),
  **first/last frame interpolation** (two images), subject reference, and `<FIRST_FRAME>` /
  `<LAST_FRAME>` / `<IMAGE_REF_N>` / `<VIDEO_REF_N>` prompt tags. Option for 4.1: feed the
  beat's M3-generated image as the first frame (`task:"image_to_video"`) to inherit the
  style anchor — my recommendation, flagged as such in §7.
- Not supported (Limitations [A]): system instructions, temperature, `top_p`, stop sequences,
  negative prompts (negatives go in the prompt text, e.g. "No dialogue"), YouTube as a media
  source, multi-video reasoning, provisioned throughput. Video references: max 3 clips,
  ≤3 s each; audio references cannot be uploaded; voice editing unsupported.
- Best practices [A]: "Use URI delivery for large videos: For videos larger than 4MB (>720p
  when available), use `delivery="uri"` in `response_format` to avoid payload size limits."
  Also: "Set `background=false`, `store=false`, and `stream=false` for faster, synchronous
  unary generation" — with the caveat that `store=false` makes the video non-editable via
  `previous_interaction_id` (we don't chain turns, so we could set it — but `background:true`
  *requires* `store:true` [F], see §3).
- Prompting notes that change provider behavior [A]: by default Omni "will try to create a
  video with a few different shots" — a single-scene beat must prompt for it ("Continuous,
  unbroken … single scene, No scene cuts"). Generated audio is described in the prompt
  ("No dialogue" is the docs' own negative-prompt example). Timing events accept natural
  language ("After 3 seconds, a woman enters the scene") and timecode syntax (`[0-3s] …`).

## 3. Async job polling [A, F]

Task 4.0's "async job polling" maps to **two distinct waits** in the current API; both are
documented with code:

### 3.1 Wait 1 — the interaction itself

- **Unary (docs' default for Omni):** the POST blocks until the interaction reaches
  `"status": "completed"` and carries the video inline/URI in the same response [A]. The
  background-execution guide [F] states why this can bite: "standard HTTP requests… typically
  close after 60 seconds".
- **Background execution:** `"background": true` on the create POST → "The API immediately
  returns an interaction ID, which client applications can use to poll for status, stream
  progress, or reconnect to a disconnected stream." Poll =
  `GET /v1beta/interactions/{id}` (same auth) until a terminal state; the guide's Python/JS/Go
  examples poll every 5 s (agent example 2 s). States: `in_progress`, `requires_action`,
  `completed`, `failed`, `cancelled` [F]. Cancel: `POST /interactions/{id}/cancel`
  (status → `cancelled`; server clean-up "can cause a slight delay before the status updates
  in GET requests"). Delete: `DELETE /interactions/{id}` → later GETs 404 [F].
  `background:true` **requires** `store:true` (the default); combining it with
  `store:false` returns `400` [F]. Stored background interactions are retained **55 days on
  paid tier** (configurable 7/14/28/55 in AI Studio; 1 day free) [F].
- **Ambiguity, disclosed:** the background-execution page's support list names "standard
  Gemini models (such as `gemini-4-argon` and `gemini-3.8-flash`) and Managed Agents" — Omni
  is not named — yet the Omni guide's best practices instruct setting `background=false` for
  fast unary generation, which only makes sense if the parameter is accepted for Omni. I
  could not resolve this from the docs alone. §7.1 recommends shipping the docs'-example
  unary+URI path first and probing `background:true` with a real key at 4.1.
- **Webhooks (preview):** project-level or per-request push notifications when LROs complete,
  "available for operations like Batch jobs, Interactions and video generation"; the docs'
  example handles a `video.generated` event carrying `event['data']['output_file_uri']` [H].
  A localhost-only app has no public listener URL, so polling stays the right pattern for us;
  recorded as the alternative, not the plan.

### 3.2 Wait 2 — the generated file (URI delivery)

- `delivery:"uri"` returns `content[].uri` pointing at
  `https://generativelanguage.googleapis.com/v1beta/files/<id>:download?alt=media` [A].
  The file behind it is **not** immediately downloadable — poll
  `GET /v1beta/files/{id}` until `state == "ACTIVE"`; docs' Bash example:
  `curl -X GET "https://generativelanguage.googleapis.com/v1beta/files/$FILE_ID?key=$API_KEY"`,
  loop on `state != ACTIVE`, `exit 1` on `FAILED`, `sleep 5` between checks [A]. The SDK
  examples poll identically (5 s) [A].
- File states (Files API get reference [I]): `STATE_UNSPECIFIED`, `PROCESSING` ("being
  processed and cannot be used for inference yet"), `ACTIVE` ("processed and available"),
  `FAILED` ("failed processing"), plus output-only `uri`, `downloadUri`, `expireTime`, and
  `error` fields.
- **Gotcha [A]:** "Currently, calling `GET /v1beta/interactions/{id}` returns the video as
  inline base64 data in the `data` field, even if the interaction was originally created with
  `delivery: "uri"`. The `uri` field is only guaranteed to be present in the initial creation
  response or Server-Sent Events (SSE) stream." → the provider must capture the URI from the
  creation response and persist it until download completes.

## 4. File download [A, G, I]

- Download endpoint: `GET /v1beta/files/{id}:download?alt=media` — docs' Bash example passes
  `?key=$API_KEY`; header auth (`x-goog-api-key`) is the same host and equally valid (the 2.2
  client attaches the key header to the Gemini host only and supports absolute URLs — the
  rule §6.5 pins is satisfied; no credential ever goes to a non-Google host).
- SDK shape: `client.files.download(file=video_output.uri, destination="output.mp4")` [A].
- Files API usage rules [G]: "The Files API lets you store up to 20 GB of files per project,
  with a per-file maximum size of 2 GB. Files are stored for 48 hours." and "you can't
  download user-uploaded files. You can download files generated by models, such as videos,
  using the `files.download` method. The Files API is available at no cost in all regions
  where the Gemini API is available." → **download promptly after ACTIVE**; `expireTime` [I]
  is the machine-readable deadline.
- Post-download: ffprobe the file — manifest durations come from probed values, never
  API-reported ones (§1.2 global constraint; the 2.3 TTS provider already pins this pattern).

## 5. Per-second pricing [C]

From the raw pricing markdown (fetched 2026-11-06), verbatim numbers:

| Model | Free tier | Input | Output text | Output video | Per-second math |
|---|---|---|---|---|---|
| `gemini-omni-1.2-flash` | **Not available** | $1.50/1M (text/image/video) | $9.00/1M | **$12.00/1M** | 5,792 tok/s × $12/1M = **$0.0695/s at 720p** |
| `gemini-omni-1.1-flash` | Not available | $1.50/1M | $9.00/1M | $17.50/1M | 5,792 tok/s × $17.50/1M = **$0.1014/s at 720p** (the 0.102 row shipped in 2.0) |

The 1.2 footnote gives the per-resolution breakdown directly: "Billing is based on total
output token consumption, calculated at a rate of 5,792 tokens per second of 720p video.
Under Standard pricing ($12.00 per 1M tokens), this equates to an effective price of
approximately **$0.07 per second (~$0.70 for a 10s clip, ~$1.04 for a 15s clip; $0.023/s for
360p at 1,931 tokens/s; $0.104/s for 1080p with upsampling at 8,688 tokens/s; and $0.209/s
for 4K with upsampling at 17,376 tokens/s**)." Recomputed exactly: 360p $0.023172/s, 720p
$0.069504/s, 1080p $0.104256/s, 4K $0.208512/s. This resolves task 2.0's open question #2
(1080p/4K billing was undocumented at 2.0 time).

Notes for the 4.1 cost model:
- Output tokens **include thinking tokens** ("Output price (including thinking tokens)" [C]);
  input tokens are negligible per clip (a ~200-char prompt ≈ 50 tok ≈ $0.000075).
- The unit in `prices.json` is `usd_per_second` and estimates **ceil fractional seconds up**
  (cost.py convention) — conservative and correct against "total output token consumption"
  billing.
- Resolution choice is the real cost lever: our platforms.json video targets are 1080p
  (1920×1080 / 1080×1920) [L]. Options: request `resolution:"1080p"` and bill $0.105/s, or
  request 720p ($0.07/s) and let assembly upscale to platform dims with ffmpeg (1.12 already
  scales). The docs mark both 1080p and 4k "(upscaled)" — i.e. the API itself renders 720p
  and upscales, so local upscaling at a 33 % discount is a defensible default. **My
  recommendation (flagged as such): generate 720p, upscale in assembly, price
  `usd_per_second: 0.07`; if 4.1 chooses native 1080p, price 0.105.** Whatever is chosen, the
  estimate must be derived from the resolution actually requested.
- `prices.json` still ships `gemini-omni-1.1-flash` @ 0.102 with a note to re-verify at 4.1
  [L]. Flipping that row (model → 1.2, price → 0.07, note updated) is **4.1 work with its
  test_cost.py pin update** — deliberately not done in this read-only spike.

## 6. Safety and limits [A, D, E, K]

- **Watermark:** "All generated videos include SynthID watermarking, which is invisible to
  viewers but can be detected programmatically for provenance verification." [A]
- **Filters:** "Omni applies content safety filters to both input prompts and generated video
  (which vary by region). Prompts that violate usage policies are blocked." [A] The
  safety-settings guide's configurable-threshold machinery has no video modality row (the
  page's only "video" mention is an unrelated video-game-dialogue example) [K] → treat video
  safety blocks as **fixed, not tunable**. Task 2.0's error taxonomy already classifies the
  safety family as permanent — a blocked prompt will not succeed on retry without a prompt
  change.
- **Regions:** editing/extending *uploaded* videos is "not currently available for users in
  the European Economic Area (EEA), Switzerland, and the United Kingdom" (extending
  model-generated videos works everywhere the API is available); uploading/editing images
  containing minors or certain recognizable people is not supported in EEA/CH/UK [A]. All
  generated output is region-filtered to some degree [A].
- **Language:** "English (EN) is fully supported, but other languages have not been
  evaluated" [A] — matches the app's English-only v1 constraint (§2).
- **Clip duration:** 1.2 renders **3–15 s** (interpolation and per-turn extension 3–10 s) at
  24 FPS [E]. Beats longer than 15 s cannot be produced as a single clip — they need
  segmentation-level splitting or an extension chain; task 4.1's request builder must reject
  or split them. (Extension rules: end-of-clip only, input ≤10 s for uploads, "extend by 10s"
  per turn, spoken-dialogue extension restrictions [A].)
- **Rate limits (hard ceilings for a BYOK Tier 1 project) [D]:** Gemini Omni Flash —
  Free tier "Not supported"; **Tier 1: 8 RPM / 40 RPD; Tier 2: 16/200; Tier 3: 40/2,000**.
  Sequential per-beat generation never approaches 8 RPM, but **40 clips/day** bounds a
  heavy video day at Tier 1; a job that exhausts RPD fails with retryable 429s that won't
  clear until the daily window rolls — per-asset retry (§6.6) plus partial completion is the
  correct behavior, and the UI should not promise same-minute retries then.
- **Spend limiter (reconfirms 2.0 §6):** Tier 1 = **$10 per 10 minutes**, violations return
  `429 RESOURCE_EXHAUSTED` [D]. At $0.07/s that needs ~140 s of 720p video in 10 minutes —
  reachable only with many long clips in rapid succession; the app's own per-job cap stays
  the primary guard, Google's 429s the backstop.
- **Latency:** the only official statement is "Video generation times vary based on
  duration, resolution, and current API load. Longer and higher-resolution videos take more
  time to generate." [A] — **no per-clip numbers are published anywhere I read.** The
  engineering consequence is §3.1's 60-second HTTP-close warning [F]: a unary POST that
  blocks past it is the failure mode to design against (short clips + URI polling now,
  `background:true` probe at 4.1). Concrete per-resolution timing must be measured with a
  real key at 4.1 and recorded in PROJECT_LOG; I could not verify it here.

## 7. Provider design recommendations for task 4.1 (my proposals, flagged as such)

1. **Transport:** one stateless `POST /v1beta/interactions` per beat via the 2.2
   `GeminiClient` (no `previous_interaction_id`, no turn chaining) — keeps per-asset retry an
   idempotent re-POST exactly like the 2.3/3.1 providers. Always
   `response_format: {"type":"video","aspect_ratio":<from format>,"resolution":"720p",
   "delivery":"uri"}`; no `task` (prompt-first per docs); no `store`/`stream`/`background`
   overrides on v1 (docs' defaults), with `background:true` as the documented fallback probe
   if unary POSTs drop connections on long clips (§3.1 ambiguity disclosed).
2. **Poll → download → probe:** capture `content[].uri` from the creation response and
   persist it (§3.2 gotcha); poll `GET /v1beta/files/{id}` at 5 s with a bounded budget
   (docs' own interval; cap ~15 min until measured data exists), `FAILED` → permanent
   GeminiError; on `ACTIVE` download via `client.download` (key only to the Gemini host),
   ffprobe the MP4, manifest takes the probed duration. Files expire in 48 h — irrelevant at
   our prompt-download latency, but the download must not be deferred to a later session.
3. **Prompts:** per-beat prompt = fixed style block (M3 anchoring pattern) + beat
   img-hint/text + **single-scene instruction** ("Continuous, unbroken shot… no scene cuts")
   + audio description ("No dialogue" — our assembly discards the generated track in favor of
   per-beat TTS; instructing neutral ambient avoids lip-flap visuals). Negatives in-prompt;
   never send `negative_prompt`.
4. **Duration/consistency options:** reject beats >15 s at request-build time (fail loud with
   the fix — split the beat); optional first-frame anchoring = attach the beat's M3 image
   with `task:"image_to_video"` if prompt-only consistency underperforms (docs: use `task`
   only when prompting fails). Extension chains exist but add statefulness — out of scope
   for v1.
5. **Cost:** `usd_per_second` from the resolution actually requested (0.07 @ 720p, 0.105 @
   1080p — §5), ceil seconds, estimate before confirmation, hard cap before each paid call —
   all existing §6.6 machinery, no new patterns.
6. **`prices.json` update:** switch the video row to `gemini-omni-1.2-flash` with the §5
   derivation in the note and a recheck entry for the 2026-12-03 1.1 routing date; flip
   `test_cost.py`'s pinned row in the same commit. (4.1's change, not this spike's.)

## 8. Could not verify (nothing here is guessed)

1. **Per-clip generation latency in seconds** — undocumented; only the qualitative statement
   [A] and the 60 s HTTP-close warning [F]. Measure with a real key at 4.1.
2. **`background:true` acceptance for Omni models specifically** — support list on the
   background page names text models/agents only [F], while the Omni best-practices section
   implies the parameter exists [A]. Docs-contradiction disclosed; probe at 4.1.
3. **Extension total-length discrepancy** — model page: "per-turn video extension up to 30s
   total" [E]; guide: "You can extend videos by 10s, up to a total length of 40s" [A]. Both
   quoted verbatim; irrelevant to v1 (no extension chains).
4. **360p/4K availability per aspect ratio** — the guide's resolution table does not state a
   ratio×resolution matrix [A]; assume all four resolutions work at both ratios but verify at
   4.1 before offering them in the UI.
5. **Whether `delivery:"uri"` changes billing or adds latency** — nothing suggests it does
   (billing is output-token based [C]); unverified empirically.
6. No live API call was made, so no observed latency, no real response bodies, and no
   confirmation that today's documented shapes are served verbatim.

## 9. Sources

- **[A]** Generate and edit videos with Gemini Omni Flash — https://ai.google.dev/gemini-api/docs/omni — page-stated "Last updated 2026-09-23 UTC" (fetched 2026-11-06 via web reader)
- **[B]** Video generation in the Gemini API (overview; Veo shutdown notice) — https://ai.google.dev/gemini-api/docs/video (canonical https://aistudio.google.com/docs/video; fetched 2026-11-06)
- **[C]** Pricing — raw markdown https://aistudio.google.com/docs/pricing.md — Omni 1.2 and 1.1 sections (fetched 2026-11-06)
- **[D]** Rate limits — raw markdown https://aistudio.google.com/docs/rate-limits.md — Omni rate table + spend-based limits (fetched 2026-11-06)
- **[E]** Gemini Omni Flash model page — raw markdown https://aistudio.google.com/docs/models/gemini-omni-flash.md — page-stamped "Latest update: November 2026" (1.2) / "August 2026" (1.1) (fetched 2026-11-06)
- **[F]** Background execution — raw markdown https://aistudio.google.com/docs/background-execution.md (fetched 2026-11-06)
- **[G]** File input — Files API — raw markdown https://aistudio.google.com/docs/files.md (fetched 2026-11-06)
- **[H]** Webhooks (preview) — raw markdown https://aistudio.google.com/docs/webhooks.md (fetched 2026-11-06)
- **[I]** Files API get reference — raw markdown https://aistudio.google.com/api-reference/files/get-file.md (fetched 2026-11-06)
- **[K]** Safety settings — raw markdown https://aistudio.google.com/docs/safety-settings.md (fetched 2026-11-06; checked for a video modality row — none)
- **[L]** Repo files read this session: `app/data/platforms.json` (all 8 `kind:"video"` formats are 16:9 1920×1080 or 9:16 1080×1920), `app/data/prices.json` video row (gemini-omni-1.1-flash, usd_per_second 0.102, verified true)
- Prior spikes: `research/gemini-api.md` (task 2.0), `research/gemini-image.md` (task 3.0)
