# 🤖 AI YouTube Shorts Generator — System Ka Poora Naksha

> **Ek line mein:** Aap lambi YouTube videos/podcasts ki list dete ho — yeh system **apne aap** unse viral-worthy 20–40 second ke Shorts kaat-ta hai, 9:16 vertical banata hai, titles/captions likhta hai, **khud YouTube aur Instagram par post karta hai**, aur phir un posts ke asli performance data se **khud ko improve** karta rehta hai — **sab kuch $0 cost par, GitHub ke free servers par.**

---

## 1. 🗺️ Poora Flow (ek video ka safar)

```
campaign/urls.txt (aap ke URLs)
        │
        ▼
[1] DOWNLOAD — yt-dlp se 720p source (GitHub runner ke disk par)
        │
        ▼
[2] TRANSCRIBE — Groq Whisper large-v3 (cloud, free)
     ↳ fail ho to: faster-whisper CPU par (local, no key)
     ↳ YA WHISPER_PROVIDER=local pin — kabhi cloud nahi
        │
        ▼
[3] HIGHLIGHTS — LLM transcript padh ke viral moments rank karta hai
     (Groq 70B → Cerebras (optional) → local Qwen 3B)
     ↳ 20–40s duration lock CODE mein enforce hota hai
     ↳ 3-second hook + DM-share polarity rules prompt mein
        │
        ▼
[4] CROP — ffmpeg se cut + OpenCV face-tracking se 9:16 vertical reframe
        │
        ▼
[5] METADATA — ek LLM call se YouTube + Instagram dono payloads
     (search-mirror first line, 40–60 char titles, ≤60 char IG hook)
     ↳ "#ad #sponsored" CODE mein har path par lagta hai (FTC)
        │
        ▼
[6] OUTPUT — mp4 + dono platform ke JSON files (Artifacts mein download)
        │
        ├──► [7a] YOUTUBE QUEUE — resumable upload (privacy=private default)
        │        3/run, 6/day quota-safety, 3-strike, auto-register
        │
        └──► [7b] INSTAGRAM QUEUE — Graph API container → publish
                 GitHub Release se public URL staging (temporary)
                 3/run, 3-strike, 60-day token auto-roll
        │
        ▼
[8] FEEDBACK LOOP — posted clips ke ASLI numbers (retention/views/reach)
     har run par khicha jata hai → "TOP performers / avoid these"
     block agli videos ke prompts mein → system seekhta rehta hai
        │
        ▼
[9] TRENDS (optional) — kal/aaj kya chal raha hai (niche set ho to)
     prompt mein framing-bias ke taur par
```

**Cron:** Din mein 4 baar (00:00, 06:00, 12:00, 18:00 UTC) = 4×/day. Ya kabhi bhi manually Actions tab se.

---

## 2. 🧰 Kya-kya Use Hua Hai (tech stack — naam, kaam, cost)

| Component | Kya use hua | Kaam kya hai | Cost |
|---|---|---|---|
| Python 3.10 | Language | Sab kuch | $0 |
| **yt-dlp** | Downloader | YouTube video download, trend search | $0 |
| **ffmpeg** | Video engine | Cut, audio extract, A/V mux | $0 |
| **OpenCV (headless)** | Reframe | Face-detect (Haar cascade) se vertical crop | $0 |
| **Groq Whisper large-v3-turbo** | Transcription (primary) | Audio→text timestamps; 8h/day free | $0 |
| **faster-whisper (CTranslate2)** | Transcription fallback | CPU par, no key, ~7–8 min/hr audio | $0 |
| **Groq Llama-3.3-70B** | LLM (primary) | Highlights + metadata; free key | $0 |
| **Cerebras** | LLM fallback (optional) | ⚠️ July 2026 se card maangta hai; skip kar do — doomsday tier hai | $0 (unset) |
| **llama.cpp + Qwen2.5-3B Q4_K_M** | LLM doomsday tier | CPU par, 3–8 min/call, JSON-grammar enforced — kabhi marne nahi deta | $0 |
| **YouTube Data API v3** | Auto-upload | Resumable upload, 10k units/day free | $0 |
| **YouTube Analytics API** | Feedback | Retention, views, likes, shares (read-only) | $0 |
| **Meta Graph API v21.0** | IG Reels upload + insights | Container→publish flow, reach/shares/saves | $0 (FB dev app free) |
| **GitHub Releases** | IG media staging | Temporary public URL (upload ke baad delete) | $0 |
| **GitHub Actions (ubuntu-latest)** | Compute | 4-core runner, public repo = unlimited minutes | $0 |
| **actions/cache** | State | Ledgers, queues, models (~2GB), .srt caches | $0 |
| **stdlib urllib** | HTTP | Koi bhaari SDK runner par nahi (sirf JSON GET/POST) | $0 |
| **JSON ledgers** | Storage | processed/failed URLs, queues, registry, stats — cache mein persist | $0 |
| **google-auth-oauthlib** | Setup-time only | Ek baar OAuth token mint karne ko, local machine par | $0 |

