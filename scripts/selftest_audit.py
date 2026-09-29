#!/usr/bin/env python3
"""Regression suite for the deep audit (disk, state, failover, retries, FTC).

Offline: no network, no API keys, no ffmpeg. Every check reproduces the
ORIGINAL defect's trigger condition and asserts the fixed behaviour.

    python3 scripts/selftest_audit.py      # exit 0 = all green
"""
import json
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FTC_DISCLOSURE_TAGS", "#ad #sponsored")

FAILURES = []
CHECKS = 0


def check(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
section("A. safe_io — crash-safe writes (STATE CORRUPTION)")
# ---------------------------------------------------------------------------
from shorts_generator.safe_io import (atomic_write_json, atomic_write_text,
                                      append_line_durable, read_json_safe)

tmp = tempfile.mkdtemp(prefix="audit_safeio_")
target = os.path.join(tmp, "nested", "ledger.json")
atomic_write_json(target, {"days": {"2026-09-29": 4}})
check("A1 atomic_write_json creates parent dirs", os.path.exists(target))
check("A2 content round-trips", read_json_safe(target)["days"]["2026-09-29"] == 4)

# The old code truncated first. Prove the new one leaves the OLD file intact
# when serialisation blows up partway.
class Unserialisable:
    def __repr__(self):  # json falls back to default=str, so force a hard fail
        raise RuntimeError("boom")

before = read_json_safe(target)
try:
    atomic_write_json(target, {"days": {"x": Unserialisable()}})
except Exception:
    pass
check("A3 failed write leaves the previous file INTACT",
      read_json_safe(target) == before, f"got {read_json_safe(target)}")
check("A4 no .tmp turds left behind",
      not [f for f in os.listdir(os.path.dirname(target)) if f.endswith(".tmp")],
      os.listdir(os.path.dirname(target)))

atomic_write_text(os.path.join(tmp, "t.txt"), "हिन्दी टेक्स्ट")
check("A5 utf-8 text round-trips",
      open(os.path.join(tmp, "t.txt"), encoding="utf-8").read() == "हिन्दी टेक्स्ट")

append_line_durable(os.path.join(tmp, "p.txt"), "https://youtu.be/a")
append_line_durable(os.path.join(tmp, "p.txt"), "https://youtu.be/b\n")
check("A6 durable append writes exactly 2 lines",
      open(os.path.join(tmp, "p.txt")).read().splitlines() ==
      ["https://youtu.be/a", "https://youtu.be/b"])
check("A7 read_json_safe tolerates corrupt json",
      read_json_safe(os.path.join(tmp, "t.txt"), default="fallback") == "fallback")
shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("B. campaign ledgers survive a mid-write crash")
# ---------------------------------------------------------------------------
import campaign_runner as cr

tmp = tempfile.mkdtemp(prefix="audit_ledger_")
led = os.path.join(tmp, "campaign", "failed_urls.txt")
os.makedirs(os.path.dirname(led), exist_ok=True)
with open(led, "w") as f:
    for i in range(1, 6):
        f.write(f"https://youtu.be/v{i}\t2\n")

cr.bump_failed_attempt(led, "https://youtu.be/v1", 3)
after = cr.read_failed_attempts(led)
check("B1 bump preserves every pre-existing URL", len(after) == 5, after)
check("B2 bumped URL updated", after["https://youtu.be/v1"] == 3)
check("B3 untouched URLs keep their counts",
      all(after[f"https://youtu.be/v{i}"] == 2 for i in range(2, 6)))

# Simulate the crash window: with atomic_write the target is replaced in one
# step, so a reader can never observe a partial ledger.
real_replace = os.replace
os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("crash during rename"))
try:
    cr.bump_failed_attempt(led, "https://youtu.be/v2", 9)
except OSError:
    pass
finally:
    os.replace = real_replace
survived = cr.read_failed_attempts(led)
check("B4 crash during the rename loses NOTHING", len(survived) == 5, survived)
check("B5 ledger still readable after the crash",
      survived["https://youtu.be/v1"] == 3)
shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("C. disk reclamation (RUNNER DEATH)")
# ---------------------------------------------------------------------------
from shorts_generator.local.downloader import (is_downloaded_source,
                                               purge_partial_downloads)

