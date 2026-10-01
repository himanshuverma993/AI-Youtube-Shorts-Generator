# OpenRouter — Free + Agentic AI for This Repo

Research date: **1 Oct 2026**. Every spec below was read live from OpenRouter's own
API (`/api/v1/models/<id>/endpoints`), model pages, and docs — not from third-party
blogs. Where something could not be confirmed on OpenRouter itself it is marked
**unverified**.

> **Correction log:** an earlier draft of this doc recommended `qwen/qwen3-coder:free`
> as the top pick. That was **wrong** — see §3. The endpoints API was then checked for
> every candidate, which is what caught it.

---

## 1. What this repo actually needs (measured, not guessed)

```
$ find . -type f \( -name "*.py" -o -name "*.md" -o -name "*.txt" ... \) | xargs cat | wc -c
386912 chars  →  ~96,728 tokens  (est. chars/4)
8,077 lines of Python
```

**Implication:** to hold the *entire* repo in one prompt you need **≥128K context**.
A 32K–64K model cannot see the repo whole — it can only crawl it file-by-file.

Largest files: `scripts/selftest_audit.py` (652), `scripts/verify_artifacts.py` (648),
`uploader_instagram.py` (465), `feedback.py` (456), `campaign_runner.py` (415),
`groq_client.py` (414).

**Honest note:** no free model "already knows" this repo — it is a private fork and is
in no training set. "Understanding the repo" means the model has (a) a context window
big enough to hold it and (b) strong repo-reasoning + tool use. Both are selectable.
Memorisation is not on the menu.

---

## 2. ✅ Recommendation: `nvidia/nemotron-3-ultra-550b-a55b:free`

Verified live from `GET /api/v1/models/nvidia/nemotron-3-ultra-550b-a55b:free/endpoints`:

| Field | Verified value |
|---|---|
| Model ID | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| Endpoint | `Nvidia \| nvidia/nemotron-3-ultra-550b-a55b-20260604:free` |
| Pricing | `"prompt":"0"`, `"completion":"0"` — **genuinely $0** |
| **context_length** | **1,000,000** → this repo (~97K tokens) fits **~10× over** |
| max_completion_tokens | 65,536 |
| supported_parameters | `reasoning`, `include_reasoning`, `temperature`, `max_tokens`, `seed`, `top_p`, **`tools`**, **`tool_choice`**, `reasoning_effort` |
| supports_tool_choice | `none` ✅ `auto` ✅ `required` ✅ `function` ✅ — **full agentic control** |
| status | `0` (active) |
| uptime | 30m **95.9%**, 5m **97.7%**, 1d **97.0%** |
| Architecture | MoE 550B total / 55B active, hybrid Transformer-Mamba |
| Vendor positioning | "long-running agentic workflows, including agent orchestration, coding agents, deep research" |

**Why it wins:** the only *live* free model combining a 1M window (whole repo in one
shot, no file crawling) with full `tools` + all four `tool_choice` modes. 6.5T tokens
last week, so it is heavily exercised rather than a neglected corner of the catalog.

---

## 3. ❌ Do not use `qwen/qwen3-coder:free`

Its spec sheet is perfect on paper — 1,048,576 context, "optimized for agentic coding
tasks such as function calling, tool use, and long-context reasoning over
repositories", $0. But:

```
GET /api/v1/models/qwen/qwen3-coder:free/endpoints
→ {"data":{ ..., "endpoints":[] }}      ← EMPTY
```

**No provider is currently serving the free variant.** The same call against
`nemotron-3-ultra:free`, `laguna-s-2.1:free`, `nemotron-3.5-lightning:free` and
`nemotron-3-super:free` all returned fully populated endpoint objects, so the empty
array is a real signal, not an auth artifact. It is catalogued but not callable —
requests would fail at routing.

---

## 4. Full verified comparison

All four rows below returned live endpoint data with `pricing.prompt == "0"`.

