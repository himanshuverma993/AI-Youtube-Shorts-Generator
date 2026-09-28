---
name: youtube-shorts-generator
description: Generate viral 9:16 YouTube Shorts (or TikTok/Reels clips) from a long-form YouTube URL or local video — at $0 cost using the free Groq tier. Triggers on requests like "make shorts from this video", "extract viral clips from this YouTube link", "auto-clip this podcast", "find the best moments and crop vertical". Pipeline downloads the source with yt-dlp, transcribes via Groq's hosted Whisper, ranks highlights through a virality framework (hook / emotional peak / opinion bomb / revelation / conflict / quotable / story peak / practical value) with Groq Llama, dedupes overlapping candidates, vertically auto-crops the top N with ffmpeg/OpenCV, and attaches FTC-compliant upload metadata (#ad #sponsored appended to every description).
---

# YouTube Shorts Generator

End-to-end pipeline that turns one long video into N viral-ready vertical clips. Each clip ships with a viral score (0–100), an opening hook line, a one-sentence reason it should perform, and upload-ready metadata whose description always ends with `#ad #sponsored`.

## When to use this skill

- "Generate shorts from this YouTube video"
- "Find the most viral 60-second clips in this podcast"
- "Auto-crop this interview to 9:16"
- "Give me TikTok clips from this lecture"

If the user only wants transcription, summarization, or thumbnails — this is the wrong skill.

## Inputs to collect before running

1. **Source** — YouTube URL (preferred) or path/URL to an mp4
2. **`num_clips`** — default 3
3. **`aspect_ratio`** — default `9:16` (also: `1:1`, `4:5`)
4. **`language`** — default auto-detect (forwarded to Groq Whisper as ISO-639-1)
5. **Output JSON path** — optional; if set, dump full result there

If the user gave a URL and nothing else, use defaults and don't block on questions.

## Prerequisites (verify before first run)