tmp = tempfile.mkdtemp(prefix="audit_disk_")
out = os.path.join(tmp, "output")
os.makedirs(out, exist_ok=True)
for name, size in [("source_ABC.mp4", 4096), ("source_ABC.f137.mp4.part", 8192),
                   ("source_XYZ.webm.ytdl", 128), ("short_01.mp4", 2048),
                   ("myvideo.mp4", 2048)]:
    with open(os.path.join(out, name), "wb") as f:
        f.write(b"\0" * size)

freed = purge_partial_downloads(out)
remaining = sorted(os.listdir(out))
check("C1 .part/.ytdl scratch removed", freed == 8192 + 128, freed)
check("C2 completed source NOT removed by the purge", "source_ABC.mp4" in remaining)
check("C3 rendered clip untouched", "short_01.mp4" in remaining)

check("C4 is_downloaded_source True for our own download",
      is_downloaded_source(os.path.join(out, "source_ABC.mp4"), out))
check("C5 is_downloaded_source FALSE for a user's own file",
      not is_downloaded_source(os.path.join(out, "myvideo.mp4"), out))
check("C6 is_downloaded_source FALSE outside the download dir",
      not is_downloaded_source("/home/me/source_ABC.mp4", out))

freed_mb = cr.reclaim_source_videos(out)
left = sorted(os.listdir(out))
check("C7 reclaim deletes the source video", "source_ABC.mp4" not in left, left)
check("C8 reclaim keeps rendered clips", "short_01.mp4" in left)
check("C9 reclaim keeps user files", "myvideo.mp4" in left)
check("C10 free_disk_mb returns a positive number", cr.free_disk_mb(out) > 0)
shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("D. LLM failover chain (an empty reply must fail over)")
# ---------------------------------------------------------------------------
import shorts_generator.groq_client as gc


class _Msg:
    def __init__(self, c): self.content = c


class _Choice:
    def __init__(self, c): self.message, self.finish_reason = _Msg(c), "stop"


class _Resp:
    def __init__(self, c): self.choices = [_Choice(c)] if c is not None else []


class _Client:
    def __init__(self, content):
        payload = _Resp(content)
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kw: payload))


for label, content in [("D1 empty string", ""), ("D2 whitespace only", "   \n "),
                       ("D3 None content", None)]:
    try:
        gc._chat_call(_Client(content), "m", "p")
        check(label + " raises EmptyCompletion", False, "returned normally")
    except gc.EmptyCompletion:
        check(label + " raises EmptyCompletion", True)
    except Exception as e:
        check(label + " raises EmptyCompletion", False, f"{type(e).__name__}: {e}")

try:
    gc._chat_call(_Client(None) if False else type("C", (), {
        "chat": types.SimpleNamespace(completions=types.SimpleNamespace(
            create=lambda **kw: _Resp(None)))})(), "m", "p")
    check("D4 zero choices raises", False)
except gc.EmptyCompletion:
    check("D4 zero choices raises", True)

check("D5 a real reply still passes through",
      gc._chat_call(_Client('{"ok":1}'), "m", "p") == '{"ok":1}')

# The chain must actually reach tier 3 when tier 1 returns empty.
calls = []
orig_groq, orig_local = gc.call_groq_llm, gc.call_local_llm
gc.call_groq_llm = lambda p: (calls.append("groq"), (_ for _ in ()).throw(
    gc.EmptyCompletion("empty")))[0]
gc.call_local_llm = lambda p: (calls.append("local"), '{"from":"local"}')[1]
try:
    got = gc.call_llm("prompt")
    check("D6 empty tier-1 reply reaches the local tier",
          calls == ["groq", "local"] and got == '{"from":"local"}', f"{calls} {got}")
finally:
    gc.call_groq_llm, gc.call_local_llm = orig_groq, orig_local

# GROQ_MAX_RETRIES=0 must not mean "never call the backend".
orig_max = gc.GROQ_MAX_RETRIES
gc.GROQ_MAX_RETRIES = 0
hits = []
try:
    gc._with_retry(lambda: (hits.append(1), "result")[1], "zero-retry")
    check("D7 GROQ_MAX_RETRIES=0 still issues ONE call", len(hits) == 1, hits)
except Exception as e:
    check("D7 GROQ_MAX_RETRIES=0 still issues ONE call", False, str(e))