| Model ID | ctx | max out | `tools` | `tool_choice` modes | status | uptime 1d | extras |
|---|---|---|---|---|---|---|---|
| **`nvidia/nemotron-3-ultra-550b-a55b:free`** | **1,000,000** | 65,536 | ✅ | none/auto/required/function | `0` | 97.0% | `reasoning_effort` |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262,144 | **235,929** | ✅ | auto/required/function | `0` | 98.2% | **`structured_outputs`, `response_format`**, `reasoning_effort` |
| `poolside/laguna-s-2.1:free` | 262,144 | 32,768 | ✅ | **auto only** | `0` | **99.9%** | fp4 quant |
| ~~`nvidia/nemotron-3.5-lightning:free`~~ | 1,000,000 | 65,536 | ✅ | none/auto/required/function | **`-2`** ⚠️ | **89.1%** | 5m uptime 85.7% — avoid |
| ~~`qwen/qwen3-coder:free`~~ | — | — | — | — | **no endpoints** | — | dead, see §3 |

Also verified on the free-models collection page but **not** endpoint-checked:
`dots-studio/dots-3-note-preview:free` (512K), `qwen/qwen3.8-27b:free` (262K),
`openrouter/free` (auto-router — non-deterministic, bad for reproducible pipelines).

### Two things worth flagging

1. **262K vs 1M discrepancy resolved.** Nemotron 3 Super's marketing blurb claims a
   "1M token context window", but the endpoint object says `context_length: 262144`.
   Trust the endpoint value — 262K is what you can actually send.
2. **`poolside/laguna-s-2.1:free` privacy caveat**, quoted from its own card:
   *"If you are using Laguna S 2.1 for free, we may use your inputs and outputs to
   train and improve our models."* Relevant if this repo ever holds secrets.
3. **`stealth/space-bunny-alpha`** is the #1 free model by usage (31.4T tokens/wk, 1M
   ctx, $0/$0) but its card says **"Going away October 5, 2026"** — four days out.
   Do not wire a pipeline to it.

### Second choice, and why it matters for *this* repo

`nvidia/nemotron-3-super-120b-a12b:free` is the only live free model exposing
**`structured_outputs` + `response_format`**. This repo parses LLM JSON by hand —
`highlights.py:_parse_json_loose()` and the `_sanitize_highlights()` /
`_fallback_metadata()` guards in `metadata.py` exist precisely because models return
malformed JSON. Schema-enforced output would let those guards become assertions
instead of repair code. Trade-off: 262K context still fits the repo ~2.7× over, so
nothing is actually lost. Only 12B active params, so it should be faster than Ultra.

---

## 5. The catch you must plan around — free-tier rate limits

From OpenRouter's own docs (`/docs/api_reference/limits`), for any ID ending `:free`:

| Credits purchased (all time) | Requests / minute | Requests / day |
|---|---|---|
| Less than 10 | 20 | **50** |
| At least 10 | 20 | **1000** |

- **50 requests/day is the real constraint, not money.** An agentic loop that reads
  files, calls tools and re-plans can burn 20–50 requests in one session.
- One-time **$10** top-up → **1000/day** (20×). One-time, not a subscription. The docs
  note the higher ceiling is granted from **9 credits**.
- Multi-accounting does **not** help: *"Making additional accounts or API keys will not
  affect your rate limits, as we govern capacity globally."*
- Check quota: `GET https://openrouter.ai/api/v1/key` →
  `free_model_daily_requests.{used,limit,remaining}`.
- Over-limit returns HTTP **429** with `X-RateLimit-Limit/-Remaining/-Reset`; honour
  `Retry-After`, back off exponentially.
- ⚠️ A **negative balance breaks even free models**: *"If your account has a negative
  credit balance, you may see `402` errors, including for free models."*
- Uptime above is ~89–99%, i.e. **not 100%** — free tiers drop requests. This repo's
  existing circuit-breaker pattern (`_groq_healthy()`, `_trip_groq_breaker()`) is the
  right shape for that.

**Bottom line:** "free, no credit tension" is true for *tokens*, not unlimited for
*requests*. 50/day suits manual repo Q&A and a handful of pipeline runs; it does not
suit an unattended campaign loop.

---

## 6. How this slots into the existing code

The repo already has a 3-tier fallback ladder, so an OpenRouter tier drops in cleanly:

- `shorts_generator/groq_client.py:306` → `call_llm(prompt)` — ladder entry point
  (Groq → Cerebras → local llama.cpp). OpenRouter becomes a peer tier.
- `shorts_generator/groq_client.py:264` / `:272` → `call_groq_llm` / `call_cerebras_llm`
  are the per-provider shapes to copy.
