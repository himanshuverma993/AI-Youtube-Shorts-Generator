# Full-Stack Code Audit — 2026-09-28

Scope: every tracked source file of the campaign system, line-by-line, plus a
dynamic verification battery for each finding. Ordered: `config.py → main.py →
pipeline.py → highlights.py → metadata.py → trends.py → groq_client.py →
local/llm.py → local/whisper.py → local/downloader.py → local/clipper.py →
transcriber.py → uploader_youtube.py → uploader_instagram.py → feedback.py →
campaign_runner.py → scripts/oauth_local_setup.py → scripts/register_post.py →
run_campaign.yml → both `__init__` files (3,986 lines, no file skipped).

Severity legend: 🔴 behavior-breaking | 🟡 edge-case risk | ⚪ documentation/hygiene.

| # | Sev | File | Finding | Fix |
|---|-----|------|---------|-----|
| F3 | 🔴 | highlights.py | Long-video chunking fed the model ABSOLUTE timestamps while `_sanitize_highlights` clamped against a chunk-relative window → every highlight from chunk 2+ was dropped; on videos ≥30 min only the first 20 minutes ever produced clips | Show chunk-relative timestamps (`rel_chunk`), keep adding `_offset` back after sanitize |
| F7 | 🔴 | uploader_instagram.py | `roll_token_forward()` was implemented but **never invoked anywhere** — IG tokens would silently lapse ~day 60 and every upload would strike out | Invoke it at the start of every `process_ig_upload_queue` |
| F8 | 🔴 | both uploaders + workflow | Queue JSON persists via Actions cache but the referenced `.mp4` files lived on the ephemeral runner disk → cross-run queued clips were guaranteed to fail 3× and strike out, silently losing uploads | (a) queued `short_*.mp4` glob added to the state cache; (b) missing-file items are dropped with 🗑 log, **no strike burned** |
| F10 | 🔴 | run_campaign.yml | Job timeout was raised to 240 min for pinned-local runs but the inner "Generate clips" step still capped at 90 → pinned-local podcasts would be killed mid-run | Step timeout 90 → 210 |
| F2 | 🟡 | main.py | CLI summary read `meta["title"]`/`["description"]` — keys that haven't existed since Phase-1 platform-split metadata → printed `None` lines | Read `meta["youtube"]` / `["instagram"]` payloads |
| F5 | 🟡 | transcriber.py | A `.srt` cache truncated by a killed process (unparseable timestamp) crashed `transcribe()` instead of rebuilding | Wrap cache load, delete + re-transcribe on `(OSError, ValueError)` |
| F9 | 🟡 | feedback.py | The prompt block lied about IG rows: hardcoded "retention 0%" and a YT-only score formula even though IG exposes neither | `[IG]`-tagged lines (reach/shares/saves); formula header built from `stats["scoring"]` |
| F11 | ⚪ | config/.env/README | "Cerebras free tier, no credit card" outdated (no-card tier ended July 2026 — verified by operator) | All three docs corrected; fallback stays optional-with-key |
| H1 | ⚪ | local/clipper.py | Temp `*.silent.mp4` orphaned when the audio-mux ffmpeg failed | try/finally cleanup |

Verification battery (all green): long-video 3-chunk run yields highlights from
every chunk with absolute timestamps restored; `process_ig_upload_queue` invokes
token roll before any API work; missing-file items drop with zero strikes on
both queues; workflow YAML validated (210/240 timeouts, mp4 cache glob); CLI
summary renders platform-split payloads; corrupt `.srt` self-heals; feedback
block renders `[IG]` lines with truthful metrics; regression sweep confirms
short-video sanitize, pinned-local zero-cloud, and YT budget math unchanged.

Operator notes (not bugs): GitHub pauses scheduled workflows after ~60 days
without repo activity — re-enable from the Actions tab if the 4×/day cron ever
stops. IG staging requires the repo to be PUBLIC (documented). Test-mode
Google OAuth apps expire refresh tokens after 7 days — publish the consent
screen (unverified is fine) for a durable token.

---

## Second pass — perfection sweep (2026-09-28, same day)

The operator ordered zero remaining gaps, so every INFO/⚪ item that survived
pass 1 was eliminated too. Six more hardening fixes, each battery-verified:

| # | Severity | Finding | Fix |
|---|----------|---------|-----|
| P1 | 🟡 | A garbage numeric env (`TREND_CACHE_HOURS="abc"` etc.) crashed the WHOLE system at `import config` | All 20 numeric envs now via `_env_int`/`_env_float` — warn + typed default, import can never die |
| P2 | ⚪ | With 5–6 measured posts, TOP-3 and BOTTOM-3 lists overlapped (same post praised AND criticized) | Bottom list now excludes top entries disjointly |
| P3 | 🟡 | yt-dlp occasionally yields non-int `views` → `:,` format crash in trend block | Defensive int-parse, falls back to no-view text |
| P4 | ⚪ | Campaign timestamp printed an empty timezone on Linux | Literal `UTC` now |
| P5 | ⚪ | IG caption dedupe treated `#money,` (trailing comma) as a different tag → duplicates | Dedupe is punctuation-tolerant both ways |
| P6 | ⚪ | `dropped_missing_file` wasn't disclosed in the campaign summary line | Both drain summaries append it when non-zero |

