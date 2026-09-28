"""End-to-end orchestrator — $0-cost stack, no OpenAI/Gemini/MuAPI anywhere.

    yt-dlp (download)            — free
    → Whisper (transcribe)       — Groq tier-1, auto-failover to local faster-whisper
    → LLM (highlights)           — Groq → Cerebras → local llama.cpp (doomsday tier-3)
    → ffmpeg/OpenCV (reframe)    — free, local
    → LLM (metadata)             — same 3-tier path, "#ad #sponsored"
                                   enforced in code
"""
import json
import os
from typing import Dict, List, Optional

from .highlights import get_highlights
from .local.clipper import crop_highlights_local
from .local.downloader import download_youtube_local
from .metadata import generate_metadata
from .transcriber import transcribe
from .trends import get_trend_block
from .feedback import get_feedback_block


def _context_for_stage(stage: str) -> str:
    """Compose ALL optional guidance blocks (trends + channel feedback) into
    one context block for a stage. Each layer returns "" when disabled/down,
    so this safely degrades to no-op — generation never depends on them."""
    blocks = [b.strip() for b in (
        get_trend_block(for_stage=stage),
        get_feedback_block(for_stage=stage),
    ) if b and b.strip()]
    return "\n\n".join(blocks)


def generate_shorts(
    youtube_url: str,
    num_clips: int = 3,
    aspect_ratio: str = "9:16",
    download_format: str = "720",
    language: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Dict:
    """Run the full pipeline and return a structured result.

    Args:
        youtube_url: source URL (YouTube, file://, or local path).
        num_clips: how many shorts to render.
        aspect_ratio: e.g. "9:16", "1:1".
        download_format: source resolution ("360" / "480" / "720" / "1080").
        language: ISO-639-1 to force Whisper language detection.
        output_dir: where the rendered short_*.mp4 files land. Defaults to
            config.OUTPUT_DIR. Batch callers (campaign_runner) MUST pass a
            per-video directory so clips from earlier URLs aren't overwritten.

    Returns:
        {
          "source_video_url": str,       # local path of the downloaded source
          "transcript": {...},
          "highlights": [...],           # all candidates ranked
          "shorts": [...],               # top `num_clips` with clip_url (local
                                         # path) + FTC-compliant metadata
        }
    """
    # Optional guidance layers (Phase-1 trends + Phase-2 feedback): framing
    # bias only, "" when disabled or in trouble — generation never blocked.
    context_highlights = _context_for_stage("highlights")
    context_metadata = _context_for_stage("metadata")

    source_path = download_youtube_local(youtube_url, fmt=download_format)

    transcript = transcribe(source_path, language=language)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    highlights_result = get_highlights(
        transcript, num_clips=num_clips, context_block=context_highlights
    )
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    print(f"[pipeline] cropping {len(top)} of {len(all_highlights)} candidates", flush=True)

    shorts = crop_highlights_local(source_path, top, aspect_ratio=aspect_ratio, out_dir=output_dir)

    # Phase-1 Fix 1: platform-split metadata (YouTube + Instagram payloads per
    # clip), FTC disclosure appended in code on every path.
    shorts = generate_metadata(shorts, transcript=transcript, context_block=context_metadata)

    write_metadata_sidecars(shorts)

    return {
        "source_video_url": source_path,
        "transcript": transcript,
        "highlights": all_highlights,
        "shorts": shorts,
    }


def write_metadata_sidecars(shorts: List[Dict]) -> None:
    """Write ``<clip>.youtube.json`` / ``<clip>.instagram.json`` next to every
    rendered mp4, so the upload artifacts contain ready-to-paste per-platform
    metadata (post descriptions / captions / hashtags)."""
    written = 0
    for short in shorts:
        clip_path = short.get("clip_url")
        meta = short.get("metadata")
        if not clip_path or not isinstance(meta, dict):
            continue
        base = clip_path[: -len(".mp4")] if clip_path.lower().endswith(".mp4") else clip_path
        for platform in ("youtube", "instagram"):
            payload = meta.get(platform)
            if not isinstance(payload, dict):
                continue
            path = f"{base}.{platform}.json"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                written += 1
            except OSError as e:
                print(f"[pipeline] sidecar write failed for {path} ({e})", flush=True)
    if written:
        print(f"[pipeline] wrote {written} platform metadata files", flush=True)