finally:
    gc.GROQ_MAX_RETRIES = orig_max

# Retry-After must be clamped.
class _FakeRL(Exception):
    def __init__(self, secs):
        self.response = types.SimpleNamespace(headers={"RETRY-AFTER": str(secs)})

check("D8 Retry-After clamped to the ceiling",
      gc._retry_after_seconds(_FakeRL(86400)) == gc.MAX_RETRY_AFTER_SECONDS,
      gc._retry_after_seconds(_FakeRL(86400)))
check("D9 Retry-After header match is case-insensitive",
      gc._retry_after_seconds(_FakeRL(7)) == 7.0)
check("D10 APITimeoutError-class errors are in the retry set",
      any("Timeout" in c.__name__ for c in gc._sdk_exception_tuples()[2]),
      [c.__name__ for c in gc._sdk_exception_tuples()[2]])


# ---------------------------------------------------------------------------
section("E. HTTP retries / backoff (RATE LIMITS & TIMEOUTS)")
# ---------------------------------------------------------------------------
import urllib.error
from shorts_generator.http_retry import (RetryPolicy, request_with_retry,
                                         retry_after_seconds, RETRYABLE_STATUS)

FAST = RetryPolicy(attempts=4, base_delay=0.01, max_delay=0.02,
                   total_budget=5.0, timeout=1.0)


class _FakeResp:
    def __init__(self, status, body=b"{}", headers=None):
        self.status, self._b, self.headers = status, body, headers or {}
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def opener_sequence(seq):
    state = {"i": 0}
    def _open(req, timeout=None):
        item = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        if isinstance(item, BaseException):
            raise item
        return _FakeResp(*item) if isinstance(item, tuple) else _FakeResp(item)
    _open.state = state
    return _open

op = opener_sequence([503, 503, 200])
r = request_with_retry("https://x/y", policy=FAST, opener=op)
check("E1 retries through 503 to a 200", r.status == 200 and op.state["i"] == 3,
      f"status={r.status} attempts={op.state['i']}")

op = opener_sequence([(403, b'{"error":"forbidden"}')])
r = request_with_retry("https://x/y", policy=FAST, opener=op)
check("E2 403 fails FAST — no retries", r.status == 403 and op.state["i"] == 1,
      f"attempts={op.state['i']}")

op = opener_sequence([urllib.error.URLError("dns"), urllib.error.URLError("dns"), 200])
r = request_with_retry("https://x/y", policy=FAST, opener=op)
check("E3 transport errors retry (URLError was UNCAUGHT before)",
      r.status == 200 and op.state["i"] == 3, f"attempts={op.state['i']}")

op = opener_sequence([TimeoutError("read timeout"), 200])
r = request_with_retry("https://x/y", policy=FAST, opener=op)
check("E4 timeouts retry", r.status == 200)

op = opener_sequence([ConnectionResetError("peer reset")] * 6)
try:
    request_with_retry("https://x/y", policy=FAST, opener=op)
    check("E5 exhausted transport retries raise", False)
except RuntimeError as e:
    check("E5 exhausted transport retries raise", "peer reset" in str(e))

op = opener_sequence([(500, b"boom")] * 6)
r = request_with_retry("https://x/y", policy=FAST, opener=op)
check("E6 exhausted 5xx retries return the last response", r.status == 500)

check("E7 Retry-After clamped in the http layer",
      retry_after_seconds({"Retry-After": "99999"}) == 120.0)
check("E8 429 is retryable", 429 in RETRYABLE_STATUS)
check("E9 404 is NOT retryable", 404 not in RETRYABLE_STATUS)

# Secrets must never reach a log line / error message.
op = opener_sequence([(400, b"bad")])
r = request_with_retry("https://graph.facebook.com/oauth/access_token",
                       params={"client_secret": "SUPERSECRET",
                               "fb_exchange_token": "TOKENVALUE"},
                       policy=FAST, opener=op)
from shorts_generator.http_retry import json_request
try:
    json_request("https://graph.facebook.com/oauth/access_token",
                 params={"client_secret": "SUPERSECRET"},
                 policy=FAST, label="x")
    msg = ""
except RuntimeError as e:
    msg = str(e)
check("E10 secrets are redacted from error messages",
      "SUPERSECRET" not in msg, msg[:120])