- `shorts_generator/transcriber.py:294` → `transcribe(media_path, language)`.

OpenRouter is OpenAI-compatible, so a tier is a base-URL swap:
`https://openrouter.ai/api/v1` with `model="nvidia/nemotron-3-ultra-550b-a55b:free"`.

### Bonus find for the transcription stage

OpenRouter has **0 free transcription models** (verified via `?max_price=0` →
"Transcription 0"), so Whisper cannot move there at $0. But two are near-free:

| Model | Price | 2-hour podcast |
|---|---|---|
| `qwen/qwen3-asr-0.6b` | $0.000003 / second | **~$0.022** |
| `qwen/qwen3-asr-1.7b` | $0.000008 / second | ~$0.058 |

Both give segment-level and word-level timestamps (which `transcriber.py` needs — see
`_segments_from_verbose()`), 30 languages + 22 Chinese dialects. The existing local
`faster-whisper` CPU tier remains the only genuinely $0 option.

---

## 7. OpenRouter vs NVIDIA direct — same model, different throttle

**They are literally the same endpoint.** Verified earlier from
`GET /api/v1/models/nvidia/nemotron-3-ultra-550b-a55b:free/endpoints`:
`"provider_name":"Nvidia"`, `"tag":"nvidia"`. OpenRouter is a **proxy**, not a second
deployment. So "switch to NVIDIA for a better model" is a category error — you get the
identical weights either way. What changes is the **rate-limit layer on top**.

| | OpenRouter `:free` | NVIDIA direct (build.nvidia.com) |
|---|---|---|
| Model ID | `nvidia/nemotron-3-ultra-550b-a55b:free` | `nvidia/nemotron-3-ultra-550b-a55b` (no `:free`) |
| Base URL | `https://openrouter.ai/api/v1` | `https://integrate.api.nvidia.com/v1` ✅ verified from NVIDIA's own code sample |
| Token price | $0 / $0 ✅ verified | $0 (trial service) |
| Rate limit | **20 RPM, 50 req/DAY** ✅ verified in docs | **~40 RPM, no published daily cap** ⚠️ *community-observed, undocumented* |
| Limit scope | per account, global | per `nvapi-` key, **shared across all models** |
| Legal basis | OpenRouter free tier | **NVIDIA API Trial Terms of Service** — a *trial*, not a permanent free tier |
| Extra value | fallback routing across providers, one key for many models | higher throughput, one less throttle layer |

**The one real reason to go direct:** 50 requests *per day* → ~40 requests *per minute*
with no daily cap. For an agent loop that is a ~1000× increase in daily throughput.

**Why "unlimited" is still the wrong word:** NVIDIA publishes no official RPM/RPD table
for the free tier. The ~40 RPM figure is community-observed. An NVIDIA forum moderator
stated (April 2026): *"There is no official way to circumvent this rate limit or to
receive a rate limit increase on that same tier"*, and that trial limits are
*"dependent on model, use-case and the amount of current overall traffic."* Reports also
conflict on whether the old 1,000-signup-credit system still applies or was phased out
in early 2025. **Measure it empirically with your own key.**

### Is the hosted model "full potential"? (NVFP4 vs BF16)

NVIDIA's own docs title the hosted build **NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4** and
publish both columns. Selected rows:

| Benchmark | BF16 | NVFP4 | Δ |
|---|---|---|---|
| SWE-Bench Verified | 71.9 | 69.7 | −2.2 |
| Terminal Bench 2.1 | 56.4 | 53.9 | −2.5 |
| SWE-Bench Multilingual | 67.7 | 65.8 | −1.9 |
| BrowseComp | 44.4 | 41.4 | −3.0 |
| TauBench avg | 70.9 | 70.3 | −0.6 |
| **RULER 1M (long context)** | 94.7 | **94.0** | −0.7 |
| **AA-LCR (long context)** | 65.4 | **65.5** | **+0.1** |
| GPQA (no tools) | 87.0 | 87.9 | +0.9 |

Independent measurement (Artificial Analysis, via third party — **unverified here**):
Intelligence Index BF16 **48.2** vs NVFP4 **47.7** — a 0.5 gap.

