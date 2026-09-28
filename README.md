# AI YouTube Shorts Generator — $0-Cost Groq Edition

**The open-source alternative to Opus Clip, Vidyo.ai, Klap, SubMagic, 2short.ai, and other AI clipping tools.** Drop in any long-form YouTube video and get back ranked, viral-ready 9:16 shorts — at **zero recurring cost**, with no per-clip credits, no watermarks, and full control over the highlight algorithm.

This fork is stripped of every paid/closed API dependency and wired to run **directly on free GitHub Actions runners** with a **free Groq API key** — no server, no credit card:

| Stage | Tool | Cost |
|---|---|---|
| Source download | `yt-dlp` | free |
| Transcription | **Groq** `whisper-large-v3-turbo` → **local faster-whisper** | free tier → $0 CPU |
| Highlight ranking | **Groq** `llama-3.3-70b-versatile` → **Cerebras** → **local llama.cpp 3B** | free → free → $0 CPU |
| Metadata + hashtags | **Groq** Llama → **Cerebras** → **local llama.cpp 3B** | free → free → $0 CPU |
| Vertical reframe | `ffmpeg` + OpenCV face tracking | free, local |
| FTC disclosure | `#ad #sponsored` appended **in code** to every description | — |

No OpenAI, Gemini, or MuAPI keys are used anywhere. **Doomsday-proof by design:** every AI stage ends in a local CPU tier, so the pipeline keeps producing clips even with *zero working API keys* — Groq dead, Cerebras dead, both dead, free tiers cancelled — the run degrades gracefully (slower, ~3–8 min per LLM call on the runner's 4 CPU cores) but never stops. Toggle with `LOCAL_LLM=false` / `WHISPER_FALLBACK=false` if you prefer hard failure over slow survival.

![longshorts](https://github.com/user-attachments/assets/3f5d1abf-bf3b-475f-8abf-5e253003453a)

## Features

- **🎬 YouTube In, Vertical Out**: any YouTube URL → N viral-ready 9:16 mp4s
- **🆓 $0 stack**: free Groq tier for all AI calls; everything else runs on your own machine
- **🤖 Virality-Aware Highlight Selection**: clips ranked on hooks, emotional peaks, opinion bombs, revelation moments, conflict, quotable lines, story peaks, and practical value
- **📈 Score + Hook + Reason for Every Clip**: each highlight ships with a viral score, an opening hook line, and a one-sentence rationale
- **🧩 Long-Video Aware**: transcripts over 30 min chunk with overlap; audio over Groq's 25 MiB upload cap is auto-split into 10-minute chunks and restitched
- **♻️ Smart Dedupe**: overlapping highlights collapse by score
- **🎯 Face-Tracked Vertical Crop**: OpenCV Haar-cascade tracking with motion smoothing; audio muxed back with ffmpeg
- **⚖️ FTC-Compliant Metadata**: every generated description ends with `#ad #sponsored` — appended in code, so the LLM can't drop it
- **🪆 Headless Campaign Mode**: `campaign_runner.py` works through a URL list, records successes in a ledger, retries failures next run — and strikes out permanently-failing URLs after 3 attempts so hopeless sources stop burning quota
- **📦 JSON Output**: `--output-json` dumps transcript + candidates + clips + metadata for downstream automation

## Setup

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg python3-venv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then paste your free key from https://console.groq.com/keys
```

`.env`:

```bash
GROQ_API_KEY=gsk_your_key_here
```

## Usage

```bash
.venv/bin/python main.py "https://www.youtube.com/watch?v=..." --num-clips 3
```

Options: `--num-clips` (default 3), `--aspect-ratio` (default `9:16`), `--format` (360/480/720/1080), `--language` (ISO-639-1 override), `--output-json`.

Every short comes back with `metadata.title`, `metadata.description` (always ending `#ad #sponsored`), and `metadata.hashtags` — ready to upload.

### Headless campaign mode

```bash
# one URL per line in campaign/urls.txt, then:
.venv/bin/python campaign_runner.py --urls-file campaign/urls.txt --num-clips 3
```

Processed URLs land in `campaign/processed_urls.txt`, so cron/CI reruns only pick up **new** sources; URLs that fail 3 runs in a row move to `campaign/failed_urls.txt` and are skipped. Rendered shorts go to per-video folders + JSON under `output/campaign_<timestamp>/`, with a `summary.json` for the whole run.

## Run on GitHub Actions (no server, no credit card)

`.github/workflows/run_campaign.yml` runs the entire pipeline on free GitHub-hosted runners — nothing to rent or manage.

**Manual run:** **Actions → Run clipping campaign → Run workflow**. Paste a YouTube URL for a single clip job, or leave the URL empty to batch-process every new URL in `campaign/urls.txt`. Choose clip count, aspect ratio, and language.

**Automatic run:** 4× daily via cron (`0 0,6,12,18 * * *` — 00:00 / 06:00 / 12:00 / 18:00 UTC), batch-processing `campaign/urls.txt`. The processed-URL ledger and Whisper transcript caches persist between runs via `actions/cache`, so each cron tick only clips **new** URLs (and never re-spends Groq quota on already-transcribed sources).

## Feedback loop (Phase 2 — learns from YOUR channel)

The pipeline stops guessing once it's collecting data:

1. **Register what you post** (manual, works without auto-upload):
   `python scripts/register_post.py --url <yt-link> --clip-path <short_XXX.mp4>` → row lands in `campaign/posting_registry.json` (sidecar `.youtube.json`/`.instagram.json` auto-fills title/caption/hashtags).
2. **One-time OAuth**: `python scripts/oauth_local_setup.py` → store the 3 GitHub secrets (see secrets table). Free read-only YouTube Analytics, no billing.
3. **Each campaign run**: rows ≥48h old get measured (retention, view velocity, likes/comments/shares/subs) → deterministic score (60% retention + 40% velocity) → **once ≥5 posts have stats**, a `CHANNEL FEEDBACK` block (top-3 vs under-performers with hooks/titles) is injected into highlight + metadata prompts. Below the threshold, or on any OAuth/API hiccup, generation continues completely unaffected.

## Auto-upload (Phase 3 — YouTube, quota-managed)

Opt-in: `UPLOAD_ENABLED=true` + the same OAuth trio (token must carry `youtube.upload` scope — re-run `scripts/oauth_local_setup.py` if minted earlier; it now includes the scope by default).

- **Safe ladder:** starts at `YT_PRIVACY=private` — review, then flip to `unlisted`/`public` when confident.
- **Quota math:** `videos.insert` costs 1,600 of the free 10,000 units/day → hard ceiling `UPLOAD_DAILY_CAP=6`, per-run `UPLOAD_MAX_PER_RUN=3`. Best-scored clips go first; the rest wait in `campaign/upload_queue.json` and drain on later cron ticks (queue-only runs also process it).
- **3-strike queue:** a clip failing `UPLOAD_MAX_ATTEMPTS` uploads is dropped with a warning instead of silently re-burning quota.
- **Feedback closure:** every successful upload auto-registers into `posting_registry.json` — the Phase-2 loop starts measuring it with zero manual work.
- Artifacts never change: clips keep landing in `output/…` regardless of upload state. Upload failures mark the queue item; the campaign continues.

## Instagram Reels upload (Phase 4 — requirements are real, be aware)

Opt-in: `IG_UPLOAD_ENABLED=true` + `IG_ACCESS_TOKEN` + `IG_USER_ID`. **Honest prerequisites:**
1. IG account is **Business/Creator**, linked to a **Facebook Page**, and your **FB Developer app** (free, Development mode works for your own account — no app review needed for self-posting).
2. The repo must be **public**: IG's container API needs a public media URL, so each clip is staged as a GitHub Release asset (`media-staging` tag) via the built-in `GITHUB_TOKEN`, then deleted after publishing.
3. Token lifecycle: seed a **60-day long-lived** user token once; with `IG_APP_ID/SECRET` also set, the runner rolls it forward automatically (`campaign/ig_token.json`, cache-persisted). Without them, re-seed manually every ~60 days.

Same rituals as YouTube: score-ranked queue (`campaign/ig_upload_queue.json`), `IG_UPLOAD_MAX_PER_RUN=3`, 3-strike drops, auto-register into the feedback registry. **IG insights** (Phase-4 3B) then measures reach/likes/comments/shares/saves — IG exposes *no retention%*, so its cohort is documented-ly scored 50% reach-velocity + 25% shares + 25% saves.

Each run on `ubuntu-latest`:

1. checks out the repo and sets up **Python 3.10**
2. installs system deps (`sudo apt-get update && sudo apt-get install -y ffmpeg`) and `pip install -r requirements.txt`
3. generates clips with `GROQ_API_KEY` injected from Actions secrets
4. uploads every rendered `.mp4` **plus the metadata JSON** as a downloadable artifact: **Actions → your run → Artifacts** (kept 14 days). Manual runs land in `output/` (`short_*.mp4` + `result.json`); batch runs upload the whole `output/campaign_<timestamp>/` tree — per-video clip folders (`video_001/short_01.mp4`, …), each video's `result_NNN.json` (per-clip title, description ending in `#ad #sponsored`, hashtags), and a `summary.json`

### GitHub Secrets

| Secret | Required | Purpose |
|---|---|---|
| `GROQ_API_KEY` | ✅ | free key from https://console.groq.com/keys — powers Whisper + Llama (primary) |
| `CEREBRAS_API_KEY` | recommended | free key from https://cloud.cerebras.ai (1M tokens/day, no card) — automatic LLM failover; if Groq's LLM dies mid-campaign, the run keeps going on Cerebras |
| `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` + `YT_REFRESH_TOKEN` | optional trio — feedback loop | minted once via `scripts/oauth_local_setup.py`; free read-only YouTube Analytics so the pipeline learns from **your channel's real retention/views** (Phase 2). Unset = loop silently off |
| `IG_ACCESS_TOKEN` + `IG_USER_ID` (+ `IG_APP_ID`/`IG_APP_SECRET`) | optional — Instagram upload + insights | 60-day long-lived user token + IG business id. `IG_APP_*` enables automatic 60-day token rolling. Requires public repo (release-asset staging) |
| `YT_COOKIES_B64` | optional | base64-encoded Netscape `cookies.txt` for yt-dlp (`base64 -w0 cookies.txt`); only needed if YouTube bot-checks the runner's datacenter IP |

Caveats of the free-runner tier: private repos consume the 2,000 free minutes/month (public repos are unlimited); GitHub auto-disables scheduled workflows after 60 days of repo inactivity (any push or a manual re-enable resets the clock); runners are capped at 4 h per job (hard-limited here to 120 min), so keep each batch modest.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | free Groq key (required) |
| `GROQ_WHISPER_MODEL` | `whisper-large-v3-turbo` | transcription model |
| `GROQ_LLM_MODEL` | `llama-3.3-70b-versatile` | highlight/metadata model (primary) |
| `CEREBRAS_LLM_MODEL` | `llama-3.3-70b` | fallback LLM model when Groq fails |
| `LLM_FALLBACK` / `LLM_CIRCUIT_BREAK_SECONDS` | `true` / `300` | failover toggle + Groq skip-after-failure window |
| `LOCAL_WHISPER_MODEL` | `small` | local Whisper fallback model: `tiny`/`base`/`small`/`medium` |
| `WHISPER_FALLBACK` / `WHISPER_CIRCUIT_BREAK_SECONDS` | `true` / `300` | local-Whisper failover toggle + Groq-Whisper skip window |
| `CAMPAIGN_NICHE` | *(empty = trends OFF)* | niche keywords that arm the trend-context layer |
| `TREND_CONTEXT` / `TRENDS_GEO` / `TREND_CACHE_HOURS` | `true` / `IN` / `24` | trend injection toggle, RSS geo, cache TTL |
| `FEEDBACK` / `FEEDBACK_MIN_POSTS` / `FEEDBACK_MIN_AGE_HOURS` | `true` / `5` / `48` | feedback loop toggle + activation thresholds |
| `UPLOAD_ENABLED` / `UPLOAD_MAX_PER_RUN` / `UPLOAD_DAILY_CAP` | `false` / `3` / `6` | auto-upload switch + per-run cap + quota ceiling |
| `YT_PRIVACY` / `YT_CATEGORY_ID` / `UPLOAD_MAX_ATTEMPTS` | `private` / `22` / `3` | publish visibility ladder, category, queue strikes |
| `LOCAL_LLM` / `LOCAL_LLM_CTX` / `LOCAL_LLM_MAX_TOKENS` | `true` / `16384` / `2048` | tier-3 doomsday LLM: enable + context + reply budget |
| `LOCAL_LLM_REPO` / `LOCAL_LLM_FILE` / `LOCAL_LLM_DIR` | `bartowski/Qwen2.5-3B-Instruct-GGUF` / Q4_K_M / `models` | which GGUF + where it lives |
| `GROQ_TIMEOUT` / `GROQ_MAX_RETRIES` | `300` / `5` | request timeout / 429-retry budget |
| `GROQ_MAX_AUDIO_BYTES` | `25165824` (24 MiB) | upload cap before audio chunking |
| `AUDIO_CHUNK_SECONDS` | `600` | chunk length when splitting audio |
| `OUTPUT_DIR` | `output` | clips, caches, campaign JSON |
| `FTC_DISCLOSURE_TAGS` | `#ad #sponsored` | appended to every description |

## Notes

- Groq's free tier is rate-limited; all calls retry with the server-provided `Retry-After` plus exponential backoff. Transcripts are cached as `.srt` so reruns don't re-spend quota.
- If Groq's **LLM** hard-fails and `CEREBRAS_API_KEY` is set, `call_llm` replays the exact same prompt on Cerebras; if Cerebras also dies (or is absent), it replays on a **local llama.cpp model** (default Qwen2.5-3B-Instruct Q4_K_M, ~1.9 GB, grammar-forced JSON so a 3B can't emit broken output). A circuit breaker per cloud skips the dead tier for the rest of the run, then re-probes it later.
- If Groq **Whisper** hard-fails (outage / audio-seconds quota / bad key), transcription fails over to **local faster-whisper** on the runner's own CPU (`LOCAL_WHISPER_MODEL=small`, int8 ≈ 7–8 min per hour of podcast audio on 4 cores) — no key, no quota, just slower.
- First tier-3 run downloads ~2 GB of model weights once; the workflow caches `models/` so later runs start instantly.
- Everything runs on stock `ubuntu-latest` runners: only `ffmpeg` from apt + four pip packages.
- `opencv-python-headless` is used (no GUI libs), so no extra system X11 packages are needed on a headless runner.
