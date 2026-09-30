"""Offline self-test for the yt-dlp player_client rotation in
shorts_generator/local/downloader.py.

Injects a fake yt_dlp module, so the bot-check retry/classification logic is
exercised with ZERO network access — safe to run anywhere, including CI.

    python3 scripts/selftest_downloader.py     # exit 0 = all assertions pass
"""
import contextlib, io, os, sys, tempfile, types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BOT = ("ERROR: [youtube] ULsyvuvg-NU: Sign in to confirm you\u2019re not a bot. "
       "Use --cookies-from-browser or --cookies for the authentication.")
RELOAD = "ERROR: [youtube] ULsyvuvg-NU: The page needs to be reloaded."
PRIVATE = "ERROR: [youtube] xxxx: Private video. Sign in if you've been granted access to this video"
FORMAT_ERR = "ERROR: [youtube] ULsyvuvg-NU: Requested format is not available."


class FakeError(Exception):
    pass


def make_fake_ytdlp(behaviour, calls, partial_events=None, phantom_clients=None):
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
            # A real successful download leaves BYTES on disk. If phantom_clients
            # includes this client, simulate returning metadata without writing a file.
            if phantom_clients and self.client in phantom_clients:
                return {"id": "ULsyvuvg-NU", "ext": "mp4"}
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


def run(name, behaviour, url="https://www.youtube.com/watch?v=ULsyvuvg-NU", env=None, partial_events=None, phantom_clients=None):
    calls = []
    sys.modules["yt_dlp"] = make_fake_ytdlp(behaviour, calls, partial_events, phantom_clients)
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
print("A. default reload refusal, android_vr success")
n, status, res, calls = run("Test A", lambda c: RELOAD if c == "default" else None)
print(f"   clients tried: {calls}")
check(status == "OK", "download succeeded after reload rotation")
check(calls == ["default", "android_vr"], "expected clients: ['default', 'android_vr']")

print("\nB. default reload refusal, tv_simply format-unavailable, android_vr success")
def b_behaviour(c):
    if c == "default":
        return RELOAD
    if c == "tv_simply":
        return FORMAT_ERR
    return None

n, status, res, calls = run(
    "Test B",
    b_behaviour,
    env={"YTDLP_PLAYER_CLIENTS": "default,tv_simply,android_vr"}
)
print(f"   clients tried: {calls}")
check(status == "OK", "download succeeded on android_vr after tv_simply format failure")
check(calls == ["default", "tv_simply", "android_vr"], "expected clients: ['default', 'tv_simply', 'android_vr']")

print("\nC. every allowed fallback returns format-unavailable")
n, status, res, calls = run("Test C", lambda c: FORMAT_ERR)
print(f"   clients tried: {calls}")
check(status == "RuntimeError", "all retryable clients attempted, then one safe RuntimeError")
check(calls == ["default", "android_vr", "tv", "web_safari"], f"attempted all 4 default clients: {calls}")
check("usable formats or required a po token" in res.lower(), "safe final error mentions usable formats or PO Token")

print("\nD. private/deleted/geo video: only first client attempted and fails fast")
for message, label in (
    (PRIVATE, "private"),
    ("Video unavailable. This video has been removed", "deleted"),
    ("Video not available in your country. Please sign in", "geo"),
    ("Copyright claim. Sign in to confirm", "copyright"),
):
    _, status, res, calls = run("Test D: " + label, lambda c, msg=message: msg)
    check(calls == ["default"] and status == "RuntimeError", label + " only first client attempted and fails fast")
    check("unavailable" in res.lower(), label + " gets categorized guidance")

print("\nE. malformed/empty cookie file: safe actionable error; no cookie values in output")
with tempfile.TemporaryDirectory() as td:
    missing = os.path.join(td, "absent.txt")
    empty = os.path.join(td, "empty.txt")
    malformed = os.path.join(td, "malformed.txt")
    open(empty, "wb").close()
    with open(malformed, "w") as fh:
        fh.write("not a Netscape cookies file with hidden values")
    for path in (missing, empty, malformed):
        _, status, res, calls = run("Test E", lambda c: None,
                                    env={"YTDLP_COOKIES_FILE": path})
        check(status == "RuntimeError" and not calls and "Netscape" in res,
              "cookie preflight fails before yt-dlp without leaking contents")
        check(path not in res and "hidden values" not in res, "no cookie path/values exposed")

print("\nF. successful yt-dlp metadata with no real file: rejected and next client attempted")
n, status, res, calls = run("Test F", lambda c: None, phantom_clients=["default"])
print(f"   clients tried: {calls}")
check(status == "OK", "rejected phantom metadata on default and recovered on android_vr")
check(calls == ["default", "android_vr"], "default phantom rejected, rotated to android_vr")

print("\nT1: default client bot-checked, android_vr works -> must rotate and succeed")
n, status, res, calls = run("T1", lambda c: BOT if c == "default" else None)
print(f"   clients tried: {calls}")
check(status == "OK", "download succeeded after rotation")
check(calls == ["default", "android_vr"], "tried default then android_vr (stopped at first success)")

print("\nT2: every client bot-checked -> RuntimeError with actionable guidance")
n, status, res, calls = run("T2", lambda c: BOT)
print(f"   clients tried: {calls}")
check(status == "RuntimeError", "raised RuntimeError (not a raw yt-dlp error)")
check(len(calls) == 4, f"exhausted all 4 default clients (got {len(calls)})")
check("YT_COOKIES_B64" in res, "error names the YT_COOKIES_B64 remedy")
check("NOT configured" in res, "error states cookies were absent")

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

print("\nT7: a 'successful' extract that wrote NO file on all clients is not accepted")
n, status, res, calls = run(
    "T7",
    lambda c: None,
    phantom_clients=["default", "android_vr", "tv", "web_safari"]
)
check(status == "RuntimeError", "phantom download rejected -> " + str(res).splitlines()[0][:70])
check("no usable file" in res.lower() or "refused every" in res.lower(),
      "phantom error message has appropriate guidance")
check(calls == ["default", "android_vr", "tv", "web_safari"], "attempted all clients before failing phantom")

print("\nT8: exact reload error rotates, purges scratch, stops at first valid file")
partial_events = []
n, status, res, calls = run("T8", lambda c: RELOAD if c == "default" else None,
                            partial_events=partial_events)
check(status == "OK", "reload error recovered")
check(calls == ["default", "android_vr"], "default then android_vr only")
check(partial_events == [True], "partial files purged before next client")

print("\nT9: all clients return reload error -> one safe final RuntimeError")
n, status, res, calls = run("T9", lambda c: RELOAD)
check(status == "RuntimeError", "final RuntimeError raised")
check(calls == ["default", "android_vr", "tv", "web_safari"],
      "exact built-in rotation order exhausted")
check("IP block or stale/invalid cookie session" in res, "actionable refusal diagnosis")

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
    check(status == "RuntimeError" and len(calls) == 4, "valid arbitrary cookie name accepted; refusal rotates")
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
