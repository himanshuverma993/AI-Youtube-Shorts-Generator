#!/usr/bin/env python3
"""Stage-by-stage diagnosis — answers "clips ban kyun nahi rahi?" without guessing.

The pipeline has five stages and a failure in stage 1 silently looks identical
to a failure in stage 5 from the outside: zero clips. Run 36494468151 died
1.03 seconds in, at the download, and every later stage never executed at all.

This script checks each stage INDEPENDENTLY so the first real breakage is
named, not inferred. It never reports PASS for something it did not actually
execute — anything it cannot run here is reported as SKIP with the reason.

Usage:
    python3 scripts/diagnose.py                      # everything offline-safe
    python3 scripts/diagnose.py --url <yt-url>       # + live download test
    python3 scripts/diagnose.py --file <video.mp4>   # + real whisper/crop test

Exit code: 0 = nothing blocking found, 1 = at least one FAIL.
"""
import argparse
import importlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
RESULTS = []


def record(status, stage, detail):
    RESULTS.append((status, stage, detail))
    icon = {PASS: "\u2705", FAIL: "\u274c", WARN: "\u26a0\ufe0f", SKIP: "\u23ed\ufe0f"}[status]
    print(f"  {icon} {status:<4} {stage:<26} {detail}", flush=True)


def header(text):
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}", flush=True)


# ---------------------------------------------------------------- stage 0
def check_runtime():
    header("STAGE 0 — runtime & binaries")
    v = sys.version_info
    record(PASS if v >= (3, 9) else FAIL, "python",
           f"{v.major}.{v.minor}.{v.micro} (need >=3.9)")

    ff = shutil.which("ffmpeg")
    if ff:
        try:
            out = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=20)
            record(PASS, "ffmpeg", out.stdout.split("\n")[0][:70])
        except Exception as e:                                    # noqa: BLE001
            record(FAIL, "ffmpeg", f"found at {ff} but not executable: {e}")
    else:
        record(FAIL, "ffmpeg",
               "NOT on PATH — the 9:16 reframe and audio mux cannot run. "
               "Install it (apt-get install -y ffmpeg / brew install ffmpeg).")

    # yt-dlp shells out to ffmpeg for merging; without it a download can
    # "succeed" and still leave no playable file.
    record(PASS if shutil.which("ffprobe") else WARN, "ffprobe",
           shutil.which("ffprobe") or "absent — not fatal, ffmpeg is what matters")


# ---------------------------------------------------------------- stage 0b
def check_dependencies():
    header("STAGE 0b — python dependencies")
    required = {
        "yt_dlp": "download (stage 1)",
        "dotenv": "config loading",
        "groq": "Whisper + LLM tier 1",
        "cv2": "face-tracked 9:16 crop (stage 4)",
        "faster_whisper": "local Whisper fallback",
        "llama_cpp": "tier-3 local LLM",
    }
    for mod, why in required.items():
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            record(PASS, mod, f"importable (v{ver}) — {why}")
        except ImportError:
            # Only yt_dlp/dotenv/cv2 are hard blockers for producing a clip.
            hard = mod in ("yt_dlp", "dotenv", "cv2")
            record(FAIL if hard else WARN, mod,
                   f"NOT importable — {why}"
                   + ("" if hard else " (optional tier; pipeline can work without it)"))

    # OpenCV 5 removed CascadeClassifier entirely — the exact B3 regression.
    try:
        import cv2
        if not hasattr(cv2, "CascadeClassifier"):
            record(FAIL, "cv2.CascadeClassifier",
                   f"OpenCV {cv2.__version__} has no CascadeClassifier — face tracking "
                   "is dead. Pin opencv-python-headless<5.")
        else:
            import cv2.data as d
            n = len([f for f in os.listdir(d.haarcascades) if f.endswith(".xml")])
            record(PASS if n else WARN, "haarcascades",
                   f"{n} cascade file(s) present" if n else "directory empty — centre-crop fallback")
    except ImportError:
        pass


