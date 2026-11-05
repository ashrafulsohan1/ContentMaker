# Gemini image research — BUILD_PLAN task 3.0 (research spike)

**Date of research:** 2026-11-06
**Method:** read-only web research against official Google documentation (primary sources),
fetched with the web reader tool (plus `curl` against the raw-markdown `.md` endpoints the
docs expose). No live API calls were made (no image-capable spend authorized, and the spike
is mandated to be read-only / zero-spend). Every fact below cites the page it came from, with
that page's own "Last updated" stamp where shown. Anything I could not verify is stated as
such in §7 — nothing in this file is a guess.

**Relation to task 2.0:** `research/gemini-api.md` (task 2.0 spike) already established the
Interactions API surface, auth, error shapes, and a first pass at image pricing. It asked this
spike (its §3.2) to evaluate the style-consistency mechanisms in depth. This file goes deeper
on exactly the three things task 3.0 names: (1) current image model IDs, (2) dimension/size
control, (3) style-consistency mechanisms — seed, reference image, style anchoring.

---

## TL;DR — decisions for tasks 3.1 / 3.2

| Question | Conclusion |
|---|---|
| Default image model | **`gemini-nano-banana-2.1`** (Nano Banana 2.1) — current workhorse, 1K/2K/4K, 14 aspect ratios, best multi-turn consistency, GA stable, no shutdown announced [A, E, F] |
| Premium/backup | `gemini-3-pro-image` (Nano Banana Pro) — only model with a first-class **style-reference** bucket (up to 3) and "accurate brand consistency"; ~2.7× the per-image price [A, C, G] |
| Dimensions | Control via `response_format: {type:"image", mime_type, aspect_ratio, image_size}` — `image_size` is `1K`/`2K`/`4K` (uppercase K mandatory, lowercase rejected). **Always set both fields explicitly**: default behavior matches the input image's size or falls back to a 1:1 square [A] |
| Resolution tier | **Request `image_size: "2K"` for every render.** 1K is undersized on every format in our `platforms.json` (§3.3); assembly downscales 2K → platform dims. Cost $0.0504/image on 2.1 [A, C] |
| Seed | **No `seed` parameter is documented anywhere on the current image path.** Deterministic regeneration is not an offered API mechanism (§5.1); third-party reports conflict and are unverified. Task 3.1's "seed" leg must be designed as "fixed style block + reference anchor," not a literal seed param |
| Style consistency | Three documented mechanisms: **(1) reference images** (up to 14; per-model buckets incl. 4 characters on 2.1 and 3 style refs on Pro), **(2) multi-turn editing** via `previous_interaction_id`, **(3) a fixed, hyper-specific style block** at the head of every prompt (docs' own template pattern). Recommended design for 3.1: stateless per-call = fixed style block + beat-1 image re-attached as reference on every later beat (§6) |
| Text overlay (3.2) | Generate art **without** baked overlay text; draw overlay locally (ffmpeg/Pillow) — keeps text pixel-exact, editable, and avoids re-spend when only the text changes. (My recommendation, flagged as such — the docs support both paths §4.4) |

---

## 1. Current image models (Nano Banana family)

Source [A] = image-generation guide ("Last updated 2026-10-09 UTC"); lifecycle from [E]
(deprecations, raw markdown fetched 2026-11-06); model-card details from [F] and [G].

| Model ID | Name | Released | Shutdown | Free tier | Role per docs |
|---|---|---|---|---|---|
| `gemini-nano-banana-2.1` | Nano Banana 2.1 | 2026-10-06 | none announced | No | "primary high-efficiency workhorse model for image generation and conversational editing" [A]; "latest high-efficiency image generation and conversational editing model" [F] |
| `gemini-3.1-flash-image` | Nano Banana 2 | 2026-05-28 | none announced (recommended replacement: 2.1) | No | "previous-generation high-efficiency workhorse"; adds a 512px tier 2.1 lacks [A] |
| `gemini-3.1-flash-lite-image` | Nano Banana 2 Lite | 2026-06-30 | none announced | No | "fastest and cheapest"; **1K only**; "not optimized for multiple reference inputs or multi-turn sequential editing" [A] |
| `gemini-3-pro-image` | Nano Banana Pro | 2026-05-28 | none announced | No | "premium choice for the most complex visual tasks… highest level of world knowledge, advanced localization, accurate brand consistency, and precision creative control" [A] |
| `gemini-2.5-flash-image` | Nano Banana (legacy) | 2025-10-02 | **2027-03-15** | No | deprecated; recommended replacement `gemini-3.1-flash-lite-image` [E]. This is the ID currently in `app/data/prices.json` — task 3.1 must not use it |
| Imagen (`imagen-*`) | — | — | shut down (4.0 family 2026-08-17) | — | "Imagen models are shut down. Use Nano Banana models for all image generation tasks." [A, E] |

Model-card specifics worth knowing for the provider ([F] for 2.1, [G] for Pro):
- **2.1 inputs:** Text, Image, Video, and PDF. **Output:** Image and Text. Token limits: input
  131,072 / output 32,768. Capabilities matrix: image generation ✅, Batch API ✅, search
  grounding ✅, thinking ✅; caching, code execution, function calling, structured outputs,
  flex/priority inference ❌.
- **Pro inputs:** Image and Text only (no video/PDF). Token limits: input 65,536 / output
  32,768. Same capability matrix.
- 2.1 fixed "tiling artifacts on wide and panoramic aspect ratios (`1:4`, `4:1`, `1:8`, `8:1`)
  at `2K` and `4K`" — the reason it exposes 14 ratios at full resolution where Pro exposes 10 [F].
- All generated images carry a **SynthID watermark** (every current model) [A].

---

## 2. Dimension / size control

### 2.1 The control surface [A]

Everything dimension-related lives in `response_format` when `type: "image"`:

```json
"response_format": {
  "type": "image",
  "mime_type": "image/jpeg",          // or "image/png"
  "aspect_ratio": "9:16",             // see §2.2 table
  "image_size": "2K"                  // "1K" | "2K" | "4K" (+ "0.5K" on 3.1-flash-image)
}
```

- Defaults (docs, "Aspect ratios and image size"): *"By default, the model matches the output
  image size to that of your input image, or otherwise generates 1:1 squares"* and *"Gemini 3
  image models generate 1K images by default."* → **the provider must always send both
  `aspect_ratio` and `image_size` explicitly**; silent defaults are wrong for every platform
  format we ship.
- Uppercase `K` is mandatory: *"You must use an uppercase 'K' … Lowercase parameters (e.g.,
  1k) will be rejected."* [A] (task 2.0 recorded the same; reaffirmed on today's page text).
