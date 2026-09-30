"""Local YouTube download via yt-dlp.

Returns a local mp4 path so the rest of the local pipeline can read it
directly off disk.
"""
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional

from ..config import OUTPUT_DIR


# yt-dlp InnerTube "player_client" rotation.
#
# WHY: YouTube bot-checks anonymous traffic coming from datacenter IPs — which
# is every GitHub Actions runner — on its default web client, returning
# "Sign in to confirm you're not a bot" with zero formats. The TV / embedded /
# mobile clients use different InnerTube endpoints that are frequently still
# served unauthenticated from the same IP, so retrying the SAME url under a
# different client recovers the download without any cookie.
#
# "default" = let yt-dlp pick (no extractor_args), i.e. today's behaviour, so
# a healthy run costs exactly one extra dict lookup. Override the whole chain
# with YTDLP_PLAYER_CLIENTS="tv,web_safari" if YouTube shifts again.
DEFAULT_PLAYER_CLIENTS = ("default", "tv_simply", "android_vr", "tv", "web_safari", "mweb")

# Errors that mean "this client was refused" rather than "this video is gone".
# Deliberately narrow: a private/deleted/geo-blocked video must fail fast on
# the first client instead of burning five retries.
_CLIENT_BLOCKED_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "cookies-from-browser",
    "failed to extract any player response",
    "unable to extract player response",
    "content is not available on this app",
    "please sign in",
    # YouTube bot-check / client refusal variants:
    "the page needs to be reloaded",
    "reload the page",
)


def _normalize_err(msg: str) -> str:
    """Lowercase and flatten smart quotes — yt-dlp emits a U+2019 in
    "you’re not a bot", which a naive `"you're" in msg` check would miss."""
    return (msg or "").replace("\u2019", "'").replace("\u2018", "'").lower()


def _is_client_blocked(err: BaseException) -> bool:
    msg = _normalize_err(str(err))
    if any(marker in msg for marker in (*_PERMANENT_MARKERS, *_FORMAT_MARKERS, *_COOKIE_ERROR_MARKERS)):
        return False
    return any(marker in msg for marker in _CLIENT_BLOCKED_MARKERS)


# Check permanent causes FIRST: some yt-dlp errors contain generic words such
# as "sign in" even when the video is private rather than client-refused.
_PERMANENT_MARKERS = (
    "private video", "video is private", "video unavailable", "deleted video",
    "has been removed", "not available in your country", "not available in your region",
    "blocked in your country", "not available in this country",
    "not available in your location", "this video is not available",
    "this video is unavailable", "video has been removed",
    "copyright claim", "copyright grounds", "copyright owner",
)
_FORMAT_MARKERS = ("requested format is not available", "requested format unavailable")
_COOKIE_ERROR_MARKERS = (
    "invalid netscape format", "does not look like a netscape format",
    "could not load cookies", "failed to load cookies", "cookie file is invalid",
)


def _error_kind(err: BaseException) -> str:
    msg = _normalize_err(str(err))
    if any(marker in msg for marker in _COOKIE_ERROR_MARKERS):
        return "cookies"
    if any(marker in msg for marker in _PERMANENT_MARKERS):
        return "unavailable"
    if any(marker in msg for marker in _FORMAT_MARKERS):
        return "format"
    if _is_client_blocked(err):
        return "refused"
    return "other"


def _safe_error(kind: str) -> str:
    return {
        "cookies": "Cookie file rejected by yt-dlp. Re-export a Netscape cookies.txt and update YT_COOKIES_B64.",
        "unavailable": "Video is private, deleted, geo/copyright-blocked or otherwise unavailable; check access to the video.",
        "format": "Requested format unavailable for this video; try another resolution/format.",
        "refused": "YouTube refused this player client (bot check / reload request).",
        "other": "Download failed for an unclassified reason; inspect yt-dlp locally without sharing credentials.",
    }[kind]