Battery: P1–P6 green (typed fallback incl. arithmetic invariants; subprocess
banner + drain assertions); regression sweep re-confirms pinned-local
zero-cloud and full compile.

---

## Third pass — final audit (2026-09-28, post-fix state)

Scope: re-read of every fix hunk in EXACT post-fix form (fixes can carry their
own bugs) + fresh end-to-end flow hunts + one consolidated golden battery
re-proving all 17 invariants on the final tree.

New findings this pass (both real, both fixed):

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| F12 | 🟡 | IG release staging uploaded assets with the clip's BASENAME (video_001/short_01.mp4, video_002/short_01.mp4 → same name). GitHub rejects duplicate asset names in one release with 422 — if a previous asset DELETE ever lagged, the next same-named clip would burn an upload attempt to strike-out | Asset names now get an epoch+random suffix — collision impossible |
| F13 | ⚪ | YouTube `_build_snippet` appended the WHOLE hashtag line whenever the joined string wasn't in the description — a description containing SOME tags got the shared ones duplicated | Tag-level, punctuation-tolerant dedupe (mirrors the IG caption fix P5) |

Golden battery on the final tree (9 suites, all green): F12 unique names,
F13 tag-dedupe + snippet shape, F3 long-video chunk productivity re-run,
duration-lock both documented behaviors (partial violation → drop-and-continue,
total violation → loud failure after 3 attempts), F7 roll-before-drain,
F8 drop-no-strike, pinned-zero-cloud, YT budget 5/6→1 posted+registered→6/6,
FTC disclosure on LLM AND fallback metadata, YAML + 20-module compile.

Accepted & bounded (documented, intentionally not changed): the campaign-state
cache grows with the upload backlog (~10–40 MB per day of backlog) — GitHub's
LRU eviction handles it and queue JSON is authoritative, so resurrected stale
mp4 files are inert without queue entries.

---

## Fourth pass — adversarial sweep (2026-09-28, on operator's "zero issues ever" order)

Method: hunt only two things — secret leak-paths into logs, and
corruption-resilience of every persisted state file. Three real findings
fixed:

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| F14 | 🔴 | `_http_json` error messages echoed the full URL **including query params** — and the IG token-roll grant sends `fb_exchange_token` + `client_secret` in params. One Graph error → both secrets in the (public-repo) CI console log | Query string stripped from all raised errors (path only); leak proven impossible by a forced-401 test asserting no token/app-id/secret strings appear |
| F15 | 🟡 | `_stored_token` crashed (`AttributeError`) on valid-JSON-wrong-shape stores (e.g. a list) | isinstance shape check |
| F15b | 🟡 | Trend context cache: same wrong-shape class (`AttributeError` escaped the loader) | isinstance check + TypeError → silent rebuild |

Adversarial battery: 41/41 cases green — every ledger/queue/registry/stats/
token-store/trend-cache loader survives missing/empty/invalid-JSON/wrong-type/
wrong-shape files; valid cache hits still served; forced 401 on the token
exchange leaks zero secret bytes; full compile clean.

