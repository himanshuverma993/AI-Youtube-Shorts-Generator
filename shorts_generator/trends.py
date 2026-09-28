"""Optional trend context — Phase-1 Fix 2 (zero-key sources, fail-soft).

Injects a compact "what is working RIGHT NOW" block into the highlight and
metadata prompts so clip framing/titles reflect current viewer behaviour
instead of a frozen prompt from months ago.

DESIGN GUARANTEES (read before extending):
  * FRAMING BIAS ONLY — trends may influence how a moment is framed and how a
    title is styled, but the block's own header forbids inventing topics not
    present in the transcript. The transcript stays the sole source of truth.
  * NEVER A HARD DEPENDENCY — disabled by default until CAMPAIGN_NICHE is set,
    every fetch failure degrades to "no block injected", and a corrupt cache
    is simply rebuilt. Clipping must never wait on the trend layer.
  * $0 FOREVER — sources are yt-dlp YouTube search (keyless) and the official
    Google Trends daily RSS (keyless, stdlib urllib+xml).

Cache: output/trend_context.json with TREND_CACHE_HOURS TTL (the campaign
workflow persists this path between runs via actions/cache).
"""
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .config import CAMPAIGN_NICHE, OUTPUT_DIR, TREND_CACHE_HOURS, TREND_CONTEXT_ENABLED, TREND_MAX_ITEMS, TRENDS_GEO

TREND_CACHE_FILENAME = "trend_context.json"
_HTTP_TIMEOUT = 20
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def trend_context_enabled() -> bool:
    """On only if the operator opted in AND named a niche (generic trends
    without a niche are noise, so we skip them entirely by design)."""
    return TREND_CONTEXT_ENABLED and bool(CAMPAIGN_NICHE)


# ---------------------------------------------------------------------------
# Sources (each returns [] on ANY failure — isolation by contract)
# ---------------------------------------------------------------------------
def fetch_youtube_shorts_titles(niche: str, max_items: int) -> List[Dict]:
    """Keyless YouTube search via yt-dlp: recent niche searches, flat/fast."""
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return []
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,      # metadata only — no downloads
            "noplaylist": True,
        }) as ydl:
            info = ydl.extract_info(f"ytsearch{max_items}:{niche} shorts", download=False) or {}
        items = []
        for e in (info.get("entries") or [])[:max_items]:
            title = (e or {}).get("title") or ""
            if title:
                items.append({
                    "title": title.strip(),
                    "views": e.get("view_count"),
                    "duration": e.get("duration"),
                })
        return items
    except Exception as e:
        print(f"[trends] YouTube trend fetch failed ({e}) — skipping source", flush=True)
        return []


def fetch_google_trends(geo: str, max_items: int) -> List[str]:
    """Official Google Trends daily RSS (no key, stdlib only)."""
    url = f"https://trends.google.com/trending/rss?geo={geo}&hours=24"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            root = ET.fromstring(resp.read())
        titles = []
        for item in root.iter("item"):
            t = item.findtext("title")
            if t and t.strip():
                titles.append(t.strip())
        return titles[:max_items]
    except Exception as e:
        print(f"[trends] Google Trends RSS failed ({e}) — skipping source", flush=True)
        return []


# ---------------------------------------------------------------------------
# Cache + orchestration
# ---------------------------------------------------------------------------
def _cache_path() -> str:
    return os.path.join(OUTPUT_DIR, TREND_CACHE_FILENAME)


def load_trend_context(force: bool = False) -> Dict:
    """Fresh trend context, served from the TTL cache when possible."""
    path = _cache_path()
    if not force:
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            age = time.time() - float(cached.get("generated_epoch", 0))
            if 0 <= age < TREND_CACHE_HOURS * 3600:
                print(f"[trends] cache hit ({age / 3600:.1f}h old, TTL {TREND_CACHE_HOURS}h)", flush=True)
                return cached
            print(f"[trends] cache stale ({age / 3600:.1f}h) — refreshing", flush=True)
        except (OSError, ValueError, TypeError):
            pass  # no cache / corrupt cache → rebuild

    # Orchestration-layer guard (defense in depth): the contract says each
    # fetcher isolates its own failures AND returns [] — enforce it here too,
    # so a future fetcher that forgets its guard can never break generation.
    try:
        yt_items = fetch_youtube_shorts_titles(CAMPAIGN_NICHE, TREND_MAX_ITEMS)
    except Exception as e:
        print(f"[trends] youtube source crashed ({e}) — skipping", flush=True)
        yt_items = []
    try:
        gt_titles = fetch_google_trends(TRENDS_GEO, TREND_MAX_ITEMS)
    except Exception as e:
        print(f"[trends] google-trends source crashed ({e}) — skipping", flush=True)
        gt_titles = []
    ctx = {
        "generated_epoch": time.time(),
        "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "niche": CAMPAIGN_NICHE,
        "geo": TRENDS_GEO,
        "youtube": yt_items,
        "google_trends": gt_titles,
        "source_ok": {"youtube": bool(yt_items), "google_trends": bool(gt_titles)},
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ctx, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"[trends] cache write failed ({e}) — continuing uncached", flush=True)
    return ctx


def build_trend_block(ctx: Dict, for_stage: str = "highlights") -> str:
    """The injectable prompt block. "" when there is nothing useful to say."""
    if not ctx:
        return ""
    lines = [
        "TREND CONTEXT (framing bias ONLY — never invent content that is not in the transcript; the transcript remains the sole source of truth):"
    ]
    yt = ctx.get("youtube") or []
    if yt:
        def _views_txt(item):
            try:
                v = int(item.get("views") or 0)
            except (TypeError, ValueError):
                v = 0  # perfection sweep: yt-dlp can return non-int views
            return f" ({v:,} views)" if v else ""
        sample = "; ".join(f"\"{i['title']}\"{_views_txt(i)}" for i in yt[:6])
        lines.append(f"- What viewers currently watch in this niche ({ctx.get('niche', '')} shorts): {sample}")
    gt = ctx.get("google_trends") or []
    if gt:
        lines.append(f"- Broad trending topics right now ({ctx.get('geo', 'IN')}): {', '.join(gt[:10])} — "
                     "use only where they NATURALLY intersect what is actually said.")
    if len(lines) == 1:
        return ""  # both sources empty → inject nothing
    if for_stage == "metadata":
        lines.append("- Title/caption STYLE may mirror the currently-performing formats above; wording must "
                     "still describe THIS clip honestly.")
    else:
        lines.append("- Prefer moments that can be framed toward a current trend WITHOUT distorting the "
                     "actual speech; trend-fit is a tiebreaker, never a content override.")
    return "\n".join(lines)


def get_trend_block(for_stage: str = "highlights", force: bool = False) -> str:
    """One-call facade for pipeline: enabled? fetch (cached) → block. Any
    failure → "" (trend layer can never break generation)."""
    if not trend_context_enabled():
        return ""
    try:
        return build_trend_block(load_trend_context(force=force), for_stage=for_stage)
    except Exception as e:
        print(f"[trends] trend layer failed ({e}) — proceeding WITHOUT trend context", flush=True)
        return ""