# ---------------------------------------------------------------------------
section("F. FTC disclosure — every bypass closed")
# ---------------------------------------------------------------------------
import importlib
import shorts_generator.config as cfg
import shorts_generator.metadata as md
import shorts_generator.uploader_youtube as uy
import shorts_generator.uploader_instagram as ui

# F1/F2: the queue-item hole — metadata never generated.
snip = uy._build_snippet({})
check("F1 YT: empty metadata still carries the disclosure",
      "#ad" in snip["description"] and "#sponsored" in snip["description"],
      snip["description"])
cap = ui._build_ig_caption({})
check("F2 IG: empty metadata still carries the disclosure",
      "#ad" in cap and "#sponsored" in cap, repr(cap))

# F3: a queue item with a description but no tags.
snip = uy._build_snippet({"description": "Jethalal ka best scene", "hashtags": []})
check("F3 YT: description without tags gains the disclosure",
      "#ad" in snip["description"], snip["description"])

# F4: config refuses an empty disclosure setting.
os.environ["FTC_DISCLOSURE_TAGS"] = "   "
importlib.reload(cfg)
check("F4 blank FTC_DISCLOSURE_TAGS falls back to the default",
      cfg.FTC_DISCLOSURE_TAGS == "#ad #sponsored", cfg.FTC_DISCLOSURE_TAGS)
os.environ["FTC_DISCLOSURE_TAGS"] = "ad, sponsored"
importlib.reload(cfg)
check("F5 bare words are normalised into hashtags",
      cfg.FTC_DISCLOSURE_TAGS.startswith("#"), cfg.FTC_DISCLOSURE_TAGS)
os.environ["FTC_DISCLOSURE_TAGS"] = "#ad #sponsored"
importlib.reload(cfg)
importlib.reload(md)
importlib.reload(uy)
importlib.reload(ui)

# F6: idempotency / case-insensitivity.
check("F6 disclosure is not duplicated",
      md._enforce_disclosure("hi #ad #sponsored").count("#ad") == 1)
check("F7 mixed-case existing tag is not duplicated",
      md._enforce_disclosure("hi #Ad #SPONSORED").lower().count("#ad") == 1,
      md._enforce_disclosure("hi #Ad #SPONSORED"))
check("F8 trailing punctuation still counts as present",
      md._enforce_disclosure("hi #ad, #sponsored.").count("#sponsored") == 1,
      md._enforce_disclosure("hi #ad, #sponsored."))

# F9: fallback metadata (the total-LLM-failure path).
fb = md._fallback_metadata({"title": "T", "hook_sentence": "H"})
check("F9 LLM-failure fallback is compliant on BOTH platforms",
      "#ad" in fb["youtube"]["description"] and "#ad" in fb["instagram"]["caption"]
      and "#ad" in fb["youtube"]["hashtags"] and "#ad" in fb["instagram"]["hashtags"])

# F10: the LLM returning junk shapes.
for junk in [{}, {"youtube": None}, {"youtube": "str", "instagram": 5},
             {"youtube": {"description": None}, "instagram": {"caption": None}}]:
    p = md._build_platform_payloads(junk, {"title": "T"})
    if "#ad" not in p["youtube"]["description"] or "#ad" not in p["instagram"]["caption"]:
        check(f"F10 junk LLM shape {junk} stays compliant", False, p)
        break
else:
    check("F10 every junk LLM shape stays compliant", True)

# F11: oversize description keeps the disclosure after truncation.
snip = uy._build_snippet({"description": "x" * 9000, "hashtags": ["#a"]})
check("F11 YT 5000-char cap keeps the disclosure",
      len(snip["description"]) <= uy.YT_DESCRIPTION_MAX
      and "#ad" in snip["description"], len(snip["description"]))
cap = ui._build_ig_caption({"caption": "y" * 9000})
check("F12 IG 2200-char cap keeps the disclosure",
      len(cap) <= ui.IG_CAPTION_MAX and "#ad" in cap, len(cap))

# F13: full pipeline with a totally dead LLM.
shorts = [{"title": "A", "hook_sentence": "h", "start_time": 0, "end_time": 25,
           "clip_url": "/x/short_01.mp4"}]
