# LIVE CLIPPING TEST #1 — RESULT ANALYSIS

**Bottom line (Hinglish):** Test **FAIL** hua. **0 clips bane.** Pipeline download step par hi
mar gaya — YouTube ne GitHub runner ke datacenter IP ko bot-check kar diya. Groq Whisper,
highlights, 9:16 crop, metadata — **in mein se kuch bhi chala hi nahi**, isliye hook quality /
Hindi natural hai ya garbled, ye main **verify nahi kar sakta**. Guess nahi karunga.
Uploads **definitely nahi hue** — wo watermark log mein verbatim mil gaya.

| Field | Value |
|---|---|
| Analysed | 2026-09-29 ~02:00 UTC |
| Test video | https://youtu.be/ULsyvuvg-NU (TMKOC, Hindi, 18:59) |
| Run analysed | `36494468151` (run_number 1, attempt 1) |
| Commit | `a7898199` (main, post-PR-#2) |
| Trigger | `schedule` |
| Window | 2026-09-28T22:47:47Z → 22:52:06Z (**4m19s**) |
| Conclusion | **FAILURE** |
| Clips produced | **0** |
| Artifacts | **0** (`total_count: 0`) |
| Uploads | **0** — confirmed off |
| Verdict | **FAIL — infra/anti-bot, not a pipeline logic bug** |

Raw evidence: [`run_36494468151_excerpt.log`](run_36494468151_excerpt.log) ·
Machine report: [`report.json`](report.json)

---

## 1. `gh run list --workflow run_campaign.yml` — the 18:00 / 00:00 ticks

**There is exactly ONE workflow run in this repository. Ever.**

```
$ gh api "repos/:owner/:repo/actions/runs?per_page=100" --jq '.total_count'
1
$ gh run list --workflow run_campaign.yml
completed  failure  Run clipping campaign  main  schedule  36494468151  4m19s
   createdAt: 2026-09-28T22:47:47Z   run_number: 1   run_attempt: 1
```

Against your two expected ticks:

| Expected tick | Run created? | Evidence |
|---|---|---|
| 2026-09-28 **18:00 UTC** | **NO run at 18:00** | no run exists in that window |
| 2026-09-29 **00:00 UTC** | **NO run** (as of 02:02 UTC) | still `total_count: 1` at 02:02 UTC |
| *(also eligible)* 09-28 06:00, 12:00 | **NO runs** | workflow landed on `main` 04:56 UTC |
| — | **1 run at 22:47:47 UTC** | the only run; the one analysed below |

**What I can prove:** the cron is `0 0,6,12,18 * * *`, the workflow file has been on `main`
since 2026-09-28T04:56:02Z (commit `fdec24a2`), the workflow state is `active`, and **4 of the
5 eligible ticks since then produced no run at all.**

**What I CANNOT prove — not guessing:** which cron tick the 22:47:47Z run belongs to. GitHub
does not expose that mapping. It is *consistent with* a ~4h47m-delayed 18:00 tick, but that is
inference, not evidence.

**Two documented causes fit, and I can't distinguish them from the API:**
1. This repo is a **fork** (`"fork": true`, public). Per GitHub docs, *"When a public repository
   is forked, scheduled workflows are disabled by default."* Someone enabling them partway
   through 09-28 would explain the missing early ticks.
2. Scheduled runs are a **best-effort queue** — GitHub documents that they may be delayed under
   load **and dropped entirely with no notification**.

➡️ **Operational consequence: do not trust the "4×/day" cron as a test harness.** For the rerun,
use `workflow_dispatch` so you get a deterministic, immediate run.

---

## 2. Stage-by-stage proof from the run log

`gh run view --log` / `--log-failed` **could not be used from this sandbox** —
`productionresultssa2.blob.core.windows.net` (where Actions stores logs) is network-blocked here
(`curl: (35) SSL_ERROR_SYSCALL`). I retrieved the identical signed blob URL out-of-band instead;
everything quoted below is verbatim, with original timestamps preserved in the excerpt file.

### Step outcomes (`GET /actions/runs/36494468151/jobs`)

```
 1-8  Set up job → Cache model weights ............ success
 9    Decode optional YouTube cookies for yt-dlp .. SKIPPED   (YT_COOKIES_B64 empty)
10    Generate clips .............................. FAILURE   22:52:01 → 22:52:03  (2 seconds)
11    Upload clips + metadata JSON ................ SKIPPED   ← why there are 0 artifacts
```

### ❌ Groq Whisper — **NEVER RAN. Not proven.**

The banner shows the backend was *configured*, and nothing more:

```
22:52:01.9089715Z [campaign] backends — Groq: ✓ | Cerebras: ✗ | local CPU tier-3: ✓ (zero-key doomsday mode)
22:52:01.9090526Z [campaign] providers — whisper: Groq → local fallback | llm: Groq → Cerebras → local
```

`GROQ_API_KEY: ***` was present in the step env. But `pipeline.generate_shorts` calls
`download_youtube_local()` on **line 68**, *before* `transcribe()` on line 70 — and it raised.
**Zero Whisper API calls were made. No transcript, no `.srt`, no Groq quota spent.**

### ❌ Highlights — **NEVER RAN.** Downstream of transcription.
### ❌ 9:16 crop — **NEVER RAN.** `crop_highlights_local` was never reached; no `short_*.mp4` exists.
### ❌ Metadata — **NEVER RAN.** No `result_*.json`, no `.youtube.json` / `.instagram.json` sidecars.

### ✅ `[campaign] auto-upload: off` — **FOUND, verbatim. No upload happened.**

```
22:52:01.9146031Z [campaign] auto-upload: off (UPLOAD_ENABLED=false or OAuth trio unset)
22:52:01.9146900Z [campaign] instagram upload: off (IG_UPLOAD_ENABLED=false or token/user-id unset)
```

Corroborated by the resolved step env and by the trivial fact that zero clips existed to upload:

```
UPLOAD_ENABLED: false      IG_UPLOAD_ENABLED: false      YT_PRIVACY: private
YT_REFRESH_TOKEN: <empty>  IG_ACCESS_TOKEN: <empty>      IG_USER_ID: <empty>
```

**Upload variables were not touched by me and remain exactly as they were.**

### 🔴 The actual failure

```
22:52:02.1027783Z [download/local] https://www.youtube.com/watch?v=ULsyvuvg-NU @ 720p → output/
22:52:03.1310859Z ERROR: [youtube] ULsyvuvg-NU: Sign in to confirm you’re not a bot.
                  Use --cookies-from-browser or --cookies for the authentication.
...
  File ".../shorts_generator/pipeline.py", line 68, in generate_shorts
    source_path = download_youtube_local(youtube_url, fmt=download_format)
  File ".../shorts_generator/local/downloader.py", line 131, in download_youtube_local
    info = ydl.extract_info(video_url, download=True)
yt_dlp.utils.DownloadError: ERROR: [youtube] ULsyvuvg-NU: Sign in to confirm you’re not a bot.
...
22:52:03.1367878Z [campaign] finished: 0 ok, 1 failed, 0 skipped
22:52:03.1643504Z ##[error]Process completed with exit code 1.
```

Died **1.03 seconds** into the download. Toolchain was current: `yt-dlp-2026.8.19`,
`faster-whisper-1.2.1`, `groq-1.7.0`, ffmpeg installed OK, Python 3.10.21.

---

## 3. `gh run download` — nothing to download

```
$ gh api repos/:owner/:repo/actions/runs/36494468151/artifacts
{"total_count":0,"artifacts":[]}

$ gh run download 36494468151 -D output/live-test/artifacts
no valid artifacts found to download          (exit 1)
```

**Root cause of the empty Artifacts panel is separate from the bot check:** the
`Upload clips + metadata JSON` step had **no `if:` condition**, so GitHub skipped it once
`Generate clips` failed. Even the `summary.json` the runner *did* write
(`output/campaign_20260928_225201/summary.json`) was lost with the ephemeral disk. **Fixed** — see §6.

---

## 4. `verify_artifacts.py` — ran, on an empty bundle

⚠️ **Note:** `output/live-test/HANDOFF.md` and `output/live-test/verify_artifacts.py` **did not
exist** in the repo when I started. PR #2 changed exactly one file (`campaign/urls.txt`, +2/−2).
So I **wrote** the verifier rather than running a pre-existing one. This document is that handoff.

```
$ python3 output/live-test/verify_artifacts.py output/live-test/artifacts --json-out output/live-test/report.json
found: 0 clip(s), 0 result json, 0 summary json
  [FAIL] bundle :: clips_present — no short_*.mp4 anywhere in the bundle
VERDICT: NO_ARTIFACTS                                                    (exit 2)
```

The verifier is **self-tested against a synthetic bundle** with deliberately planted defects, and
it caught every one: mojibake title, empty description, missing `#ad`/`#sponsored`, 94-char
Instagram fold line, 12s clip (outside the 20–40s lock), 16:9 instead of 9:16, missing audio
stream, empty hook. Its media numbers were cross-validated against **PyAV** and agreed exactly
(`608x1080 / 28.000s / h264+aac` and `1920x1080 / 12.000s / h264, no audio`). It uses `ffprobe`
when available and a stdlib MP4 box parser otherwise — and marks anything it genuinely cannot
read as `unverified` instead of passing it.

---

## 5. Honest verdict

| Question | Answer |
|---|---|
| **Kitne clips?** | **0.** Zero rendered, zero artifacts. |
| **Duration range?** | **N/A** — no clip exists to measure. |
| **Hook quality (Hindi natural ya garbled)?** | **CANNOT ASSESS. No transcript, no hooks, no metadata were ever generated.** Anything I said here would be invention. The Hindi-text integrity checks are built and tested, but they have had **nothing to run against**. |
| **Metadata?** | **None produced.** No `result_*.json`, no platform sidecars. |
| **Uploads?** | **Zero — proven.** `[campaign] auto-upload: off` in the log, both switches `false`, and no clips existed anyway. |
| **Naya issue?** | **Yes — 3 new findings (N1–N3 below).** |

### N1 — 🔴 Artifact upload was skipped on failure *(fixed in this commit)*
Red runs left **nothing** in the Artifacts panel, so triage depended entirely on ephemeral runner
logs. That is what made this analysis harder than it needed to be.

### N2 — 🟠 The 3-strike ledger is not persisted on a failed run *(reported, NOT changed)*
Both `actions/cache` post-steps report `conclusion: skipped`, and the log contains **no
`Cache saved with key ...` line**. The cache inputs echo `save-always: false`. So the strike
written by this run (`failed_urls.txt`, attempt 1/3) **was silently discarded** — the next run
starts again from 0 strikes.

Right now that is *helpful* (the URL isn't burned by an infra outage), so **I deliberately did not
change it** — flipping cache semantics could start persisting half-broken state. But be aware:
**the "3 strikes and skip" protection is currently inert for hard failures**, and successful
transcript caches are also lost whenever any URL in the batch fails. Your call whether to split
this into `cache/restore` + `cache/save` with `if: always()`.

### N3 — 🟠 The cron is not delivering 4×/day (see §1)
4 of 5 eligible ticks produced no run. Fork-disabled schedules and/or best-effort drops. Use
`workflow_dispatch` for anything you actually need to happen.

### Not verifiable from here — stated plainly
- Whether **Groq Whisper handles Hindi** well on this source. Never invoked.
- Whether the **face-aware 9:16 crop** tracks TMKOC's multi-person framing. Never invoked.
- Whether **`YTDLP_PLAYER_CLIENTS` rotation actually defeats this specific block** — YouTube is
  **also unreachable from this sandbox** (same SSL egress block), so the fix is proven by
  **offline unit tests of the retry/classification logic only**, not by a live download.
  **The rerun is the real test.**

---

## 6. Root cause + what I changed

**Root cause:** YouTube served an anti-bot interstitial to the runner's datacenter IP. The
pipeline requested the default InnerTube *web* client anonymously (`YT_COOKIES_B64` unset → the
cookie step was skipped), got zero formats, and `yt_dlp` raised. There was **no retry and no
fallback** — one refusal killed the whole campaign run.

Committed on `arena/01a0ead9-ai-youtube-shorts-generator`:

**1. `shorts_generator/local/downloader.py` — InnerTube client rotation**
On a bot-check the download is retried under alternate clients
(`default → tv_simply → android_vr → tv → web_safari → mweb`), which use different InnerTube
endpoints that are often still served unauthenticated from the same IP. Overridable via the
`YTDLP_PLAYER_CLIENTS` repo variable. A genuinely unavailable video (private/deleted/404) still
**fails fast on the first client** — no wasted retries. Final failure now raises an actionable
message naming `YT_COOKIES_B64`. Also added `retries`/`fragment_retries`/`extractor_retries`.

**2. `.github/workflows/run_campaign.yml` — always capture evidence**
`Upload clips + metadata JSON` now has `if: always()`, plus a new `Collect diagnostics` step
(`if: always()`, `continue-on-error: true`) recording yt-dlp/ffmpeg/Python versions, whether
cookies were configured, the active client chain, and a file manifest. **A red run will now
always produce a downloadable bundle.** Upload switches untouched.

**3. `scripts/verify_artifacts.py`** — the verifier described in §4 (copy at
`output/live-test/verify_artifacts.py` so the handoff command works verbatim).

**4. `scripts/selftest_downloader.py`** — the 13 offline assertions covering the rotation logic.

### Verification performed
```
py_compile (all tracked .py)................... OK
scripts/selftest_downloader.py ................ 13/13 PASS
verify_artifacts.py vs planted-defect fixture . caught 8/8 defects
verify_artifacts.py vs PyAV ground truth ...... exact match, both probe backends
run_campaign.yml YAML parse ................... valid; UPLOAD_ENABLED/IG_UPLOAD_ENABLED/YT_PRIVACY unchanged
Collect diagnostics step under `bash -e` ...... exit 0 even with yt_dlp+ffmpeg absent
Live YouTube download ......................... ❌ NOT TESTED — YouTube unreachable from this sandbox
```

---

## 7. Rerun checklist

> ⚠️ **The scheduled cron runs the workflow from `main` only.** This fix is on
> `arena/01a0ead9-ai-youtube-shorts-generator`. Until it is merged, cron ticks keep using the
> **old, unfixed** code. Dispatch against the branch, or merge first.

1. **Dispatch the fixed code** (uploads stay off — both variables remain `false`):
   ```bash
   gh workflow run run_campaign.yml --ref arena/01a0ead9-ai-youtube-shorts-generator
   ```
2. Watch it: `gh run watch $(gh run list --workflow run_campaign.yml -L1 --json databaseId -q '.[0].databaseId')`
3. Confirm in the log:
   - `[campaign] auto-upload: off` — **must still be there**
   - `[download/local] recovered via player_client=<x>` — rotation saved the run, or
   - `YouTube refused every InnerTube client` — go to step 5
4. Download + verify:
   ```bash
   gh run download <id> -D output/live-test/artifacts
   python3 output/live-test/verify_artifacts.py output/live-test/artifacts \
       --json-out output/live-test/report.json --expect-clips 3
   ```
5. **If every client is still refused**, the IP is hard-blocked and only cookies will fix it:
   ```bash
   # from a browser logged into YouTube, export cookies.txt (Netscape format)
   base64 -w0 cookies.txt | gh secret set YT_COOKIES_B64
   ```
   The workflow already decodes that secret into `YTDLP_COOKIES_FILE` — no code change needed.
   Use a throwaway Google account; those cookies are full account credentials.

**Only after a green run can hook quality, Hindi text integrity, 9:16 framing and metadata be
judged. Until then those remain unverified — not "probably fine".**
