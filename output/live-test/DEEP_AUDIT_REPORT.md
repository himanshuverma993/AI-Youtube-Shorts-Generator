# Deep audit report — AI-Youtube-Shorts-Generator

Branch `arena/01a0ead9-ai-youtube-shorts-generator` · baseline `b7183b2` · 2026-09-29

This is the follow-up to the first live clipping test (`output/live-test/HANDOFF.md`).
It records what was **proved by running code**, what was fixed, and what is still
open. Nothing here is softened, and anything unverifiable is marked as such.

---

## 1. The headline, unchanged

**The first live test FAILED at the download stage and produced zero clips.**

Run `36494468151` (2026-09-28) died when YouTube bot-checked the GitHub runner IP:

```
ERROR: [youtube] ULsyvuvg-NU: Sign in to confirm you're not a bot.
```

Nothing downstream ever ran. Therefore, **from the live test itself**:

| Question asked | Honest answer |
| --- | --- |
| Number of clips | **0** |
| Duration range | **Not measurable** — no clip was produced |
| Hook quality, Hindi natural or garbled? | **Cannot be assessed.** Whisper never ran. |
| Metadata quality | **Cannot be assessed.** The LLM never ran. |
| Did Groq Whisper run? | **No.** |
| Did 9:16 cropping run? | **No.** |
| Did `[campaign] auto-upload: off` appear? | **Yes** — uploads were correctly disabled. |

The one thing the live test positively confirmed is the safety property: **no
upload happened.** Every quality question remains **unanswered by live data** and
is only answerable by a re-run.

---

## 2. What changed since the handoff

The audit continued past the download failure and found defects that would have
broken the run *even if* the download had succeeded. Two of them were reproduced
by execution, not by reading code.

### 2.1 Proven bug — the FTC disclosure could silently vanish

Run, not theorised:

- `_build_snippet({})` returned `description: ''`
- `_build_ig_caption({})` returned `''`
- setting `FTC_DISCLOSURE_TAGS=""` removed the disclosure **everywhere**

A sponsored clip could therefore publish with no `#ad`. Closed at three
independent layers — a config floor that refuses an empty value, the generation
path, and the upload boundary — and covered by 13 assertions (`F1`–`F13`),
including a dead-LLM end-to-end path.

### 2.2 Proven bug — campaign ledger corruption (D18)

`bump_failed_attempt` used `open(path, "w")`, which **truncates before writing**.
A repro in `/tmp/corrupt` killed the process mid-loop:

> a 5-URL ledger at 2 strikes each collapsed to **2 surviving lines** — 3 URLs
> silently reset to **zero strikes**.

Consequence: the 4×/day cron resumes burning download CPU and Groq quota on
videos that had already struck out. Fixed with `atomic_write_text`
(write-temp → `fsync` → `os.replace`).

### 2.3 Proven bug — duplicate publishing (D22 / D28)

Both uploaders ran `upload → register_post → _bump_ledger` inside **one** `try`.
A `register_post` failure re-queued an item whose video was **already live**, and
the quota ledger was never charged — so the next tick published it again.

New ordering on **both** platforms:

```
publish → charge ledger → rewrite + persist queue → then best-effort register_post
```

A `register_post` failure now only logs *"uploaded but NOT registered — the
feedback loop will not measure it"*.

### 2.4 Disk exhaustion (D1)

A 19-minute 720p source is ~300 MB and nothing deleted it. `generate_shorts` now
downloads inside `try/finally` and the `finally` removes the source — but only
when `is_downloaded_source()` agrees, so a **user's own local input file is never
deleted**. `campaign_runner` additionally sweeps between URLs and runs a per-URL
free-space pre-flight that records a shortfall as `transient: True` (no strike).

Measured in the integration test: **62 MB reclaimed across two videos, zero
`source_*` files surviving a full run.**

### 2.5 Context-budget estimate was wrong for Hindi — and my first fix was also wrong

The original budget assumed a flat 4 chars/token. These are **byte-level BPE**
tokenizers, and a Devanagari character costs 3 UTF-8 bytes, so a Hindi prompt
that "fit" overflowed `n_ctx` by roughly 3×.

My first replacement was a hand-tuned linear interpolation on the non-ASCII
ratio. **The regression suite caught it** — it produced 2.11 chars/token for
Hindi, which is optimistic. Replaced with a derivation from the sample's real
bytes-per-character:

| Script | chars/token |
| --- | --- |
| pure ASCII | 3.60 |
| Hindi with ASCII spaces | 1.44 |
| pure Devanagari | 1.20 |
| emoji | 1.00 |

### 2.6 Workflow-level fixes

- **Cache thrash.** The state cache persisted `output/campaign_*/video_*/short_*.mp4`
  under key `campaign-state-<run_id>` — a **new entry every run**. With uploads
  OFF nothing leaves the queues, so each tick re-cached the whole accumulated
  clip history, walking into GitHub's 10 GB per-repo limit and LRU-evicting the
  tier-3 model weights, which were then re-downloaded every run.
  New `scripts/prune_state_cache.py` keeps only clips an upload queue actually
  references. Steps were **reordered** so the artifact upload (the durable copy)
  runs *before* the prune, which runs before the cache save.
  An unreadable queue keeps everything — wasteful is the recoverable failure.
- **`pip install --no-cache-dir`** — the wheel cache reached ~1 GB on disk the
  campaign needs.
- **`df -h` in diagnostics**, plus `du -sh output/*` and a check for stray
  `source_*.mp4`. Disk exhaustion previously surfaced as a confusing ffmpeg error.

---

## 3. Verification

`scripts/selftest_audit.py` — **83 assertions, all passing, fully offline.**

| Section | Covers |
| --- | --- |
| A | `safe_io` crash-safety |
| B | ledger survival under mid-write kill |
| C | disk reclamation |
| D | LLM failover (`EmptyCompletion`, `GROQ_MAX_RETRIES=0`, Retry-After clamp) |
| E | `http_retry` incl. secret redaction |
| F | FTC disclosure, 13 bypass attempts |
| G | clipper guards on degenerate media |
| H | Devanagari token budget |
| I | transcript cache keying |
| J | **end-to-end** disk reclaim + campaign integration |
| K | state-cache prune |

Plus `selftest_downloader`, `selftest_clipper`, `selftest_pipeline` — all green.
`pyflakes` clean across every tracked Python file; all compile.

### A real render was performed

A 45-second test video was rendered through the actual clipper with ffmpeg:

```
short_01.mp4: 404x720 ratio=0.5611 (9:16=0.5625) frames=750 fps=30.0 dur=25.0s
short_03.mp4: 404x720 ratio=0.5611 (9:16=0.5625) frames=690 fps=30.0 dur=23.0s
```

An inverted window (`30.0s → 29.0s`) was rejected up front and **left no orphan
file** on disk.

### Two test fixtures were wrong, and the code was right

Worth stating because it cuts the other way:

- `selftest_downloader`'s fake yt-dlp "succeeded" without writing a file. The
  new empty-output guard correctly rejected it. **The fixture was fixed, not the
  guard** — and a new `T7` now asserts a phantom download is refused.
- Section `J` initially placed a fake source outside `OUTPUT_DIR`, and
  `is_downloaded_source()` correctly refused to delete it. The narrow guard did
  exactly its job.

---

## 4. What is still NOT verified

State plainly:

1. **No clip has ever been produced from the real test video.** Everything above
   is unit, integration and synthetic-render evidence.
2. **Hindi Whisper accuracy is unmeasured.** Whether the hooks are natural or
   garbled is genuinely unknown.
3. **The yt-dlp client rotation has never beaten a live bot check.** The sandbox
   cannot reach YouTube (`TLS/SSL connection has been closed (EOF)`), so the
   rotation is proven by unit tests only. Whether it defeats a real GitHub-runner
   bot check is **unknown** — it may still need `YTDLP_COOKIES_FILE`.
4. **No upload path has run against a live API.**

---

## 5. Still open

| ID | Defect |
| --- | --- |
| D7 | Chunk offset assumes exactly `i * AUDIO_CHUNK_SECONDS`; drift on variable chunks. |
| D10 | `short_{i:02d}.mp4` overwrites on rerun. |
| D17 | `chunk_transcript` step `CHUNK_SIZE − CHUNK_OVERLAP` ⇒ infinite loop if equal; overlap-tail highlights clamp to `end <= start` and are dropped. |
| D27 | Whole clip held in RAM on upload; no `Content-Range` resume. |

---

## 6. Recommended next step

Re-run the workflow. **I cannot dispatch it** — this session's token is read-only
on Actions (`gh workflow run`, the dispatches API and the permissions endpoint
all return 403).

Before re-running, consider setting `YTDLP_COOKIES_FILE`: item 3 above is the
single most likely cause of a second failure, and the client rotation alone may
not be enough.

