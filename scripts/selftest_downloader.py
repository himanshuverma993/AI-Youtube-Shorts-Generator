"""Offline self-test for the yt-dlp player_client rotation in
shorts_generator/local/downloader.py.

Injects a fake yt_dlp module, so the bot-check retry/classification logic is
exercised with ZERO network access — safe to run anywhere, including CI.

    python3 scripts/selftest_downloader.py     # exit 0 = all assertions pass

Background: campaign run 36494468151 (2026-09-28) died because YouTube
bot-checked the GitHub runner IP and the downloader had no fallback client.
"""
import os, sys, tempfile, types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BOT = ("ERROR: [youtube] ULsyvuvg-NU: Sign in to confirm you\u2019re not a bot. "
       "Use --cookies-from-browser or --cookies for the authentication.")
PRIVATE = "ERROR: [youtube] xxxx: Private video. Sign in if you've been granted access to this video"


class FakeError(Exception):
    pass


def make_fake_ytdlp(behaviour, calls):
    """behaviour(client) -> None to succeed, or an exception message to raise."""
    mod = types.ModuleType("yt_dlp")

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            ea = opts.get("extractor_args") or {}
            self.client = (ea.get("youtube", {}).get("player_client") or ["default"])[0]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            calls.append(self.client)
            msg = behaviour(self.client)
            if msg:
                raise FakeError(msg)
            # A real successful download leaves BYTES on disk. The fake used
            # to return metadata only, which the downloader's new
            # empty-output guard correctly rejects — so the fixture now
            # writes a file, matching what yt-dlp actually does.
            info = {"id": "ULsyvuvg-NU", "ext": "mp4"}
            out = self.prepare_filename(info)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(b"\0" * 1024)
            return info

        def prepare_filename(self, info):
            return os.path.join(self.opts["outtmpl"].rsplit(os.sep, 1)[0],
                                f"source_{info['id']}.mp4")

    mod.YoutubeDL = FakeYDL
    mod.utils = types.SimpleNamespace(DownloadError=FakeError)
    return mod


def run(name, behaviour, url="https://www.youtube.com/watch?v=ULsyvuvg-NU", env=None):
    calls = []
    sys.modules["yt_dlp"] = make_fake_ytdlp(behaviour, calls)
    for k in list(sys.modules):
        if k.startswith("shorts_generator"):
            del sys.modules[k]
    old = dict(os.environ)
    os.environ.pop("YTDLP_PLAYER_CLIENTS", None)
    os.environ.pop("YTDLP_COOKIES_FILE", None)
    os.environ.update(env or {})
    try:
        from shorts_generator.local.downloader import download_youtube_local
        with tempfile.TemporaryDirectory() as td:
            try:
                res = download_youtube_local(url, fmt="720", out_dir=td)
                return name, "OK", res, calls
            except Exception as e:
                return name, type(e).__name__, str(e), calls
    finally:
        os.environ.clear()
        os.environ.update(old)


FAILS = 0


def check(cond, label):
    global FAILS
    print(("   PASS  " if cond else "   FAIL  ") + label)
    if not cond:
        FAILS += 1


print("=" * 78)
print("T1: default client bot-checked, tv_simply works -> must rotate and succeed")
n, status, res, calls = run("T1", lambda c: BOT if c == "default" else None)
print(f"   clients tried: {calls}")
check(status == "OK", "download succeeded after rotation")
check(calls == ["default", "tv_simply"], "tried default then tv_simply (stopped at first success)")

print("\nT2: every client bot-checked -> RuntimeError with actionable guidance")
n, status, res, calls = run("T2", lambda c: BOT)
print(f"   clients tried: {calls}")
check(status == "RuntimeError", "raised RuntimeError (not a raw yt-dlp error)")
check(len(calls) == 6, f"exhausted all 6 clients (got {len(calls)})")
check("YT_COOKIES_B64" in res, "error names the YT_COOKIES_B64 remedy")
check("NOT configured" in res, "error states cookies were absent")

print("\nT3: private video -> must FAIL FAST on the first client, no retry burn")
n, status, res, calls = run("T3", lambda c: PRIVATE)
print(f"   clients tried: {calls}")
check(calls == ["default"], "only one attempt made")
check(status == "FakeError", "original yt-dlp error propagated unchanged")

print("\nT4: YTDLP_PLAYER_CLIENTS override is honoured")
n, status, res, calls = run("T4", lambda c: BOT if c != "web_safari" else None,
                            env={"YTDLP_PLAYER_CLIENTS": "tv,web_safari"})
print(f"   clients tried: {calls}")
check(calls == ["tv", "web_safari"], "used exactly the override chain")
check(status == "OK", "succeeded on web_safari")

print("\nT5: non-YouTube URL -> single attempt, no rotation")
n, status, res, calls = run("T5", lambda c: BOT, url="https://example.com/video.mp4")
print(f"   clients tried: {calls}")
check(len(calls) == 1, "exactly one attempt for a non-YouTube host")

print("\nT6: smart-quote bot-check string is detected (U+2019 in \u201cyou\u2019re\u201d)")
from shorts_generator.local.downloader import _is_client_blocked
check(_is_client_blocked(Exception(BOT)), "curly-apostrophe bot-check classified as blocked")
check(not _is_client_blocked(Exception("Video unavailable")), "'Video unavailable' NOT treated as bot-check")
check(not _is_client_blocked(Exception("HTTP Error 404: Not Found")), "404 NOT treated as bot-check")

print("\nT7: a 'successful' extract that wrote NO file is not accepted")
# ---------------------------------------------------------------------------
# yt-dlp can report success for a format it then fails to merge. Returning
# that phantom path sent an empty/absent file into ffmpeg and the real cause
# surfaced three stages later as an unreadable-frame-size error.
class _PhantomYDL:
    def __init__(self, opts): self.opts = opts
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=True):
        _phantom_calls.append(1)
        return {"id": "ULsyvuvg-NU", "ext": "mp4"}
    def prepare_filename(self, info):
        return os.path.join(self.opts["outtmpl"].rsplit(os.sep, 1)[0],
                            f"source_{info['id']}.mp4")

_phantom_calls = []
mod = types.ModuleType("yt_dlp")
mod.YoutubeDL = _PhantomYDL
mod.utils = types.SimpleNamespace(DownloadError=FakeError)
with tempfile.TemporaryDirectory() as td:
    sys.modules["yt_dlp"] = mod
    from shorts_generator.local.downloader import download_youtube_local as _dl
    try:
        _dl("https://www.youtube.com/watch?v=ULsyvuvg-NU", out_dir=td)
        check(False, "phantom download rejected (it returned a path anyway)")
    except RuntimeError as e:
        check("no usable file" in str(e).lower() or "refused every" in str(e).lower(),
              "phantom download rejected -> " + str(e).splitlines()[0][:70])
    finally:
        sys.modules.pop("yt_dlp", None)

print("\n" + "=" * 78)
print(f"RESULT: {'ALL CHECKS PASSED' if FAILS == 0 else str(FAILS) + ' CHECK(S) FAILED'}")
sys.exit(1 if FAILS else 0)