def _validate_cookie_file(path: str) -> None:
    """Format-only preflight, never log paths/contents or assume a cookie name.

    yt-dlp/YouTube, not this check, decide whether the session is usable.
    """
    valid = True
    try:
        with open(path, "rb") as fh:
            header = fh.readline(4096).strip()
            if header not in (b"# Netscape HTTP Cookie File", b"# HTTP Cookie File"):
                raise ValueError("bad header")
            found = False
            for line in fh:
                if not line.strip() or (line.startswith(b"#") and not line.startswith(b"#HttpOnly_")):
                    continue
                if len(line.rstrip(b"\r\n").split(b"\t")) == 7:
                    found = True
                    break
            if not found:
                raise ValueError("no Netscape cookie rows")
    except (OSError, ValueError):
        valid = False
    if not valid:
        # Outside the except: no raw path-containing exception in __context__.
        raise RuntimeError(
            "YTDLP_COOKIES_FILE is missing, empty or malformed. Export a non-empty "
            "Netscape cookies.txt and set YT_COOKIES_B64; cookie contents are not logged."
        )


def _player_clients() -> list:
    """The client rotation to try, honouring the YTDLP_PLAYER_CLIENTS override."""
    raw = os.environ.get("YTDLP_PLAYER_CLIENTS", "").strip()
    if raw:
        clients = [c.strip() for c in raw.split(",") if c.strip()]
        if clients:
            return clients
    return list(DEFAULT_PLAYER_CLIENTS)


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp is required for --mode local. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e
    return yt_dlp


def _format_for(fmt: str) -> str:
    """Map our '720' / '1080' shorthand to a yt-dlp format selector."""
    try:
        height = int(fmt)
    except ValueError:
        height = 720
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={height}][ext=mp4]/best"
    )


def _extract_youtube_video_id(source: str) -> Optional[str]:
    """Best-effort extraction of a YouTube video id from a URL."""
    parsed = urlparse(source)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return video_id or None

    if "youtube.com" in host:
        if parsed.path.startswith("/watch"):
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [""])[0]
            return video_id or None
        match = re.search(r"/(?:shorts|embed|live)/([^/?#&]+)", parsed.path)
        if match:
            return match.group(1)

    return None


def _resolve_local_path(source: str) -> Optional[str]:
    """Return a local filesystem path if the input already points at one."""
    parsed = urlparse(source)
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw_path = f"//{parsed.netloc}{raw_path}"
        candidate = Path(raw_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(f"Local file URL does not exist: {source}")

    if parsed.scheme in ("http", "https"):
        return None

    candidate = Path(source).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())

    if any(sep in source for sep in (os.sep, "/")) or source.startswith("~") or source.startswith("."):
        raise RuntimeError(f"Local file path does not exist: {source}")

    return None


def _existing_download(out_dir: str, video_id: str) -> Optional[str]:
    """Return a cached download path if we already have this YouTube id.

    Zero-byte files are ignored: a runner killed mid-download can leave the
    final name in place with no content, and returning it would send an empty
    file into ffmpeg and produce a baffling error three stages later.
    """
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = os.path.join(out_dir, f"source_{video_id}{ext}")
        try:
            if os.path.getsize(candidate) > 0:
                return candidate
        except OSError:
            continue
    return None


# yt-dlp's in-progress scratch files. A download that dies partway through —
# bot-check mid-stream, runner timeout, SIGKILL — leaves these behind, and for
# a 720p source they are HUNDREDS OF MEGABYTES each. Nothing ever cleaned them
# up, so a repeatedly-failing URL silently filled the runner's 14 GB disk one
# cron tick at a time until an unrelated step died with ENOSPC.
_PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".download")


def purge_partial_downloads(out_dir: str, video_id: Optional[str] = None) -> int:
    """Delete yt-dlp scratch files. Returns the number of bytes reclaimed."""
    if not os.path.isdir(out_dir):
        return 0
    stem = f"source_{video_id}" if video_id else "source_"
    reclaimed = 0
    for name in os.listdir(out_dir):
        if not name.startswith(stem):
            continue
        # ".part" can be mid-name, e.g. "source_x.f137.mp4.part-Frag3"
        if not any(suffix in name for suffix in _PARTIAL_SUFFIXES):
            continue
        path = os.path.join(out_dir, name)
        try:
            size = os.path.getsize(path)
            os.remove(path)
            reclaimed += size
        except OSError:
            continue
    if reclaimed:
        print(f"[download/local] cleaned {reclaimed / 1e6:.1f} MB of partial "
              f"download scratch files", flush=True)
    return reclaimed


def is_downloaded_source(path: str, out_dir: Optional[str] = None) -> bool:
    """True when ``path`` is a file THIS module downloaded (so it is safe to
    delete after processing), False when it is a user-supplied local input.

    Deleting a user's own input file would be catastrophic and unrecoverable,
    so the test is deliberately narrow: the file must live in the download
    directory AND carry the ``source_`` prefix this module writes.
    """
    if not path:
        return False
    base = os.path.basename(path)
    if not base.startswith("source_"):
        return False
    target_dir = os.path.abspath(out_dir or OUTPUT_DIR)
    return os.path.abspath(os.path.dirname(path)) == target_dir