**Two things that reframe this:**
1. NVIDIA's research page states Nemotron 3 Ultra was **pre-trained in NVFP4** ("trained
   using an NVFP4 pre-training recipe"). NVFP4 is the model's *native* format, not a
   hosting downgrade. The 0.5-point gap is the whole cost.
2. **Long context is unaffected** (RULER 1M 94.0, AA-LCR 65.5). Long-context repo
   reasoning — exactly this repo's use case — is where quantisation costs you nothing.
   The losses concentrate in agentic coding and browsing.

### What "truly full potential" would actually cost

From NVIDIA's model summary, **Minimum GPU Requirement: 4×GB200, 4×B200, 4×GB300,
4×B300, or 8×H100.** That is the price of self-hosting BF16 — roughly a quarter-million
dollars of hardware or ~$20–30/hr rented — to gain **0.5 Intelligence Index points**.
Not a real option, and not worth it.

### Is Nemotron 3 Ultra the biggest/best free model? No.

It is 550B total / 55B active. **Kimi K3 is ~2.78T parameters (~5× larger)** and also
open-weight; GLM-5.1-754B-A40B is also larger. Whether either has a working free
endpoint right now was **not verified** here — and `qwen3-coder:free` being dark is a
reminder that a catalog listing is not proof of availability. Bigger is also not
automatically better: Nemotron 3 Ultra's claim is throughput (5.9× vs GLM-5.1, 4.8× vs
Kimi-K2.6 on 8k-in/64k-out), not top accuracy.

### Two repo-relevant details worth keeping

- **Hindi is a supported language** — verified in NVIDIA's model summary ("English,
  French, Spanish, Italian, German, Japanese, Korean, **Hindi**, Brazilian Portuguese,
  and Chinese"). Directly relevant if Shorts output targets Hindi audiences.
- **Reasoning is switchable**: `extra_body={"chat_template_kwargs":{"enable_thinking":True}}`,
  plus a `reasoning_budget`. For the JSON-only calls in `highlights.py` / `metadata.py`
  you likely want thinking **off** (faster, cleaner JSON); for repo analysis, **on**.
- License is **OpenMDW-1.1**, and NVIDIA states the model "is ready for commercial and
  non-commercial use" — so monetised Shorts are fine.

### Recommendation

**Go NVIDIA-direct as primary, keep OpenRouter as fallback.** Same weights, one less
throttle layer, ~40 RPM instead of 50/day. Keep OpenRouter wired as tier-2 because it
adds provider fallback routing and lets you A/B other free models with the same key.
This repo's ladder (`call_llm()` at `groq_client.py:306`) already has the shape for it.

---

## 8. What was NOT verified (honest gaps)

1. **No model was actually run.** The sandbox has no `OPENROUTER_API_KEY` and direct
   egress to `openrouter.ai` is blocked (`SSL_ERROR_SYSCALL`); all research went
   through a read-only fetch path. **No latency, output-quality, or tool-calling
   reliability was measured here.** Everything above is live *spec sheet* data.
2. **Output quality for this specific repo is unknown.** Uptime and context are
   verified; whether Nemotron 3 Ultra produces clean highlight JSON on *your* podcast
   transcripts is not. Test it against `scripts/selftest_pipeline.py` before trusting it.
3. **Free-tier availability churns without notice** — `qwen3-coder:free` going dark is
   proof. Re-check `endpoints` before committing; an empty array means uncallable.
4. Third-party speed claims (gpt-oss-120b at 36 tok/s, North Mini Code at 69 tok/s) come
   from June 2026 blogs and are **unverified** here.
5. **NVIDIA's direct rate limit is not officially documented anywhere.** The ~40 RPM
   figure, the absence of a daily cap, and the claim that signup credits were phased out
   are all community-observed and mutually inconsistent across sources. This is the
   single biggest unknown in §7 — verify it against your own `nvapi-` key before
   depending on it.
6. **Whether Kimi K3 / GLM-5.1 / MiniMax M3 have working free endpoints was not
   checked.** Only Nemotron-family and Poolside endpoints were confirmed live via the
   endpoints API.
7. **BF16 vs NVFP4 benchmark table was read from NVIDIA's own docs**, but the
   Artificial Analysis Intelligence Index numbers (48.2 / 47.7) came via a third-party
   article and were not confirmed on artificialanalysis.ai.