**Providers (final stack rule):** Groq Whisper primary → local faster-whisper. Groq LLM primary → Cerebras (agar key ho) → local Qwen 3B. Aur `WHISPER_PROVIDER=local` / `LLM_PROVIDER=local` pins se backend kabhi cloud ko chhuta hi nahi — 100% local mode bhi chalta hai.

---

## 3. 📁 Files Ka Kaam (har ek, ek line mein)

| File | Kaam |
|---|---|
| `main.py` | Ek URL clip karne ka CLI (`python main.py <url>`) |
| `campaign_runner.py` | Campaign engine: URLs list process, 3-strike, queues drain, banners |
| `shorts_generator/config.py` | Saari settings/env switches (typo-proof parsers ke saath) |
| `shorts_generator/pipeline.py` | Orchestrator: download→transcribe→highlights→crop→metadata |
| `shorts_generator/local/downloader.py` | yt-dlp wrapper + local file support + cookies |
| `shorts_generator/local/clipper.py` | ffmpeg cut + OpenCV vertical reframe |
| `shorts_generator/transcriber.py` | Groq Whisper ↔ local failover + .srt cache + chunking |
| `shorts_generator/local/whisper.py` | faster-whisper CPU loader |
| `shorts_generator/groq_client.py` | LLM 3-tier chain + retry + circuit breakers + provider pin |
| `shorts_generator/local/llm.py` | Qwen 3B llama.cpp (self-healing model download) |
| `shorts_generator/highlights.py` | Viral moments: prompt, 20–40s lock, long-video chunking, dedupe |
| `shorts_generator/metadata.py` | YT+IG payloads, hook caps, FTC disclosure enforcement |
| `shorts_generator/trends.py` | Trend context (yt-dlp search + Google Trends RSS, cached, fail-soft) |
| `shorts_generator/feedback.py` | Registry, YT Analytics + IG insights, scoring, prompt block |
| `shorts_generator/uploader_youtube.py` | YT resumable upload, quota ledger, queue, 3-strike |
| `shorts_generator/uploader_instagram.py` | IG container flow, GitHub-release staging, token roll, queue |
| `scripts/oauth_local_setup.py` | Ek baar: YouTube OAuth refresh token mint (local machine par) |
| `scripts/register_post.py` | Manual posts ko feedback registry mein daalna |
| `.github/workflows/run_campaign.yml` | Cron 4×/day + manual run, caches, artifacts, timeouts |
| `AUDIT.md` | 4 audit passes ki poori history (20 findings → 20 fixes) |

---

## 4. 🛡️ Design ke Niyam (in hi ki wajah se system "hamesha zinda" hai)

1. **Kuch bhi fail ho → clipping kabhi nahi rukti.** Trends/feedback/upload ke fail hone se sirf logs aate hain.
2. **Har upload opt-in hai aur privacy=private se shuru hota hai** — kuch bhi galti se public nahi hota.
3. **Har queue 3-strike follow karti hai** — hopeless cheezon par quota/barbaadi nahi.
4. **Har dollar-quota ke pehle cap hai** — YouTube 6/day hard ceiling (1600 units × 6 = 9,600 < 10,000 free).
5. **#ad #sponsored code mein** — LLM par bharosa nahi (FTC legal compliance).
6. **Secrets kabhi logs mein nahi** (adhivarso adversarial audit se sealed).
7. **State files corrupt ho jayein to khud theek hoti hain** (41 corruption cases tested).
8. **Computer band ho to queue samajhti hai** — missing file = no-strike drop.

---

## 5. 🔑 Aapko Kya Setting Karke Rakha Hai (quick recap)

- **Secret (Settings→Secrets→Actions):** `GROQ_API_KEY` ✅ (aapne daal diya)
- **Baad mein (YouTube ke liye):** `GOOGLE_CLIENT_ID/SECRET` + `YT_REFRESH_TOKEN` (scripts/oauth_local_setup.py se ek baar)
- **IG (jab chahiye):** `IG_ACCESS_TOKEN`, `IG_USER_ID`, (`IG_APP_ID/SECRET` recommended)
- **Variables (Settings→Variables):** `CAMPAIGN_NICHE`, `UPLOAD_ENABLED=true`, `YT_PRIVACY=private→public`, `IG_UPLOAD_ENABLED=true`, `WHISPER_PROVIDER=local?`, `LLM_PROVIDER=local?`

---

## 6. 📜 Safar (phases — samajhne ke liye)

| Phase | Kya bana |
|---|---|
| Base (upstream) | Clipping pipeline (yt-dlp + whisper + LLM) |
| P1 | Har platform ki alag metadata + trend-awareness |
| P2 | Feedback loop (apne channel ke data se seekhna) |
| P3 | YouTube auto-upload |
| P4 | Instagram Reels auto-upload + IG insights |
| Sovereignty | 100%-local mode (koi cloud key ki zaroorat nahi) |
| 4 Audit passes | 20 issues mile — sab fix (AUDIT.md) |

**Agla mantava (aapka haath mein):** `campaign/urls.txt` mein URLs → Actions tab → arena branch select → Run → log check. First live run hi final stamp hai. 🚀