---

# Addendum — second review pass, 2026-09-29

Requested: a full re-verification with nothing skipped, then merge, then trigger.

## 7. A SECOND run failed, and it was not in the original handoff

`gh run list` surfaced run **`36517202871`** — the 00:00 UTC tick, which
actually started at **03:27 UTC** (3.5 hours late; GitHub's scheduler is
best-effort). It ran on `main @ a7898199`, i.e. **none of the fixes**, and died
in **1.2 seconds**:

```
downloader.py line 131 -> ydl.extract_info(url, download=True)
ERROR: [youtube] ULsyvuvg-NU: Sign in to confirm you're not a bot.
```

A single call, no fallback. Confirmed from the job log, and it also gave three
facts worth recording verbatim from the live env dump:

| Observed | Meaning |
| --- | --- |
| `UPLOAD_ENABLED: false`, `IG_UPLOAD_ENABLED: false`, all OAuth blank | uploads are definitively off — verified, not assumed |
| `YT_COOKIES_B64:` empty, cookie step skipped | **no cookies are configured** |
| `Cache not found for input keys: campaign-state-...` | the campaign cache is empty: the URL has **zero strikes** |
| `opencv-python-headless-5.0.0.93` installed | the `<5` pin was not on `main` |

## 8. The four open defects, measured against THIS video

Rather than leave them as abstract risks, each was checked against the actual
18:59 test video:

| ID | Applies to this run? | Evidence |
| --- | --- | --- |
| **D7** chunk offset drift | **No** | The audio extracts to **4.6 MB** at 32 kbps/16 kHz mono, under the 25 MB Groq threshold, so `_split_audio` is never called. Forced anyway, measured drift was **+0.010 s** on chunk 2. |
| **D10** clip overwrite | **No** | Every run writes to a fresh `campaign_<UTC-timestamp>/video_NNN/`. |
| **D17** chunk loop | **No** | 1139 s < the 1200 s chunk size, so it is a single chunk. Step is 1140 s > 0, so no infinite loop regardless. |
| **D27** upload RAM | **No** | Uploads are off. |

None of the four endanger this live test.

## 9. Full dress rehearsal on real media

A **19-minute, 402 MB** source was rendered through the real pipeline with real
ffmpeg, stubbing only the two network boundaries (Whisper and the LLM):

```
3 clips rendered  |  404x720, ratio 0.5611 (9:16 = 0.5625; 405 is odd, so
                     even-rounding gives 404)  |  durations 28s / 32s / 27s
#ad #sponsored present in the YT description AND the IG caption
Hindi written readable, not \uXXXX-escaped
[pipeline] removed source (421 MB)  -> no source_* survived
```

`scripts/verify_artifacts.py` on that output:

```
checks: 74 pass / 0 fail / 6 warn / 0 unverified / 13 info
clip duration range: 27.00s - 32.00s
VERDICT: PASS
```

The 6 warnings are cosmetic (title-length targets and hashtag counts from the
stubbed LLM). A side observation: with OpenCV 5 in the sandbox the clipper
**degraded to a static centre crop and did not crash**, which is the guard from
`e3fa287` doing its job.

## 10. Bot-check no longer costs a strike

The old code charged one: the failed run logged *"2 attempt(s) left before
strike-out"*. Verified against the current classifier:

| Error | Strike? |
| --- | --- |
| raw yt-dlp bot check | **no strike** |
| "refused every InnerTube client" | **no strike** |
| private video | strike |
| no detectable speech | strike |

So repeated dispatches cannot retire the URL.

## 11. Merged — and the trigger I could not do

PR **#3** merged to `main` as **`55dc4f7`**. `main` now carries the client
rotation and the `opencv<5` pin.

**I could not start the run.** `gh workflow run` and the raw dispatches API both
return `403 Resource not accessible by integration`, on `main` and on the arena
branch. No run was created. The user has to press the button.

Suites at merge time: `selftest_audit` 83/83, plus `selftest_downloader`,
`selftest_clipper`, `selftest_pipeline` — all green. pyflakes clean.

## 12. The honest odds on the next run

The rotation is a reasoned mitigation, **not a proven one** — the sandbox cannot
reach YouTube, so it has never faced a live bot check. Two consecutive runs were
blocked in about a second each from GitHub's IP range. If the re-run fails the
same way, the fix is `YT_COOKIES_B64`, not more code.