---

## Fourth pass — CI download chain + stage diagnosis (2026-10-01)

Scope: the "clips ban hi nahi rahi" report. Root cause was re-derived from
`output/live-test/HANDOFF.md` rather than assumed, then the download path was
re-read line by line against yt-dlp 2026.08.19.

| # | Sev | Finding | Fix |
|---|-----|---------|-----|
| C1 | 🟠 | `DEFAULT_PLAYER_CLIENTS` led with `"default"`, i.e. yt-dlp's **web** client — the exact client that produced `Sign in to confirm you're not a bot` in run 36494468151. On a GitHub Actions runner (datacenter IP) the first attempt was therefore a *guaranteed* failure, burning that attempt plus its `extractor_retries` before the rotation designed to avoid it ever started. | New `CI_PLAYER_CLIENTS = ("tv_simply","android_vr","tv","web_safari","mweb","default")`, selected by `_on_ci()` (`GITHUB_ACTIONS` / `CI`). Residential ordering unchanged. All six names validated against yt-dlp 2026.08.19's own client table in `yt_dlp/extractor/youtube/_base.py` — `tv_simply` and `mweb` were valid but previously unused. `YTDLP_PLAYER_CLIENTS` still overrides both. |
| C2 | 🟡 | `selftest_downloader.py::run()` cleared `YTDLP_PLAYER_CLIENTS` but **not** `GITHUB_ACTIONS` / `CI`. Once C1 landed, the suite's expected client chains would differ depending on where it ran — and this suite is itself executed on GitHub Actions, where both are set, so it would have failed only in CI. | `run()` now pops both, so every case asserts a deterministic chain. |
| C3 | ⚪ | No way to answer "which stage is actually broken?" — a stage-1 download failure is indistinguishable from a stage-4 render failure from the outside: both yield zero clips. | New `scripts/diagnose.py`: checks runtime, binaries, deps, credentials, real HTTPS reachability, live download with client attribution, and a real end-to-end pipeline run. Reports SKIP (never PASS) for anything it could not execute; exits 1 on any FAIL. |

### A false PASS caught during verification

The first version of `diagnose.py` probed reachability with a bare TCP connect
and reported `PASS www.youtube.com:443 TCP connect OK`. That was **wrong**: the
sandbox accepts the connection and then terminates the TLS handshake. Verified
directly —

```
www.youtube.com: TCP connect  -> OK
www.youtube.com: TLS handshake-> FAIL  SSLZeroReturnError: TLS/SSL connection has been closed (EOF)
```

The probe now performs the real TLS handshake *and* an HTTPS GET, and reports the
deepest step reached. Post-fix it correctly reports the host as unreachable.

### Regression coverage added (`scripts/selftest_downloader.py`)

`C1`–`C4`: CI chain skips the leading web client and still recovers; all six CI
clients are exhausted before one actionable `RuntimeError`; the residential chain
still leads with `default`; an explicit override beats the CI chain.

### Battery (all green, this tree)

```
py_compile every tracked .py .................. OK
selftest_audit ................................ ALL 83 CHECKS PASSED
selftest_pipeline ............................. ALL CHECKS PASSED
selftest_clipper .............................. ALL CHECKS PASSED (T5 end-to-end
                                                9:16 render included — it was
                                                previously SKIPPED for want of
                                                ffmpeg/PyAV, i.e. the repo's most
                                                visual-critical path had never
                                                actually executed)
selftest_downloader ........................... ALL CHECKS PASSED
scripts/diagnose.py ........................... exits 1 with a truthful FAIL
```

### Still NOT verified — unchanged, and not claimed otherwise

- **No live YouTube download has occurred.** YouTube is unreachable from this
  sandbox (TLS terminated), so C1's chain is proven by unit tests and by the
  client names being valid in yt-dlp's own table — **not** by a recovered
  download.
- **Groq Whisper has never run here** (no key). Hindi transcription quality,
  hook quality and metadata integrity remain zero-data.
- The workflow rerun is still operator-gated: the agent token is 403 on
  workflow dispatch.