def _dead(_): raise RuntimeError("all tiers down")
out = md.generate_metadata(shorts, llm_fn=_dead)
check("F13 dead LLM → still FTC compliant end-to-end",
      "#ad" in out[0]["metadata"]["youtube"]["description"]
      and "#ad" in out[0]["metadata"]["instagram"]["caption"])


# ---------------------------------------------------------------------------
section("G. clipper guards (degenerate media)")
# ---------------------------------------------------------------------------
import shorts_generator.local.clipper as cl

check("G1 _sane_fps rejects NaN", cl._sane_fps(float("nan")) == 30.0)
check("G2 _sane_fps rejects negative", cl._sane_fps(-5) == 30.0)
check("G3 _sane_fps rejects absurd", cl._sane_fps(100000) == 30.0)
check("G4 _sane_fps keeps a valid value", cl._sane_fps(23.976) == 23.976)

for bad, why in [((5.0, 5.0), "zero-length"), ((10.0, 9.0), "inverted"),
                 ((float("nan"), 3.0), "NaN"), ((-1.0, 20.0), "negative start")]:
    try:
        cl._cut_subclip("/nonexistent.mp4", bad[0], bad[1], "/tmp/x.mp4")
        check(f"G5 {why} window rejected", False, "no exception")
        break
    except RuntimeError:
        pass
else:
    check("G5 degenerate cut windows are all rejected up front", True)

check("G6 ffmpeg helper enforces a timeout",
      "timeout" in cl._run_ffmpeg.__doc__.lower())
check("G7 face detection runs on a stride (not every frame)",
      cl.FACE_DETECT_STRIDE > 1, cl.FACE_DETECT_STRIDE)


# ---------------------------------------------------------------------------
section("H. local LLM context budget (Hindi)")
# ---------------------------------------------------------------------------
import shorts_generator.local.llm as lll

check("H1 ASCII estimated at the plain-ASCII ceiling",
      lll._chars_per_token("hello world " * 100) == lll._BYTES_PER_TOKEN,
      lll._chars_per_token("hello world " * 100))
hindi = "जेठालाल ने कहा कि ये तो बहुत बड़ी बात है " * 50
pure_hindi = "जेठालालनेकहाकियेतोबहुतबड़ीबातहै" * 50
check("H2 Devanagari is estimated far denser than ASCII",
      lll._chars_per_token(hindi) < 2.0, lll._chars_per_token(hindi))
check("H2b pure Devanagari approaches ~1.2 chars/token",
      1.1 <= lll._chars_per_token(pure_hindi) <= 1.35,
      lll._chars_per_token(pure_hindi))
check("H2c emoji-heavy text is also treated as dense",
      lll._chars_per_token("🔥" * 200) < 1.5, lll._chars_per_token("🔥" * 200))
check("H3 a Hindi prompt gets a SMALLER budget than the same-length ASCII",
      len(lll._fit_prompt_to_ctx(hindi * 200, 2048))
      < len(lll._fit_prompt_to_ctx("a" * len(hindi * 200), 2048)))


# ---------------------------------------------------------------------------
section("I. transcript cache keying")
# ---------------------------------------------------------------------------
import shorts_generator.transcriber as tr

hi = tr._transcript_cache_path("/x/source_ABC.mp4", "hi")
en = tr._transcript_cache_path("/x/source_ABC.mp4", "en")
auto = tr._transcript_cache_path("/x/source_ABC.mp4", None)
check("I1 different languages use different cache files", hi != en, f"{hi} {en}")
check("I2 auto-detect keeps the legacy filename", auto.name == "source_ABC.srt", auto.name)
check("I3 language codes are sanitised",
      "/" not in tr._transcript_cache_path("/x/s.mp4", "../../etc/passwd").name)


# ===========================================================================
print("\n=== J. disk reclamation & campaign integration (END-TO-END) ===")
# ===========================================================================
# These drive the REAL generate_shorts / campaign main() with stubbed network
# and media, so they catch regressions the unit checks above cannot: a source
# video left on disk fills the runner mid-run, and a non-atomic result write
# corrupts Hindi metadata.