- Pixel dimensions are **not** free-form: you pick a supported ratio + tier, and the API
  returns exactly the pixel size from its table (below). Arbitrary sizes like 1080×1920 are
  not requestable — assembly must downscale/crop from the nearest supported size.
- `response_format` may also be an array (`[{"type":"text"},{"type":"image"}]`) to request
  text+image together, or a single image entry to suppress the conversational text [A].

### 2.2 Supported ratios and exact pixel sizes — `gemini-nano-banana-2.1` [A]

14 ratios; every ratio exists at 1K/2K/4K. Tokens per image are constant per tier:
1K = 1120, 2K = 1680, 4K = 2520 (guide table; see §2.4 for a 4K token discrepancy on the
pricing page).

| Ratio | 1K | 2K | 4K |
|---|---|---|---|
| 1:1 | 1024×1024 | 2048×2048 | 4096×4096 |
| 9:16 | 768×1376 | 1536×2752 | 3072×5504 |
| 16:9 | 1376×768 | 2752×1536 | 5504×3072 |
| 4:5 | 928×1152 | 1856×2304 | 3712×4608 |
| 5:4 | 1152×928 | 2304×1856 | 4608×3712 |
| 3:4 | 896×1200 | 1792×2400 | 3584×4800 |
| 4:3 | 1200×896 | 2400×1792 | 4800×3584 |
| 2:3 | 848×1264 | 1696×2528 | 3392×5056 |
| 3:2 | 1264×848 | 2528×1696 | 5056×3392 |
| 21:9 | 1584×672 | 3168×1344 | 6336×2688 |
| 1:4 | 512×2048 | 1024×4096 | 2048×8192 |
| 4:1 | 2048×512 | 4096×1024 | 8192×2048 |
| 1:8 | 384×3072 | 768×6144 | 1536×12288 |
| 8:1 | 3072×384 | 6144×768 | 12288×1536 |

Other models [A]: `gemini-3.1-flash-image` has the **same 14 ratios** plus a 512px tier
(e.g. 9:16 @512px = 384×688, 747 tokens); `gemini-3-pro-image` has the **10 standard ratios
only** (no 1:4/1:8/4:1/8:1) at the same 1K/2K/4K pixel sizes, with tokens 1120 (1K/2K) and
2000 (4K); `gemini-2.5-flash-image` (dying) is 10 ratios at ~1024px-class sizes, 1290 tokens.

