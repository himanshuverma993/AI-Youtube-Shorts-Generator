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
    "requested format is not available",
)


def _normalize_err(msg: str) -> str:
    """Lowercase and flatten smart quotes — yt-dlp emits a U+2019 in
    "you’re not a bot", which a naive `"you're" in msg` check would miss."""
    return (msg or "").replace("\u2019", "'").replace("\u2018", "'").lower()


def _is_client_blocked(err: BaseException) -> bool:
    msg = _normalize_err(str(err))
    return any(marker in msg for marker in _CLIENT_BLOCKED_MARKERS)


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
    """Return a cached download path if we already have this YouTube id."""
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = os.path.join(out_dir, f"source_{video_id}{ext}")
        if os.path.exists(candidate):
            return candidate
    return None


def download_youtube_local(video_url: str, fmt: str = "720", out_dir: Optional[str] = None) -> str:
    """Download a remote URL or return a local file path unchanged."""
    local_path = _resolve_local_path(video_url)
    if local_path:
        print(f"[download/local] using local file: {local_path}", flush=True)
        return local_path

    yt_dlp = _import_ytdlp()
    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    video_id = _extract_youtube_video_id(video_url)
    if video_id:
        cached = _existing_download(out_dir, video_id)
        if cached:
            print(f"[download/local] reusing cached download: {cached}", flush=True)
            return cached

    print(f"[download/local] {video_url} @ {fmt}p → {out_dir}/", flush=True)
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

    # Optional authenticated downloads — vital on datacenter IPs (GitHub
    # Actions runners) where YouTube frequently bot-checks anonymous traffic.
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    have_cookies = bool(cookies_file and os.path.exists(cookies_file))
    if have_cookies:
        base_opts["cookiefile"] = cookies_file
        print(f"[download/local] using cookie file: {cookies_file}", flush=True)

    # Only YouTube has the InnerTube client concept — everything else gets a
    # single straightforward attempt.
    clients = _player_clients() if video_id else ["default"]

    last_err: Optional[BaseException] = None
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
            blocked = _is_client_blocked(e)
            more = attempt < len(clients)
            print(
                f"[download/local] player_client={client} failed "
                f"({'bot-check/client refusal' if blocked else type(e).__name__}): "
                f"{str(e).splitlines()[0][:200]}",
                flush=True,
            )
            if blocked and more:
                print(f"[download/local] retrying with player_client={clients[attempt]} "
                      f"({attempt}/{len(clients)} exhausted)", flush=True)
                continue
            if not blocked:
                raise  # genuinely unavailable video — fail fast, don't burn retries
            break
        else:
            if client != "default":
                print(f"[download/local] recovered via player_client={client} "
                      f"(set YTDLP_PLAYER_CLIENTS={client} to try it first)", flush=True)
            print(f"[download/local] ready: {path}", flush=True)
            return path

    raise RuntimeError(
        f"YouTube refused every InnerTube client ({', '.join(clients)}) for {video_url}.\n"
        "  This is YouTube bot-checking the runner's datacenter IP, not a bug in the clip\n"
        "  pipeline. Fixes, cheapest first:\n"
        "    1. Wait for the next scheduled tick — the block is IP- and time-dependent.\n"
        "    2. Set the YT_COOKIES_B64 repo secret (base64 of a Netscape cookies.txt from\n"
        "       a logged-in YouTube session); the workflow already decodes it into\n"
        "       YTDLP_COOKIES_FILE and yt-dlp picks it up automatically.\n"
        "    3. Pin a different client chain via the YTDLP_PLAYER_CLIENTS variable.\n"
        f"  Cookies were {'PRESENT' if have_cookies else 'NOT configured'} for this attempt.\n"
        f"  Last error: {last_err}"
    ) from last_err