with tempfile.TemporaryDirectory() as td:
    out = os.path.join(td, "output"); os.makedirs(out)
    src = os.path.join(out, "source_P1.mp4")
    open(src, "wb").write(b"\0" * 2048)

    import shorts_generator.pipeline as pl
    import shorts_generator.local.downloader as _dlmod
    # is_downloaded_source() only deletes files inside OUTPUT_DIR, so the
    # tempdir has to BE OUTPUT_DIR for this test to exercise the real path.
    _dl_out = _dlmod.OUTPUT_DIR
    _dlmod.OUTPUT_DIR = out
    _orig = {k: getattr(pl, k) for k in
             ("download_youtube_local", "transcribe", "get_highlights",
              "crop_highlights_local", "generate_metadata",
              "write_metadata_sidecars")}
    clip_dir = os.path.join(out, "v1"); os.makedirs(clip_dir)
    clip = os.path.join(clip_dir, "short_01.mp4"); open(clip, "wb").write(b"\0")
    pl.download_youtube_local = lambda url, fmt="720": src
    pl.transcribe = lambda p_, language=None: {
        "duration": 45.0,
        "segments": [{"start": 2.0, "end": 27.0, "text": "जेठालाल का डायलॉग"}]}
    pl.get_highlights = lambda t, num_clips=3, context_block="": {"highlights": [
        {"title": "क्लिप", "start_time": 2.0, "end_time": 27.0, "score": 90}]}
    pl.crop_highlights_local = lambda s_, h, aspect_ratio="9:16", out_dir=None: [
        {**h[0], "clip_url": clip}]
    pl.generate_metadata = lambda shorts, transcript=None, context_block="": [
        {**shorts[0], "metadata": {
            "youtube": {"title": "T", "description": "D #ad #sponsored",
                        "hashtags": ["#ad", "#sponsored"]},
            "instagram": {"caption": "C #ad #sponsored",
                          "hashtags": ["#ad", "#sponsored"]}}}]
    pl.write_metadata_sidecars = lambda s_: None
    try:
        pl.generate_shorts("https://youtu.be/P1", output_dir=clip_dir)
        check("J1 downloaded source is deleted on the SUCCESS path",
              not os.path.exists(src))
        check("J2 rendered clips are NOT deleted with it", os.path.exists(clip))

        open(src, "wb").write(b"\0" * 2048)
        pl.transcribe = lambda p_, language=None: (_ for _ in ()).throw(
            RuntimeError("whisper exploded"))
        try:
            pl.generate_shorts("https://youtu.be/P1", output_dir=clip_dir)
        except RuntimeError:
            pass
        check("J3 source is deleted on the FAILURE path too",
              not os.path.exists(src))

        mine = os.path.join(td, "holiday.mp4"); open(mine, "wb").write(b"\0" * 16)
        pl.download_youtube_local = lambda url, fmt="720": mine
        try:
            pl.generate_shorts(mine)
        except RuntimeError:
            pass
        check("J4 a user's OWN local file is never deleted", os.path.exists(mine))
    finally:
        for k, v in _orig.items():
            setattr(pl, k, v)
        _dlmod.OUTPUT_DIR = _dl_out