def download_youtube_local(video_url: str, fmt: str = "720", out_dir: Optional[str] = None) -> str:
    """Download a remote URL or return a local file path unchanged."""
    local_path = _resolve_local_path(video_url)
    if local_path:
        print(f"[download/local] using local file: {local_path}", flush=True)
        return local_path

    cookies_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if cookies_file:
        _validate_cookie_file(cookies_file)

    yt_dlp = _import_ytdlp()
    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    video_id = _extract_youtube_video_id(video_url)
    if video_id:
        cached = _existing_download(out_dir, video_id)
        if cached:
            print(f"[download/local] reusing cached download: {cached}", flush=True)
            return cached

    safe_id = video_id if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else "(id unavailable)"
    print(f"[download/local] downloading YouTube video {safe_id}" if video_id
          else "[download/local] downloading remote video", flush=True)
    base_opts = {
        "format": _format_for(fmt),
        "outtmpl": os.path.join(out_dir, "source_%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Transient 5xx/throttling on a free runner shouldn't kill the campaign.
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
    }

    # Preflight checks file structure only; never print the path or cookie data.
    have_cookies = bool(cookies_file)
    if have_cookies:
        base_opts["cookiefile"] = cookies_file
        print("[download/local] cookie file format validated (session not verified)", flush=True)

    # Only YouTube has the InnerTube client concept — everything else gets a
    # single straightforward attempt.
    clients = _player_clients() if video_id else ["default"]

    last_err: Optional[BaseException] = None
    terminal_error: Optional[str] = None
    for attempt, client in enumerate(clients, 1):
        ydl_opts = dict(base_opts)
        if client != "default":
            ydl_opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                path = ydl.prepare_filename(info)
                # merge_output_format may rename the extension after merge
                if not os.path.exists(path):
                    stem, _ = os.path.splitext(path)
                    for ext in (".mp4", ".mkv", ".webm"):
                        if os.path.exists(stem + ext):
                            path = stem + ext
                            break
        except Exception as e:  # noqa: BLE001 - classified below
            last_err = e
            kind = _error_kind(e)
            # DISK: a failed attempt can leave a multi-hundred-MB .part file.
            # Purge before the NEXT client retries, otherwise a 5-client
            # rotation over a big video can strand 5 partial copies at once.
            purge_partial_downloads(out_dir, video_id)
            blocked = kind == "refused"
            more = attempt < len(clients)
            print(f"[download/local] player_client={client} failed: {_safe_error(kind)}", flush=True)
            if blocked and more:
                print(f"[download/local] retrying with player_client={clients[attempt]} "
                      f"({attempt}/{len(clients)} exhausted)", flush=True)
                continue
            if not blocked:
                # Raise outside the except so __context__ cannot contain secrets.
                terminal_error = _safe_error(kind)
                break
            break
        else:
            # Guard against a "successful" extract that produced nothing on
            # disk: yt-dlp can report success for a format it then failed to
            # merge, and an empty source poisons every downstream stage.
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                purge_partial_downloads(out_dir, video_id)
                last_err = RuntimeError("yt-dlp reported success but produced no usable file")
                if attempt < len(clients):
                    print(f"[download/local] player_client={client} produced an "
                          f"empty file — trying {clients[attempt]}", flush=True)
                    continue
                break
            if client != "default":
                print(f"[download/local] recovered via player_client={client} "
                      f"(set YTDLP_PLAYER_CLIENTS={client} to try it first)", flush=True)
            print(f"[download/local] ready: {path}", flush=True)
            return path

    if terminal_error:
        raise RuntimeError(terminal_error)

    raise RuntimeError(
        f"YouTube refused every InnerTube client ({', '.join(clients)}). "
        "Likely runner IP block or stale/invalid cookie session; check access "
        "to this video locally. "
        + ("Cookies file was configured (format valid, session not verified). "
           if have_cookies else "Cookies NOT configured; set YT_COOKIES_B64 with a Netscape cookies.txt. ")
        + ("Last attempt produced no usable file." if last_err and not _is_client_blocked(last_err)
           else "All clients returned a YouTube refusal.")
    ) from None