# ---------------------------------------------------------------- stage 0c
def check_credentials():
    header("STAGE 0c — credentials (decide which LLM/Whisper tier runs)")
    groq = os.environ.get("GROQ_API_KEY", "").strip()
    record(PASS if groq else WARN, "GROQ_API_KEY",
           f"set ({len(groq)} chars)" if groq
           else "EMPTY — Whisper + LLM fall through to the local CPU tier "
                "(~25-40 min per 2h podcast, ~3-8 min per LLM call). Clips still "
                "get made, just slowly.")
    record(PASS if os.environ.get("CEREBRAS_API_KEY", "").strip() else SKIP,
           "CEREBRAS_API_KEY",
           "set" if os.environ.get("CEREBRAS_API_KEY", "").strip()
           else "unset — optional LLM fallback (no-card tier ended July 2026)")

    ck = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    b64 = os.environ.get("YT_COOKIES_B64", "").strip()
    if ck:
        record(PASS, "YTDLP_COOKIES_FILE", f"set -> {ck}")
    elif b64:
        record(WARN, "YT_COOKIES_B64",
               "set but YTDLP_COOKIES_FILE is not — the workflow decodes it; "
               "locally you must decode it yourself first")
    else:
        record(WARN, "cookies",
               "no cookie configured. Not fatal on a residential IP, but on a "
               "datacenter IP (GitHub Actions) this is the usual bot-check cause. "
               "Set YT_COOKIES_B64 with a Netscape cookies.txt.")


def check_cookies():
    """Validate the cookie file the same way the downloader does, plus the two
    things the downloader deliberately does NOT check: how many rows are
    YouTube's, and whether any of them have already expired.

    An expired session is the classic silent killer — the file is perfectly
    well-formed, yt-dlp accepts it, and YouTube still bot-checks the request.
    """
    header("STAGE 0d — YouTube cookies (the bot-check fix)")
    import base64
    import time

    path = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    tmp = None

    if not path:
        b64 = os.environ.get("YT_COOKIES_B64", "").strip()
        if not b64:
            record(SKIP, "cookies",
                   "neither YTDLP_COOKIES_FILE nor YT_COOKIES_B64 is set. Pass "
                   "--cookies /path/to/cookies.txt to check one.")
            return
        try:
            raw = base64.b64decode(b64, validate=False)
            tmp = tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False)
            tmp.write(raw)
            tmp.close()
            path = tmp.name
            record(PASS, "YT_COOKIES_B64", f"decodes cleanly ({len(raw)} bytes)")
        except Exception as e:                                    # noqa: BLE001
            record(FAIL, "YT_COOKIES_B64",
                   f"not valid base64: {e}. Encode with: base64 -w0 cookies.txt")
            return

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        record(FAIL, "cookie file", f"cannot read: {e}")
        return
    finally:
        pass

    # Same preflight the downloader runs, so a PASS here means a PASS there.
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from shorts_generator.local.downloader import _validate_cookie_file
        _validate_cookie_file(path)
        record(PASS, "netscape format", "accepted by the downloader's preflight")
    except Exception as e:                                        # noqa: BLE001
        record(FAIL, "netscape format", str(e))
        if tmp:
            os.unlink(tmp.name)
        return

    if data.startswith(b"\xef\xbb\xbf"):
        record(WARN, "BOM", "file starts with a UTF-8 BOM — yt-dlp may reject it; "
                            "re-save as UTF-8 without BOM")

    rows, yt_rows, expired = 0, 0, 0
    now = time.time()
    for line in data.splitlines():
        if not line.strip():
            continue
        if line.startswith(b"#") and not line.startswith(b"#HttpOnly_"):
            continue
        f = line.split(b"\t")
        if len(f) != 7:
            continue
        rows += 1
        domain = f[0].lstrip(b"#").replace(b"HttpOnly_", b"").decode("utf-8", "replace")
        if "youtube.com" in domain or "google.com" in domain:
            yt_rows += 1
        try:
            exp = int(f[4])
            if 0 < exp < now:
                expired += 1
        except (ValueError, IndexError):
            pass

    record(PASS if rows else FAIL, "cookie rows", f"{rows} row(s) parsed")
    record(PASS if yt_rows else FAIL, "youtube/google rows",
           f"{yt_rows} — these are the ones that matter for auth")
    if expired:
        record(FAIL, "expiry",
               f"{expired} of {rows} cookie(s) have ALREADY EXPIRED. The file is "
               "well-formed, so nothing errors — YouTube just keeps bot-checking. "
               "Re-export cookies.txt from a browser where you are still signed in.")
    else:
        record(PASS, "expiry", "no cookie in this file is past its expiry")

    record(WARN, "session validity",
           "format + expiry only. Whether YouTube still honours this session can "
           "only be proven by a real download — run with --url to test it.")
    if tmp:
        os.unlink(tmp.name)


