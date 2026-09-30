"""Offline self-test for the yt-dlp player_client rotation in
shorts_generator/local/downloader.py.

Injects a fake yt_dlp module, so the bot-check retry/classification logic is
exercised with ZERO network access — safe to run anywhere, including CI.

    python3 scripts/selftest_downloader.py     # exit 0 = all assertions pass

Background: campaign run 36494468151 (2026-09-28) died because YouTube
bot-checked the GitHub runner IP and the downloader had no fallback client.
"""
import contextlib, io, os, sys, tempfile, types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BOT = ("ERROR: [youtube] ULsyvuvg-NU: Sign in to confirm you\u2019re not a bot. "
       "Use --cookies-from-browser or --cookies for the authentication.")
RELOAD = "ERROR: [youtube] ULsyvuvg-NU: The page needs to be reloaded."
PRIVATE = "ERROR: [youtube] xxxx: Private video. Sign in if you've been granted access to this video"


class FakeError(Exception):
    pass


def make_fake_ytdlp(behaviour, calls, partial_events=None):
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
            partial = os.path.join(os.path.dirname(self.opts["outtmpl"]),
                                   "source_ULsyvuvg-NU.f137.mp4.part")
            if msg:
                if partial_events is not None:
                    with open(partial, "wb") as fh:
                        fh.write(b"unfinished")
                raise FakeError(msg)
            if partial_events is not None:
                partial_events.append(not os.path.exists(partial))
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


def run(name, behaviour, url="https://www.youtube.com/watch?v=ULsyvuvg-NU", env=None, partial_events=None):
    calls = []
    sys.modules["yt_dlp"] = make_fake_ytdlp(behaviour, calls, partial_events)
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
check(status == "RuntimeError" and "private" in res.lower(), "private video fails fast with safe guidance")

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
check(_is_client_blocked(Exception(RELOAD)), "exact reload message classified as client refusal")
check(_is_client_blocked(Exception("Please reload the page")), "reload-the-page variant classified")
check(not _is_client_blocked(Exception(PRIVATE)), "private video NOT classified as client refusal")
check(not _is_client_blocked(Exception("Requested format is not available")), "format NOT classified as client refusal")

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

print("\nT8: exact reload error rotates, purges scratch, stops at first valid file")
partial_events = []
n, status, res, calls = run("T8", lambda c: RELOAD if c == "default" else None,
                            partial_events=partial_events)
check(status == "OK", "reload error recovered")
check(calls == ["default", "tv_simply"], "default then tv_simply only")
check(partial_events == [True], "partial files purged before next client")

print("\nT9: all clients return reload error -> one safe final RuntimeError")
n, status, res, calls = run("T9", lambda c: RELOAD)
check(status == "RuntimeError", "final RuntimeError raised")
check(calls == ["default", "tv_simply", "android_vr", "tv", "web_safari", "mweb"],
      "exact built-in rotation order exhausted")
check("IP block or stale/invalid cookie session" in res, "actionable refusal diagnosis")

print("\nT10: private, deleted, geo, copyright, format fail fast even with retry words")
for message, label in (
    (PRIVATE, "private"),
    ("Video unavailable. This video has been removed", "deleted"),
    ("Video not available in your country. Please sign in", "geo"),
    ("Copyright claim. Sign in to confirm", "copyright"),
    ("Requested format is not available", "format"),
):
    _, status, res, calls = run(label, lambda c, msg=message: msg)
    check(calls == ["default"] and status == "RuntimeError", label + " fails fast")
    check(("format" if label == "format" else "unavailable") in res.lower(),
          label + " gets categorized guidance")

print("\nT11: missing, empty and malformed cookie file produce safe diagnostics")
with tempfile.TemporaryDirectory() as td:
    missing = os.path.join(td, "absent.txt")
    empty = os.path.join(td, "empty.txt")
    malformed = os.path.join(td, "malformed.txt")
    open(empty, "wb").close()
    with open(malformed, "w") as fh:
        fh.write("not a Netscape cookies file with hidden values")
    for path in (missing, empty, malformed):
        _, status, res, calls = run("T11", lambda c: None,
                                    env={"YTDLP_COOKIES_FILE": path})
        check(status == "RuntimeError" and not calls and "Netscape" in res,
              "cookie preflight fails before yt-dlp without leaking contents")
        check(path not in res and "hidden values" not in res, "no cookie path/values exposed")

print("\nT12: valid Netscape cookie file, sensitive yt-dlp message never exposed")
secret = "VERY_PRIVATE_COOKIE_VALUE_123"
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "cookie-file")
    with open(path, "w") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        fh.write(f".youtube.com\tTRUE\t/\tTRUE\t9999999999\tANY_NAME\t{secret}\n")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _, status, res, calls = run("T12", lambda c: RELOAD + " " + secret,
                                    env={"YTDLP_COOKIES_FILE": path},
                                    url="https://www.youtube.com/watch?v=ULsyvuvg-NU&token=" + secret)
    check(status == "RuntimeError" and len(calls) == 6, "valid arbitrary cookie name accepted; refusal rotates")
    check(secret not in res + output.getvalue() and path not in res + output.getvalue(),
          "no sensitive value, URL query or cookie path in output/error")
    check("Cookies file was configured" in res, "session issue distinguished from missing cookies")

print("\nT13: unknown yt-dlp error does not echo secret in logs or exceptions")
output = io.StringIO()
with contextlib.redirect_stdout(output):
    _, status, res, calls = run("T13", lambda c: "Unexpected error: " + secret)
check(status == "RuntimeError" and calls == ["default"], "unclassified failure fails fast")
check(secret not in res + output.getvalue(), "unclassified error safely summarized")

print("\nT14: cookie-file rejection is distinguished and fails fast")
_, status, res, calls = run("T14", lambda c: "invalid Netscape format: " + secret)
check(status == "RuntimeError" and calls == ["default"] and "Cookie file rejected" in res,
      "yt-dlp malformed cookie diagnosis without retry burn")
check(secret not in res, "cookie value omitted from diagnostic")

print("\n" + "=" * 78)
print(f"RESULT: {'ALL CHECKS PASSED' if FAILS == 0 else str(FAILS) + ' CHECK(S) FAILED'}")
sys.exit(1 if FAILS else 0)
