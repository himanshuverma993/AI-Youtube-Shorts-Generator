#!/usr/bin/env python3
"""Register a MANUALLY-POSTED clip into the feedback registry (Phase 2).

The feedback loop measures what worked on YOUR channel — but it needs to know
what you actually posted. Run this once per uploaded short:

    python scripts/register_post.py --url "https://youtu.be/dQw4w9WgXcQ" \
        --clip-path output/campaign_.../video_001/short_001.mp4

`--clip-path` auto-fills title/caption/hashtags from the sibling
`<clip>.youtube.json` / `<clip>.instagram.json` sidecars. Manual flags
(--title/--caption/--hook/--hashtags/...) override or supply missing fields.

Register at least 5 posts; once they're 48h old, feedback stats merge into
the next campaign runs automatically (needs the FEEDBACK OAuth secrets set;
run scripts/oauth_local_setup.py once to create them).
"""
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from shorts_generator.feedback import parse_youtube_video_id, register_post, load_registry
from shorts_generator.config import FEEDBACK_MIN_POSTS, FEEDBACK_MIN_AGE_HOURS


def _read_sidecar_fields(clip_path: str):
    """Best-effort pull of title/caption/hashtags from platform sidecars."""
    title, caption, hashtags = "", "", []
    stem = clip_path[: -len(".mp4")] if clip_path.lower().endswith(".mp4") else clip_path
    yt_path = f"{stem}.youtube.json"
    ig_path = f"{stem}.instagram.json"
    if os.path.exists(yt_path):
        data = json.load(open(yt_path, encoding="utf-8"))
        title = str(data.get("title") or "")
        hashtags = list(data.get("hashtags") or [])
    if os.path.exists(ig_path):
        data = json.load(open(ig_path, encoding="utf-8"))
        caption = str(data.get("caption") or "")
    return title, caption, hashtags


def _parse_posted_at(raw: str):
    if not raw:
        return None
    import datetime
    try:
        dt = datetime.datetime.strptime(raw.strip(), "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError:
        raise SystemExit(f"--posted-at must be YYYY-MM-DD (got {raw!r})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="YouTube link/id of the posted short")
    ap.add_argument("--clip-path", default="", help="path to the source clip (or sidecar) to auto-fill fields")
    ap.add_argument("--title", default="")
    ap.add_argument("--caption", default="")
    ap.add_argument("--hook", default="", help="the clip's opening hook sentence")
    ap.add_argument("--hashtags", default="", help="comma-separated")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--source-url", default="", help="original long video URL the clip came from")
    ap.add_argument("--posted-at", default="", help="YYYY-MM-DD (defaults to now)")
    ap.add_argument("--registry", default=os.path.join("campaign", "posting_registry.json"))
    args = ap.parse_args()

    video_id = parse_youtube_video_id(args.url)
    title, caption, hashtags = _read_sidecar_fields(args.clip_path) if args.clip_path else ("", "", [])

    row = register_post(
        platform="youtube",
        video_id=video_id,
        title=args.title or title,
        caption=args.caption or caption,
        hook_line=args.hook,
        hashtags=[t.strip() for t in args.hashtags.split(",") if t.strip()] or hashtags,
        duration_seconds=args.duration,
        source_url=args.source_url,
        posted_at_epoch=_parse_posted_at(args.posted_at),
        path=args.registry,
    )
    total = len(load_registry(args.registry).get("posts", []))
    print(f"✅ registered youtube:{video_id} — \"{row.get('title', '')[:60]}\"")
    print(f"registry now holds {total} post(s). Feedback activates with "
          f">={FEEDBACK_MIN_POSTS} posts older than {FEEDBACK_MIN_AGE_HOURS}h + OAuth secrets set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