# ---------------------------------------------------------------- stage 1
def check_network():
    header("STAGE 1 — network reachability (the #1 cause of zero clips)")
    # A bare TCP connect is NOT enough: sandboxes and some networks accept the
    # connection and then kill the TLS handshake, which still makes YouTube
    # completely unusable while a connect-only probe reports PASS. Do the real
    # handshake, then a real HTTPS GET, and report the deepest step reached.
    import ssl
    import urllib.request

    for host in ("www.youtube.com", "youtu.be"):
        try:
            with socket.create_connection((host, 443), timeout=12):
                pass
        except Exception as e:                                    # noqa: BLE001
            record(FAIL, host, f"TCP connect failed: {e}. Nothing downstream can run.")
            continue

        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=12) as s:
                with ctx.wrap_socket(s, server_hostname=host):
                    pass
        except Exception as e:                                    # noqa: BLE001
            record(FAIL, host,
                   f"TCP connects but the TLS handshake fails ({type(e).__name__}) — "
                   "the host is effectively UNREACHABLE despite the open port. "
                   "A firewall/proxy is terminating the connection.")
            continue

        try:
            req = urllib.request.Request(f"https://{host}/",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                record(PASS, host, f"HTTPS reachable (HTTP {r.status})")
        except Exception as e:                                    # noqa: BLE001
            code = getattr(e, "code", None)
            if code:
                # 4xx still proves the TLS path works and YouTube answered.
                record(PASS, host, f"HTTPS reachable (HTTP {code})")
            else:
                record(FAIL, host, f"TLS ok but the HTTPS request failed: {e}")


def check_download(url, fmt="720"):
    header("STAGE 1b — LIVE download test (client rotation)")
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from shorts_generator.local.downloader import _player_clients, download_youtube_local
    except Exception as e:                                        # noqa: BLE001
        record(FAIL, "import downloader", str(e))
        return None

    clients = _player_clients()
    on_ci = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))
    record(WARN if on_ci else PASS, "player chain",
           f"{' -> '.join(clients)}"
           + ("  [CI chain — datacenter IP]" if on_ci else "  [residential chain]"))

    with tempfile.TemporaryDirectory() as td:
        try:
            path = download_youtube_local(url, fmt=fmt, out_dir=td)
            size = os.path.getsize(path)
            if size > 0:
                record(PASS, "download", f"{size / 1e6:.1f} MB -> {os.path.basename(path)}")
                record(PASS, "STAGES 2-5 unblocked",
                       "the source exists, so Whisper/highlights/crop/metadata can run")
                return path
            record(FAIL, "download", "returned a path but the file is empty")
        except Exception as e:                                    # noqa: BLE001
            msg = str(e)
            record(FAIL, "download", msg[:300])
            low = msg.lower()
            if "not a bot" in low or "sign in to confirm" in low or "refused" in low:
                record(FAIL, "DIAGNOSIS",
                       "YouTube bot-check. This is an IP/session problem, NOT a code "
                       "bug — every client was refused. Fix with cookies "
                       "(YT_COOKIES_B64) or run from a residential IP.")
            elif "unavailable" in low or "private" in low:
                record(FAIL, "DIAGNOSIS", "the video itself is private/removed/geo-blocked.")
        return None