- Python 3.10+, `ffmpeg` on PATH (`sudo apt-get install -y ffmpeg`)
- A Groq key — set `GROQ_API_KEY` in `.env` (free: https://console.groq.com/keys). Powers transcription AND is the primary LLM. If missing, stop and ask the user for it; do not invent one.
- Optional: `CEREBRAS_API_KEY` (free: https://cloud.cerebras.ai) — automatic LLM failover; runs Groq-only if absent.
- `pip install -r requirements.txt` inside a venv

## Pipeline (what to execute)

1. **Download** (`local/downloader.py`) — yt-dlp pulls the source at the requested resolution (default `720`).
2. **Transcribe** (`transcriber.py`) — ffmpeg extracts a 16 kHz 32 kbps mono mp3; Groq `whisper-large-v3-turbo` returns timestamped `verbose_json` segments. Files over the 25 MiB free-tier upload cap are split into 10-minute chunks and restitched with offset timestamps. Results cache as `.srt` so reruns are free.
3. **Classify content type** — Groq Llama tags the video (podcast / interview / tutorial / vlog / lecture / debate) and density.
4. **Chunk if long** (`highlights.py`) — transcripts > `LONG_VIDEO_THRESHOLD` (1800s) split into 1200s windows with 60s overlap.
5. **Rank highlights** — Groq Llama scores each chunk against `VIRALITY_CRITERIA` under the 2026 rules: 3-second hook gate (reject silence/intro chatter/slow build-up), DM-share polarity priority, and a strict 20–40s duration lock (>90% APV window). Returns `start_time`, `end_time`, `score` 0–100, `title`, `hook_sentence`, `virality_reason`.
6. **Dedupe** — collapse >50% overlaps, keeping the higher score.
7. **Top-N selection** — sort by score, take `num_clips`.
8. **Vertical auto-crop** (`local/clipper.py`) — ffmpeg cut + OpenCV Haar-cascade face tracking with motion smoothing; audio muxed back with ffmpeg.
9. **Metadata** (`metadata.py`) — Groq Llama writes title/description/hashtags per clip under the 2026 SEO rules: description sentence #1 is a natural-language search query mirroring keywords from the clip's spoken excerpt (YouTube Search + Instagram OCR indexing), titles are 40–60 chars with the curiosity gap front-loaded, and hashtags are 3–5 high-intent niche tags; `_enforce_disclosure` then appends `#ad #sponsored` to EVERY description in code (idempotent, guaranteed even on LLM failure via the deterministic fallback).

## Invocation

CLI (the standard path):

```bash
python main.py "<YOUTUBE_URL>" \
    --num-clips 5 \
    --aspect-ratio 9:16 \
    --output-json result.json
```

Python API (when embedding in another pipeline):

```python
from shorts_generator import generate_shorts

result = generate_shorts(
    "<URL>",
    num_clips=5,
    aspect_ratio="9:16",
)
for short in result["shorts"]:
    print(short["score"], short["metadata"]["title"], short["clip_url"])
    print(short["metadata"]["description"])   # always ends with #ad #sponsored
```

Campaign mode — one URL per line in `campaign/urls.txt` (used by `.github/workflows/run_campaign.yml` on scheduled runs):

```bash
python campaign_runner.py --urls-file campaign/urls.txt --num-clips 3
```

## CLI flags reference

| Flag | Default | Notes |
|------|---------|-------|
| `--num-clips` | `3` | How many shorts to render |
| `--aspect-ratio` | `9:16` | `9:16` for TikTok/Reels, `1:1` square, anything else by flag |
| `--format` | `720` | Source download resolution |
| `--language` | auto | Whisper language code (e.g. `en`) |
| `--output-json` | — | Dump full result (transcript + all candidates + clip paths + metadata) |

## Output schema

```json
{
  "source_video_url": "output/source_<id>.mp4",
  "transcript": { "duration": 1873.4, "segments": [...] },
  "highlights": [ /* every candidate, before top-N cut */ ],
  "shorts": [
    {
      "title": "The one mistake that cost me $50K",
      "start_time": 124.3,
      "end_time": 187.6,
      "score": 92,
      "hook_sentence": "Nobody talks about this, but it killed my first startup...",
      "virality_reason": "Opens with a number + regret, peaks on a contrarian lesson",
      "clip_url": "output/short_01.mp4",
      "metadata": {
        "title": "The $50K Mistake Nobody Warns You About",
        "description": "Cut from the full episode — the exact moment everything changed. #shorts #ad #sponsored",
        "hashtags": ["#shorts", "#startups", "#ad", "#sponsored"]
      }
    }
  ]
}
```

When reporting back to the user, surface for each clip: rank, score, time range, title, hook, clip path, and description (confirm it ends with `#ad #sponsored`). Skip the raw transcript unless asked.

## Tunable knobs

- `shorts_generator/highlights.py`
  - `VIRALITY_CRITERIA` — reorder or extend signals
  - `HIGHLIGHT_SYSTEM_PROMPT` — duration sweet spot, hook rules, JSON schema
  - `CHUNK_SIZE_SECONDS` / `LONG_VIDEO_THRESHOLD` / `CHUNK_OVERLAP_SECONDS`
- `shorts_generator/config.py` (or env vars)
  - `GROQ_WHISPER_MODEL` — `whisper-large-v3-turbo`
  - `GROQ_LLM_MODEL` — `llama-3.3-70b-versatile`
  - `GROQ_MAX_RETRIES` / `GROQ_TIMEOUT` — free-tier rate-limit resilience
  - `AUDIO_CHUNK_SECONDS` / `GROQ_MAX_AUDIO_BYTES` — upload chunking
  - `FTC_DISCLOSURE_TAGS` — defaults to `#ad #sponsored`

## Failure modes — handle, don't paper over

- **Whisper produced no segments** — likely no detectable speech or a hard language. Retry with `--language <code>` (correct ISO-639-1) before declaring failure.
- **GROQ_API_KEY missing or rejected** — surface the exact error; never fabricate a key.
- **Groq rate limited (429)** — the client already retries with `Retry-After` + backoff; if it still exhausts retries, rerun later — the `.srt` cache and `campaign/processed_urls.txt` ledger prevent double-spending quota.
- **Groq LLM hard-fails (outage/quota/bad key)** — `call_llm` automatically replays the prompt on Cerebras and opens a circuit breaker (`LLM_CIRCUIT_BREAK_SECONDS`) so later calls in the run go straight to the fallback.
- **Feedback loop (trends + channel data) misbehaves** — both layers are fail-soft: a crash, missing OAuth secrets, or an empty cache results in zero prompt injection and normal generation. Nothing above the noise floor changes; surface honestly if blocks stop appearing in logs (`[trends]`/`[feedback]`/`[llm]` lines).
- **Auto-upload issues (Phase 3)** — upload failures NEVER block clipping: queue items keep, attempts increment, 3rd strike drops the item with a warning. If uploads silently do nothing, first check `UPLOAD_ENABLED`, then that the refresh token was minted with `youtube.upload` scope (oauth_local_setup.py's third scope — older tokens need a re-mint), then `YT_PRIVACY` (default private means the video exists but is not public).
- **Instagram upload issues (Phase 4)** — in order: `IG_UPLOAD_ENABLED` on? token valid and not expired (check `campaign/ig_token.json` roll log)? repo PUBLIC (release staging fails 404/401 on private repos)? IG account Business/Creator + page-linked? FB app has the IG account as admin/tester (Development mode)? Container stuck `IN_PROGRESS` beyond timeout = video rejected by IG (recode/format).
- **Cerebras ALSO fails / no keys at all (doomsday)** — `call_llm` replays on the local llama.cpp tier (`local/llm.py`: Qwen2.5-3B Q4_K_M default, grammar-forced JSON, middle-truncated prompts). Slower per call; quality of clip SELECTION drops a notch vs 70 B, but the pipeline never stops. Disable with `LOCAL_LLM=false` if you prefer an honest hard stop.
- **Groq Whisper hard-fails (outage/audio-seconds quota)** — `transcribe()` fails over to local faster-whisper on the runner's CPU (`LOCAL_WHISPER_MODEL`, circuit breaker via `WHISPER_CIRCUIT_BREAK_SECONDS`); the transcript shape is identical downstream. Slower, lower quality — but the run never dies. Disable with `WHISPER_FALLBACK=false` if you prefer hard failure.
- **Duration lock rejected everything** — candidates outside the strict 20–40s window are dropped in `_sanitize_highlights`; after 3 re-prompt attempts the URL fails visibly. In campaign mode it retries next run, striking out after 3 failed runs (`campaign/failed_urls.txt`) — never "fix" this by quietly loosening the window.
- **Highlight ranker returned fewer than `num_clips`** — return what survived dedupe with a note; don't pad with low-score filler.
- **Metadata LLM call failed** — deterministic fallback metadata is used automatically (still ends with `#ad #sponsored`).

## Done criteria

1. `result["shorts"]` has up to `num_clips` entries, each with a working local `clip_url` (mp4 exists on disk).
2. Every clip's `metadata.description` ends with `#ad #sponsored`.
3. The user has been shown the ranked list (score, time range, title, hook, path).
4. If `--output-json` was set, the file exists and parses.