### 2.3 Fit check against `app/data/platforms.json` (read this session, lines 12–64)

| Platform format (file:line) | Target px | Nearest 2.1 size @2K | Covers? | @1K |
|---|---|---|---|---|
| youtube_shorts / tiktok / reels / stories (9:16) | 1080×1920 | 1536×2752 | ✅ downscale | 768×1376 ❌ |
| instagram_feed_square 1:1 (`platforms.json:28`) | 1080×1080 | 2048×2048 | ✅ | 1024×1024 ❌ (56 px short) |
| linkedin_carousel 1:1 (`platforms.json:57`) | 1200×1200 | 2048×2048 | ✅ | 1024×1024 ❌ |
| instagram 4:5 portrait/carousel (`platforms.json:29-30`) | 1080×1350 | 1856×2304 | ✅ | 928×1152 ❌ |
| 16:9 video-frame + X/FB/LinkedIn images (`platforms.json:12,38-39,47-48,55-56`) | 1920×1080 / 1200×675 / 1200×630 / 1200×627 | 2752×1536 | ✅ | 1376×768 ❌ (<1920 wide) |
| pinterest_pin 2:3 (`platforms.json:64`) | 1000×1500 | 1696×2528 | ✅ | 848×1264 ❌ |

**Conclusion: 1K is undersized on every format we ship; `image_size: "2K"` on every render,
assembly downscales to platform dims.** (Same conclusion task 2.0 reached from a partial
table — now verified against the full 14-ratio table and our actual `platforms.json`.)
4K is never needed: no platform target exceeds a 2K size, and it costs ~2.2× a 2K image.

### 2.4 Image pricing (Standard tier) [C — raw markdown `aistudio.google.com/docs/pricing.md`]

Billing unit is **output image tokens** (per 1M), independent of aspect ratio within a tier:

| Model | Input | Image output | Per-image (page-stated) | Batch |
|---|---|---|---|---|
| `gemini-nano-banana-2.1` | $1.50/1M | **$30.00/1M** | $0.0336 (1K) · **$0.0504 (2K)** · $0.113 (4K) | half ($0.0252 per 2K) |
| `gemini-3.1-flash-image` | $0.50/1M | $60.00/1M | $0.045 (0.5K) · $0.067 (1K) · $0.101 (2K) · $0.151 (4K) | n/a (no batch tab) |
| `gemini-3.1-flash-lite-image` | $0.25/1M | $30.00/1M | $0.0336 (1K only) | half |
| `gemini-3-pro-image` | $2.00/1M (≈$0.0011/img) | $120.00/1M | $0.134 (1K/2K) · $0.24 (4K) | n/a |
| `gemini-2.5-flash-image` (dying) | $0.30/1M | $30.00/1M | $0.039 | half |

- No free tier on **any** current image model — a paid (billing-enabled) project is required
  (page lists "Free Tier: Not available" for all image models) [C].
- Text/thinking output on 2.1 bills separately ($7.50/1M) — small but nonzero: the model
  usually emits a sentence of conversational text alongside the image unless
  `response_format` requests image-only [A, C].
- **Discrepancy found (recorded, not resolved):** the pricing page footnote says a 2.1 4K
  image "consume[s] 3780 tokens … equivalent to $0.113" while the image guide's table lists
  2520 tokens for every 4K ratio [A vs C]. The official per-image figure to budget is the
  pricing page's $0.113. Irrelevant at our 2K target (1680 tokens agrees on both pages);
  prices.json keeps only the 2K figure, so no config change needed.
- Worked cost model for us: an 8-beat piece at 2K on 2.1 ≈ 8 × $0.0504 = **$0.40 per job**;
  on Pro ≈ 8 × $0.134 = $1.07. Well inside the §6.6 confirm-and-cap flow.
- Rate-limit shape [D — raw markdown, fetched 2026-11-06]: image models are metered like
  other models (RPM/TPM/RPD per project, plus an "IPM" images-per-minute analogue specific
  to Nano Banana); spend-based limits exist per tier ($10 per rolling 10 min on Tier 1 →
  `429 RESOURCE_EXHAUSTED`); RPD resets midnight Pacific. Numeric per-model RPM/IPM is no
  longer published as a static table — visible only per-account in AI Studio (same gap task
  2.0 recorded). Grounding searches: 5,000/month free shared across Gemini-3+ models, then
  $14/1k, daily cap 1,500 — but we don't ground images (§4.3).