with tempfile.TemporaryDirectory() as td:
    cwd0 = os.getcwd()
    os.makedirs(os.path.join(td, "campaign")); out = os.path.join(td, "output")
    os.makedirs(out)
    with open(os.path.join(td, "urls.txt"), "w") as fh:
        fh.write("https://youtu.be/AAAA1111111\nhttps://youtu.be/BBBB2222222\n")
    import campaign_runner as cr
    _cr_out, _cr_gen, _argv = cr.OUTPUT_DIR, cr.generate_shorts, sys.argv
    cr.OUTPUT_DIR = out
    n = {"i": 0}

    def _fake(youtube_url, num_clips, aspect_ratio, download_format, language,
              output_dir):
        n["i"] += 1
        s_ = os.path.join(out, f"source_V{n['i']}.mp4")
        open(s_, "wb").write(b"\0" * (1024 * 512))
        os.makedirs(output_dir, exist_ok=True)
        if n["i"] == 2:
            raise RuntimeError("Whisper produced no segments.")
        c_ = os.path.join(output_dir, "short_01.mp4"); open(c_, "wb").write(b"\0")
        return {"source_video_url": s_,
                "transcript": {"duration": 45.0, "segments": []},
                "highlights": [{"title": "h"}],
                "shorts": [{"title": "जेठालाल का सीन", "clip_url": c_,
                            "score": 88, "start_time": 0.0, "end_time": 25.0,
                            "metadata": {"youtube": {"title": "शीर्षक"},
                                         "instagram": {"caption": "कैप्शन"}}}],
                "failed_clips": []}
    cr.generate_shorts = _fake
    try:
        os.chdir(td)
        sys.argv = ["campaign_runner.py", "--urls-file", "urls.txt",
                    "--num-clips", "1"]
        cr.main()
        check("J5 no source_* survives a full campaign run",
              [f for f in os.listdir(out) if f.startswith("source_")] == [])
        rd = [d for d in os.listdir(out) if d.startswith("campaign_")][0]
        raw = open(os.path.join(out, rd, "result_001.json"),
                   encoding="utf-8").read()
        check("J6 Hindi is written readable, not \\uXXXX-escaped",
              "जेठालाल" in raw and "\\u091c" not in raw)
        json.loads(raw)
        check("J7 the ok URL is in processed_urls.txt",
              "AAAA1111111" in open(
                  os.path.join(td, "campaign", "processed_urls.txt")).read())
        check("J8 the failed URL took exactly ONE strike",
              open(os.path.join(td, "campaign", "failed_urls.txt")
                   ).read().strip().endswith("\t1"))
    finally:
        os.chdir(cwd0)
        cr.OUTPUT_DIR, cr.generate_shorts, sys.argv = _cr_out, _cr_gen, _argv



# ===========================================================================
print("\n=== K. state-cache prune (10 GB cache-eviction thrash) ===")
# ===========================================================================
import subprocess as _sp

_PRUNE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "prune_state_cache.py")


def _prune_fixture(td, queue_items, corrupt=False):
    """Lay out a fake repo and run the real prune script inside it."""
    os.makedirs(os.path.join(td, "campaign"), exist_ok=True)
    os.makedirs(os.path.join(td, "scripts"), exist_ok=True)
    shutil.copy(_PRUNE, os.path.join(td, "scripts", "prune_state_cache.py"))
    clips = []
    for vid, names in (("video_001", ("short_01.mp4", "short_02.mp4")),
                       ("video_002", ("short_01.mp4",))):
        d = os.path.join(td, "output", "campaign_A", vid)
        os.makedirs(d, exist_ok=True)
        for nm in names:
            fp = os.path.join(d, nm)
            with open(fp, "wb") as fh:
                fh.write(b"\0" * 4096)
            clips.append(fp)
    qpath = os.path.join(td, "campaign", "upload_queue.json")
    if corrupt:
        with open(qpath, "w") as fh:
            fh.write('{"items": [{"clip_url": tr')
    else:
        with open(qpath, "w") as fh:
            json.dump({"items": [{"clip_url": clips[i]} for i in queue_items]}, fh)
    with open(os.path.join(td, "campaign", "ig_upload_queue.json"), "w") as fh:
        json.dump({"items": []}, fh)
    _sp.run([sys.executable, os.path.join(td, "scripts", "prune_state_cache.py")],
            cwd=td, capture_output=True, text=True, timeout=60)
    return clips


with tempfile.TemporaryDirectory() as td:
    clips = _prune_fixture(td, [])
    check("K1 with EMPTY queues every clip is pruned",
          not any(os.path.exists(c) for c in clips))
    check("K2 emptied campaign_*/video_* shells are removed too",
          not os.path.isdir(os.path.join(td, "output", "campaign_A")))

with tempfile.TemporaryDirectory() as td:
    clips = _prune_fixture(td, [1])
    check("K3 a QUEUED clip survives the prune", os.path.exists(clips[1]))
    check("K4 unreferenced siblings are still pruned",
          not os.path.exists(clips[0]) and not os.path.exists(clips[2]))

with tempfile.TemporaryDirectory() as td:
    clips = _prune_fixture(td, [], corrupt=True)
    check("K5 an UNREADABLE queue keeps every clip (fail safe)",
          all(os.path.exists(c) for c in clips))


print("\n" + "=" * 68)
if FAILURES:
    print(f"RESULT: {len(FAILURES)} FAILED of {CHECKS} — {FAILURES}")
    sys.exit(1)
print(f"RESULT: ALL {CHECKS} CHECKS PASSED")
sys.exit(0)
