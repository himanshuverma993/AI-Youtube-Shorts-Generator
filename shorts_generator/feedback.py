"""Channel feedback loop — Phase 2 (Fix 3A).

Actuates the approved plan: the pipeline no longer guesses what works — it
MEASURES on your own posted videos and feeds the results back into the
highlight/metadata prompts.

PARTS
  * posting registry   ``campaign/posting_registry.json`` — one row per posted
                       clip (registered via ``scripts/register_post.py`` now,
                       auto-registered by the YouTube uploader in Phase 3).
  * stats refresh      YouTube Analytics API (channel-owner OAuth, FREE quota)
                       pulled once per campaign run into
                       ``campaign/feedback_stats.json``.
  * scoring            deterministic & transparent — 60% retention weight
                       (averageViewPercentage) + 40% view velocity
                       (log views per day, normalized across observed posts).
  * prompt injection   "CHANNEL FEEDBACK" block listing measured top/bottom
                       performers, injected at the same guidance slot as the
                       trend block. Only fires with >= FEEDBACK_MIN_POSTS
                       measured posts; any failure in the loop degrades to ""
                       so clipping is never blocked. Zero paid dependencies —
                       the runner path is stdlib urllib only.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .config import (
    FEEDBACK_ENABLED,
    FEEDBACK_MIN_AGE_HOURS,
    FEEDBACK_MIN_POSTS,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    IG_FEEDBACK_ENABLED,
    IG_USER_ID,
    YT_REFRESH_TOKEN,
)

# IG insights (Phase-4 3B): reach + engagement only — Instagram exposes NO
# retention metric, so IG posts are scored within their own cohort:
# 50% reach-velocity + 25% shares + 25% saves (all log-normalized).
_IG_GRAPH = "https://graph.facebook.com/v21.0"
_IG_METRICS = "reach,likes,comments,shares,saved"


def ig_feedback_configured() -> bool:
    """IG insights active only when opted in AND a user id + token exist."""
    from .uploader_instagram import _stored_token  # lazy: uploader imports us too
    from .config import IG_ACCESS_TOKEN
    return bool(FEEDBACK_ENABLED and IG_FEEDBACK_ENABLED and IG_USER_ID
                and (IG_ACCESS_TOKEN or _stored_token()))

REGISTRY_PATH = os.path.join("campaign", "posting_registry.json")
STATS_PATH = os.path.join("campaign", "feedback_stats.json")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
_METRICS = "views,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained"
_HTTP_TIMEOUT = 30
_YT_ID_RE = __import__("re").compile(r"^[a-zA-Z0-9_-]{11}$")


def _google_trio_present() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and YT_REFRESH_TOKEN)


def feedback_configured() -> bool:
    """True when ANY feedback source is usable (YouTube Analytics trio OR
    Instagram token+id). Missing creds are a SILENT skip — never an error."""
    return FEEDBACK_ENABLED and (_google_trio_present() or ig_feedback_configured())


# ---------------------------------------------------------------------------
# Registry (persisted via workflow cache, like the other campaign ledgers)
# ---------------------------------------------------------------------------
def parse_youtube_video_id(url_or_id: str) -> str:
    """Accept watch/youtu.be/shorts URLs or a bare 11-char id."""
    s = (url_or_id or "").strip()
    if _YT_ID_RE.match(s):
        return s
    try:
        parsed = urllib.parse.urlparse(s)
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host and parsed.path.strip("/"):
            return parsed.path.strip("/").split("/")[0]
        if parsed.path == "/watch":
            vid = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
            if vid:
                return vid
        # /shorts/<id> or /live/<id> or /embed/<id>
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            return parts[1]
    except Exception:
        pass
    raise ValueError(f"Could not extract a YouTube video id from: {url_or_id!r}")


def load_registry(path: str = REGISTRY_PATH) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("posts"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"posts": []}


def save_registry(registry: Dict, path: str = REGISTRY_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=1)


def register_post(
    *,
    platform: str,
    video_id: str,
    title: str = "",
    caption: str = "",
    hook_line: str = "",
    hashtags: Optional[List[str]] = None,
    duration_seconds: Optional[float] = None,
    source_url: str = "",
    clip_start: Optional[float] = None,
    clip_end: Optional[float] = None,
    posted_at_epoch: Optional[float] = None,
    path: str = REGISTRY_PATH,
) -> Dict:
    """Upsert one posted clip into the registry (dedupes by platform+video_id).
    Returns the updated registry row."""
    registry = load_registry(path)
    posted_at = float(posted_at_epoch if posted_at_epoch is not None else time.time())
    row = {
        "platform": platform,
        "video_id": video_id,
        "title": title.strip(),
        "caption": caption.strip(),
        "hook_line": hook_line.strip(),
        "hashtags": list(hashtags or []),
        "duration_seconds": duration_seconds,
        "source_url": source_url,
        "clip_start": clip_start,
        "clip_end": clip_end,
        "posted_at_epoch": posted_at,
        "posted_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(posted_at)),
    }
    for i, existing in enumerate(registry["posts"]):
        if existing.get("platform") == platform and existing.get("video_id") == video_id:
            row = {**existing, **{k: v for k, v in row.items() if v not in ("", None, [])}}
            registry["posts"][i] = row
            break
    else:
        registry["posts"].append(row)
    save_registry(registry, path)
    return row


# ---------------------------------------------------------------------------
# Stats fetching (YouTube Analytics — channel-owner OAuth, free)
# ---------------------------------------------------------------------------
def _http_json(url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None,
               method: str = "GET", form: Optional[Dict] = None) -> Dict:
    """Stdlib JSON-over-HTTP helper. Split out so tests can patch it."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method=method, headers=headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, data=data, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _mint_access_token() -> str:
    """Exchange the long-lived refresh token for a short-lived access token."""
    data = _http_json(_TOKEN_URL, method="POST", form={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": YT_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    })
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"token exchange returned no access_token: {data}")
    return token


def _fetch_analytics_rows(access_token: str, video_ids: List[str]) -> Dict[str, Dict]:
    """One Analytics report call for up to ~200 videos; {video_id: metrics}."""
    if not video_ids:
        return {}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    data = _http_json(_ANALYTICS_URL, params={
        "ids": "channel==MINE",
        "dimensions": "video",
        "metrics": _METRICS,
        "startDate": "2005-02-01",
        "endDate": today,
        "filters": f"video=={','.join(video_ids)}",
        "maxResults": 200,
    }, headers={"Authorization": f"Bearer {access_token}"})
    headers = [h["name"] for h in data.get("columnHeaders", [])]
    rows: Dict[str, Dict] = {}
    for row in data.get("rows", []) or []:
        vid, values = row[0], row[1:]
        rows[vid] = {name: values[i] for i, name in enumerate(headers[1:]) if i < len(values)}
    return rows


# ---------------------------------------------------------------------------
# Scoring — deterministic, transparent, documented in the block header
# ---------------------------------------------------------------------------
def _score_post(row: Dict, max_velocity: float) -> float:
    """score = 60 × retention + 40 × normalized log-view-velocity."""
    retention = min(max(float(row.get("averageViewPercentage") or 0.0), 0.0), 100.0) / 100.0
    velocity = float(row.get("view_velocity") or 0.0)
    norm_v = velocity / max_velocity if max_velocity > 0 else 0.0
    return round(60 * retention + 40 * norm_v, 1)


def _score_ig_post(row: Dict, max_velocity: float, max_shares: float, max_saves: float) -> float:
    """IG cohort formula (no retention exists on IG — this is the documented
    adaptation): 50 × reach-velocity + 25 × shares + 25 × saves, all 0..1."""
    def _log(v):
        return math.log1p(float(v or 0.0))
    vel = float(row.get("view_velocity") or 0.0)
    norm_v = vel / max_velocity if max_velocity > 0 else 0.0
    norm_sh = _log(row.get("shares")) / _log(max_shares) if max_shares else 0.0
    norm_sv = _log(row.get("saved")) / _log(max_saves) if max_saves else 0.0
    return round(50 * norm_v + 25 * norm_sh + 25 * norm_sv, 1)


def _fetch_ig_insight_rows(token: str, media_ids: List[str]) -> Dict[str, Dict]:
    """Per-media IG insights; {media_id: {metric: value}}. Per-media isolation:
    one dead media id must not forfeit the batch."""
    rows: Dict[str, Dict] = {}
    for mid in media_ids:
        try:
            data = _http_json(
                f"{_IG_GRAPH}/{mid}/insights",
                params={"metric": _IG_METRICS},
                headers={"Authorization": f"Bearer {token}"},
            )
            values = {}
            for item in data.get("data", []) or []:
                vals = item.get("values") or []
                if vals:
                    values[item.get("name")] = vals[0].get("value", 0)
            if values:
                rows[mid] = values
        except Exception as e:
            print(f"[feedback] IG insights for {mid} failed ({e}) — skipping media", flush=True)
    return rows


def _with_view_velocity(row: Dict) -> Dict:
    views = float(row.get("views") or 0.0)
    age_days = max((time.time() - float(row.get("posted_at_epoch") or time.time())) / 86400.0, 0.5)
    row["view_velocity"] = round(math.log1p(views) / age_days, 3)
    return row


# ---------------------------------------------------------------------------
# Orchestrator — refresh + block builder + facade
# ---------------------------------------------------------------------------
def refresh_feedback_stats(registry: Optional[Dict] = None,
                           registry_path: str = REGISTRY_PATH,
                           stats_path: str = STATS_PATH) -> Dict:
    """Pull Analytics for eligible (youtube, old-enough) registry rows and
    merge into stats with scores. Returns the stats document."""
    registry = registry if registry is not None else load_registry(registry_path)
    now = time.time()
    min_age = FEEDBACK_MIN_AGE_HOURS * 3600
    eligible = [p for p in registry.get("posts", [])
                if p.get("platform") == "youtube"
                and p.get("video_id")
                and now - float(p.get("posted_at_epoch") or 0) >= min_age]
    ids = [p["video_id"] for p in eligible][:200]

    # YouTube branch runs only when its own creds exist — an IG-only setup
    # (or an expired Google token) must not forfeit the other platform.
    analytics = {}
    if ids:
        if _google_trio_present():
            try:
                token = _mint_access_token()
                analytics = _fetch_analytics_rows(token, ids)
            except Exception as e:
                print(f"[feedback] YT stats failed ({e}) — YT skipped, other sources continue", flush=True)
        else:
            print("[feedback] youtube posts registered but Google OAuth trio absent — skipping YT stats", flush=True)
    by_id = {p["video_id"]: p for p in eligible}
    measured: List[Dict] = []
    for vid, metrics in analytics.items():
        reg = by_id.get(vid, {})
        row = {**reg, **{k: v for k, v in metrics.items()}}
        measured.append(_with_view_velocity(row))

    max_velocity = max((m["view_velocity"] for m in measured), default=0.0)
    for m in measured:
        m["score"] = _score_post(m, max_velocity)

    # ── Phase-4 3B: Instagram cohort (if configured) ─────────────────────
    ig_measured: List[Dict] = []
    if ig_feedback_configured():
        ig_eligible = [p for p in registry.get("posts", [])
                       if p.get("platform") == "instagram"
                       and p.get("video_id")
                       and now - float(p.get("posted_at_epoch") or 0) >= min_age]
        if ig_eligible:
            from .uploader_instagram import ig_access_token  # lazy (circular-safe)
            ig_token = ig_access_token()
            ig_stats = _fetch_ig_insight_rows(ig_token, [p["video_id"] for p in ig_eligible][:100])
            ig_by_id = {p["video_id"]: p for p in ig_eligible}
            for mid, metrics in ig_stats.items():
                reg = ig_by_id.get(mid, {})
                row = {
                    **reg,
                    **metrics,
                    "views": float(metrics.get("reach") or 0.0),  # reach == IG's distribution count
                }
                ig_measured.append(_with_view_velocity(row))
            ig_max_v = max((m["view_velocity"] for m in ig_measured), default=0.0)
            ig_max_sh = max((float(m.get("shares") or 0) for m in ig_measured), default=0.0)
            ig_max_sv = max((float(m.get("saved") or 0) for m in ig_measured), default=0.0)
            for m in ig_measured:
                m["score"] = _score_ig_post(m, ig_max_v, ig_max_sh, ig_max_sv)

    all_posts = measured + ig_measured
    stats = {
        "generated_epoch": now,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        "registered_posts": len(registry.get("posts", [])),
        "eligible_posts": len(eligible),
        "measured_posts": len(all_posts),
        "scoring": {
            "youtube": "60% avgViewPercentage + 40% log-view-velocity (cohort-normalized)",
            "instagram": "50% reach-velocity + 25% shares + 25% saves (IG exposes no retention%)",
        },
        "posts": sorted(all_posts, key=lambda r: r["score"], reverse=True),
    }
    os.makedirs(os.path.dirname(stats_path) or ".", exist_ok=True)
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    return stats


def load_feedback_stats(stats_path: str = STATS_PATH) -> Dict:
    try:
        with open(stats_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("posts"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"measured_posts": 0, "posts": []}


def _fmt_post_line(row: Dict, rank: int) -> str:
    title = (row.get("title") or row.get("video_id") or "?").strip()
    hook = (row.get("hook_line") or "").strip()
    views = row.get("views") or 0
    # Platform-truthful metrics (audit F9): IG has NO retention metric —
    # call reach "reach", not "retention 0%", so the model isn't misled.
    if row.get("platform") == "instagram":
        shares = int(float(row.get("shares") or 0))
        saves = int(float(row.get("saved") or 0))
        line = f"{rank}. [IG] \"{title}\" — reach {int(views):,}, shares {shares:,}, saves {saves:,}, score {row.get('score', 0)}"
    else:
        retention = row.get("averageViewPercentage") or 0
        line = (f"{rank}. [YT] \"{title}\" — retention {retention:.0f}%, views "
                f"{int(views):,}, score {row.get('score', 0)}")
    if hook:
        line += f" — opening hook: \"{hook}\""
    return line


def build_feedback_block(stats: Dict, for_stage: str = "highlights") -> str:
    """The injectable block. "" until FEEDBACK_MIN_POSTS posts have stats —
    noisy feedback from 1–2 uploads would hurt more than help."""
    posts = stats.get("posts") or []
    if len(posts) < FEEDBACK_MIN_POSTS:
        return ""
    top = posts[:3]
    bottom = posts[-3:][::-1]
    scoring = stats.get("scoring") or {}
    scoring_note = "; ".join(f"{k}: {v}" for k, v in scoring.items()) or "60% retention + 40% view velocity"
    lines = [
        "CHANNEL FEEDBACK (measured on THIS channel's real posted videos — actual "
        "platform analytics data; weight this above generic advice):",
        f"TOP PERFORMERS (score formula — {scoring_note}):",
    ]
    lines += [_fmt_post_line(p, i + 1) for i, p in enumerate(top)]
    lines.append("UNDERPERFORMERS (avoid repeating these patterns):")
    lines += [_fmt_post_line(p, i + 1) for i, p in enumerate(bottom)]
    if for_stage == "metadata":
        lines.append("Guidance: model this run's titles/hashtags/caption styles closer to the top "
                     "performers; avoid the underperformers' wording patterns.")
    else:
        lines.append("Guidance: prefer moments whose hook/framing resembles the top performers; "
                     "avoid topic/angle patterns of the underperformers.")
    return "\n".join(lines)


def get_feedback_block(for_stage: str = "highlights",
                       registry_path: str = REGISTRY_PATH,
                       stats_path: str = STATS_PATH) -> str:
    """Pipeline facade: configured? → refresh stats (soft) → build block.
    Any failure anywhere → "" (the loop can never block generation)."""
    if not feedback_configured():
        return ""
    try:
        stats = refresh_feedback_stats(registry_path=registry_path, stats_path=stats_path)
    except Exception as e:
        print(f"[feedback] stats refresh failed ({e}) — proceeding WITHOUT feedback context", flush=True)
        stats = load_feedback_stats(stats_path)
    block = build_feedback_block(stats, for_stage=for_stage)
    if block:
        print(f"[feedback] injecting feedback block ({stats.get('measured_posts', 0)} measured posts)",
              flush=True)
    return block