# ---------------------------------------------------------------- stages 2-4
def check_pipeline_on_file(path):
    header("STAGES 2-4 — real run against a local file (no YouTube involved)")
    record(PASS, "input", path)
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from shorts_generator.pipeline import generate_shorts
    except Exception as e:                                        # noqa: BLE001
        record(FAIL, "import pipeline", str(e))
        return

    with tempfile.TemporaryDirectory() as td:
        try:
            result = generate_shorts(path, num_clips=1, output_dir=td)
        except Exception as e:                                    # noqa: BLE001
            record(FAIL, "pipeline", f"{type(e).__name__}: {e}"[:400])
            low = str(e).lower()
            if "no segments" in low:
                record(FAIL, "DIAGNOSIS",
                       "Whisper ran but found no speech. Check GROQ_API_KEY, or that "
                       "the file actually has an audio track.")
            elif "zero clips" in low:
                record(FAIL, "DIAGNOSIS",
                       "Transcription worked but the LLM returned no highlights — "
                       "LLM tier problem, not a download problem.")
            elif "failed to render" in low:
                record(FAIL, "DIAGNOSIS",
                       "Highlights worked but ffmpeg/OpenCV could not render. "
                       "Stage 4 is the breakage.")
            return

        shorts = result.get("shorts") or []
        failed = result.get("failed_clips") or []
        record(PASS if shorts else FAIL, "clips rendered",
               f"{len(shorts)} rendered, {len(failed)} failed")
        for s in shorts:
            cu = s.get("clip_url")
            ok = bool(cu) and os.path.exists(cu) and os.path.getsize(cu) > 0
            record(PASS if ok else FAIL, "clip file",
                   f"{os.path.basename(cu)} {os.path.getsize(cu) / 1e6:.1f} MB" if ok
                   else f"missing or empty: {cu}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="YouTube URL to test the live download against")
    ap.add_argument("--file", help="Local video file — runs the real pipeline, skipping YouTube")
    ap.add_argument("--cookies", help="Path to a Netscape cookies.txt to validate "
                                      "(otherwise YTDLP_COOKIES_FILE / YT_COOKIES_B64 are used)")
    ap.add_argument("--format", default="720", help="download resolution (default 720)")
    args = ap.parse_args()

    print("AI YouTube Shorts Generator — stage diagnosis")
    print("Nothing here uploads, deletes sources, or touches your ledgers.")

    if args.cookies:
        os.environ["YTDLP_COOKIES_FILE"] = args.cookies

    check_runtime()
    check_dependencies()
    check_credentials()
    check_cookies()
    check_network()

    source = args.file
    if args.url:
        source = check_download(args.url, args.format) or source

    if source and os.path.exists(source):
        check_pipeline_on_file(source)
    elif args.url or args.file:
        record(SKIP, "stages 2-4", "no usable source file, so nothing to render")
    else:
        record(SKIP, "stages 2-4",
               "pass --url <yt-url> or --file <video.mp4> to test the real pipeline")

    header("SUMMARY")
    fails = [r for r in RESULTS if r[0] == FAIL]
    warns = [r for r in RESULTS if r[0] == WARN]
    skips = [r for r in RESULTS if r[0] == SKIP]
    print(f"  {len(RESULTS) - len(fails) - len(warns) - len(skips)} pass, "
          f"{len(fails)} FAIL, {len(warns)} warn, {len(skips)} skipped")
    if fails:
        print("\n  The first thing to fix:")
        print(f"    \u274c {fails[0][1]} — {fails[0][2]}")
    else:
        print("\n  No blocking failure found in what this machine could test.")
    if skips:
        print(f"\n  {len(skips)} check(s) were SKIPPED, not passed — re-run with "
              "--url or --file to cover them.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