---

## 3. API mechanics the image provider rides on

- Same endpoint as TTS (task 2.2's `GeminiClient`): `POST
  https://generativelanguage.googleapis.com/v1beta/interactions`, auth via
  `x-goog-api-key` header; image comes back in `steps[] → model_output → content[]` blocks
  with `type: "image"` and base64 `data`; `interaction.output_image` is an SDK-only
  convenience [A, and gemini-api.md §1.1].
- **Multi-turn editing** passes `previous_interaction_id`; the docs call multi-turn
  conversation "the recommended way to iterate on images" [A].
- **`tools`, `system_instruction`, and `generation_config` are interaction-scoped** — they
  are *not* carried across `previous_interaction_id` turns and must be re-sent on every call [B].
- Server-side storage: interactions are stored by default (`store=true`); paid tier retains
  them **55 days**; `store=false` opts out **but is incompatible with
  `previous_interaction_id`** and with background execution. Stored interactions can be
  deleted programmatically by ID [B]. → BYOK note for 3.1: stateless per-call requests
  (`store` default + no chaining) still store content 55 days server-side; the app should
  surface this or prefer stateless mode deliberately (§6).
- Thinking on image models is always on and cannot be disabled [A]. Levels: 2.1
  minimal/medium/**high=medium default**; 3.1-flash and lite minimal/high, default minimal.
  The model "generates up to two interim images" while thinking; thought *images* are not
  charged but thinking *tokens* are billed by default [A]. Practical latency/cost note: for
  eight-beat jobs, `thinking_level: "minimal"` on 2.1 is a legitimate cost/latency lever the
  provider can expose as an option (quality trade-off — 3.1-flash ships `minimal` as its
  default, so it's a sanctioned setting, not a hack).
- Batch API is supported on the image model cards [F, G] and the image guide advertises it
  (higher limits, ≤24 h turnaround) [A] — but the Interactions overview says Batch is *not
  yet* available on the Interactions API [B]. Not needed for per-beat interactive flow;
  recorded for completeness.

---

## 4. Style-consistency mechanisms (the core of task 3.0)

### 4.1 Seed — **not a documented mechanism on the current image path**

I searched for a seed/reproducibility knob on the image models. Findings:

- The full image-generation guide (fetched in its entirety today; stamp 2026-10-09)
  **never mentions a `seed` parameter**. Its "Optional configurations" section names exactly
  three knobs — output format, aspect ratio, image size — plus `generation_config`
  `thinking_level` [A].
- The Interactions API overview describes `generation_config` as "including `thinking_level`,
  `temperature`, etc." with no seed [B]; the general prompt-design guide discusses
  temperature/topK/topP for *text* determinism only, with no image-determinism claim [H].
- Third-party sources conflict and are **unverified**: a GitHub issue on
  GoogleCloudPlatform/vertex-ai-creative-studio claims Gemini 2.5 Flash Image accepted a
  `seed` via API (and asks for it to be surfaced in that studio UI); a third-party help
  article claims Nano Banana Pro "doesn't officially support the seed parameter" and that
  every generation is a fresh random draw. The 2.5 model those positive reports describe is
  deprecated (shuts down 2027-03-15) [E]. I could not verify any of this against a primary
  Google doc, and I made no live API call to probe it (zero-spend spike).
- **Conclusion for task 3.1:** design consistency *without* assuming seed. Do not accept a
  user-facing "seed" option that silently does nothing; if a seed field is ever tried
  empirically at task 3.5's consistency check, treat it as an undocumented extra, not a
  contract.

### 4.2 Reference images — the primary API-level consistency mechanism [A]

"Mix up to 14 reference images" per request (as plain `{"type":"image", mime_type, data}`
blocks in `input`, alongside the text). The docs split the 14 into per-model *roles*:

| Model | Objects (high-fidelity) | Characters (consistency) | **Style references** |
|---|---|---|---|
| 3.1 Flash Lite | up to 14 | N/A | N/A |
| **Nano Banana 2.1 & 3.1 Flash** | up to 10 | **up to 4** | N/A (no dedicated bucket) |
| **Nano Banana Pro (3 Pro Image)** | up to 6 | up to 5 | **up to 3** |

- A dedicated "style reference" bucket exists **only on Pro** — the one mechanism Google
  documents for "paint everything in *this* style." On 2.1 the same effect is achieved by
  passing the anchor image as an ordinary reference: the editing docs state the model
  *"will match the original image's style, lighting, and perspective"* when working from a
  provided image [A], and the style-transfer section is just reference-image + instruction.
- "High-fidelity" is the docs' word for object/logo/character preservation: the limitation
  section repeats "character resemblance of up to 4 characters and the fidelity of up to 10
  objects in a single workflow" for 2.1 / 3.1-flash [A]; the 2.1 model card confirms "multi
  -image fusion supporting up to 14 reference images (character consistency for up to 4
  characters and object fidelity for up to 10 objects)" [F].
- Character-consistency workflow documented under "Character consistency: 360 view": keep
  prompting different angles **"include previously generated images in subsequent prompts to
  maintain consistency"**; for complex poses, include a pose reference [A]. This is exactly
  the beat-N-references-beat-1 pattern our style anchor needs, documented as intended usage.

### 4.3 Multi-turn conversational editing [A, B]

`previous_interaction_id` chains turns ("Update this infographic to be in Spanish. Do not
change any other elements."). Docs push it as the recommended iteration loop and 2.1
specifically advertises "improved … multi-turn character consistency" [A, F].

Costs of building the provider on it (why §6 recommends against chaining for our pipeline):
server-side state (55-day retention, `store=true` required), `store=false` disables chaining,
params like `response_format`/`generation_config` must be re-sent anyway (interaction-scoped
[B]), a regenerated beat-1 breaks every downstream chain ID, and per-asset retry (§6.6)
becomes stateful coupling instead of an idempotent re-POST. Chaining is great in a chat UI;
it is the wrong dependency for a batch job engine.

### 4.4 Style anchoring via a fixed style block — doc-sanctioned prompting pattern [A, H]

The docs' own prompting guide is the evidence that a **fixed, hyper-specific style preamble**
is the sanctioned anchoring technique:

- "Stylized illustrations & stickers" template: *"A [style] of a [subject] doing [activity].
  The design features [visual qualities, e.g., bold outlines, cel-shading, etc.] and
  [color/background preference]"* — with the note: *"Describe the artistic style, subject,
  and medium. Be specific about the visual detail (bold lines, colors, etc.) **for consistent
  results**"* [A].
- Best practices: "Be hyper-specific" (style adjectives enumerated in detail), "Provide
  context and intent", "Iterate and refine" ("Keep everything the same, but …"), "Use
  step-by-step instructions", "Use semantic negative prompts", "Control the camera" [A].
- Gemini-3 prompting principles: put constraints/role/format "at the very beginning of the
  user prompt"; use one consistent structural format (XML-style tags or Markdown headings)
  within a prompt [H]. → a `<style>…</style>` block at the head of every image prompt is
  aligned with both pages.
- Text-in-image guidance cuts both ways for 3.2: Gemini is "advanced" at text rendering, but
  the limitation section warns "the model won't always follow the exact number of image
  outputs" and recommends generating the text first, then the image, for text-heavy art [A].
  For *platform overlay* text (handles, captions) the robust path is drawing it locally:
  pixel-exact, platform-safe, and a text change costs $0 instead of a re-roll. (My
  recommendation, not a doc quote — the overlay decision is task 3.2's, per BUILD_PLAN
  "ffmpeg drawtext vs Pillow — decided on spike data"; the spike data says: either local
  tool works, do NOT bake overlay text into the generation prompt.)

---

## 5. Recommended provider design for task 3.1 (`app/providers/gemini/image.py`)

Derived from the above; each line traces to a section here.

1. **Model from `prices.json`** (like TTS in 2.3): default `gemini-nano-banana-2.1`,
   `gemini-3-pro-image` selectable for "brand/consistency premium" jobs; never hardcode IDs
   (BUILD_PLAN §0 rule; `prices.json` already carries the 2.1 2K figure).
2. **One stateless POST per beat** (task 2.2 client): `input = [text, optional ref images…]`,
   `response_format = {type:image, mime_type, aspect_ratio, image_size:"2K"}`,
   `generation_config.thinking_level` optional. No `previous_interaction_id`, no grounding
   tools (avoids search display requirements and grounding spend [A]).
3. **Aspect-ratio map**: platform `aspect [w,h]` → API ratio string via gcd reduction, with a
   fixed table of the 14 supported ratios as validation (§2.2); reject unsupported ratios at
   estimate time with a "exactly what's wrong" error (§6.5 error-shape style), don't let the
   API 400 mid-job.
4. **Style consistency package** per job: (a) fixed style block — one short, hyper-specific
   paragraph from the user's preset, prepended verbatim to every beat prompt, phrased as the
   docs' template (style + medium + palette + background + camera); (b) **style anchor** —
   beat 1 renders first; every later beat re-attaches beat 1's PNG as a reference image with
   an instruction to match its style/palette/lighting while depicting the new beat content
   (docs: provided-image edits match "style, lighting, and perspective" [A]); (c) characters
   ≥2? cap reference usage at the model's character bucket (4 on 2.1) and say so in options
   copy. This combination is the "seed/reference as supported" reading of task 3.1: reference
   supported, seed not offered (§4.1).
5. **Retry = re-POST** the identical request (stateless), so §6.6 bounded retries and
   per-asset regen work unchanged; a regenerated beat N re-downloads only beat N.
6. **Cost estimate**: beats × prices.json per-image figure at the requested tier (2K default),
   plus the fixed per-beat text/thinking pennies if we leave text output enabled; cheaper to
   request image-only `response_format` [A].
7. **Dimensions contract**: provider returns the image bytes plus the *actual* pixel size;
   `assembly/images.py` (3.2) downscales to the platform's `resolution` from
   `platforms.json` — center-crop only if aspect drifts (it shouldn't; the ratio table is
   exact).
8. **BYOK/privacy surface**: disclose that interactions are stored server-side 55 days on the
   paid tier [B]; consider a "delete after job" sweep using the documented delete-by-ID [B]
   as a later hardening, not a 3.1 blocker.

---

## 6. What I could not verify (kept out of conclusions)

1. **`seed` on the legacy `generateContent` surface**: I did not fetch the generateContent
   REST reference (docs toggle; the Interactions shapes above are complete for our provider).
   Third-party posts claim a seed exists there for the *deprecated* 2.5 image model; unverified.
2. **2.1 4K token count** — 2520 (guide table [A]) vs 3780 (pricing footnote [C]). Flagged in
   §2.4; irrelevant at our 2K tier.
3. **Per-account RPM/IPM numbers** for image models — not published statically; only visible
   in AI Studio per account [D].
4. **`store=false` field name on the image path** — the overview documents the concept [B];
   I did not fetch a request-schema reference to confirm exact JSON casing. If 3.1 wants the
   opt-out, verify the field on the wire before relying on it.
5. **Live behavior of style anchoring quality** — everything in §4 is documentation-grounded;
   actual visual consistency is task 3.5's empirical check (BUILD_PLAN §3, "3-beat sample
   run, style regression reviewed visually").

## 7. Sources

All fetched 2026-11-06 via the web reader tool (full page content) or `curl` on the `.md`
raw-markdown endpoints:

- **[A]** Nano Banana image generation — https://ai.google.dev/gemini-api/docs/image-generation — page-stated "Last updated 2026-10-09 UTC"
- **[B]** Interactions API overview — https://ai.google.dev/gemini-api/docs/interactions — page-stated "Last updated 2026-09-23 UTC"
- **[C]** Gemini Developer API pricing — raw markdown https://aistudio.google.com/docs/pricing.md (canonical https://ai.google.dev/gemini-api/docs/pricing); no page date shown
- **[D]** Rate limits — raw markdown https://aistudio.google.com/docs/rate-limits.md; no page date shown
- **[E]** Deprecations — raw markdown https://aistudio.google.com/docs/deprecations.md (canonical https://ai.google.dev/gemini-api/docs/deprecations); no page date shown
- **[F]** Gemini Nano Banana 2.1 model card — https://aistudio.google.com/docs/models/gemini-nano-banana-2.1.md — "Latest update: October 2026"
- **[G]** Gemini 3 Pro Image model card — https://aistudio.google.com/docs/models/gemini-3-pro-image.md — "Latest update: November 2025"
- **[H]** Prompt design strategies — https://ai.google.dev/gemini-api/docs/prompting-strategies — page-stated "Last updated 2026-10-13 UTC"
- Repo material read: `research/gemini-api.md` (task 2.0 spike — API surface context and the §3.2 hand-off note), `app/data/platforms.json` (§2.3 fit check), `BUILD_PLAN.md` §2/§3 (task row 3.0, global constraints)
- Third-party, **unverified**, cited only in §4.1 as conflicting reports: github.com/GoogleCloudPlatform/vertex-ai-creative-studio issue #914; help.apiyi.com Nano Banana Pro seed article; r/generativeAI thread on closed-model seeds. Found via web search, not used for any conclusion.
