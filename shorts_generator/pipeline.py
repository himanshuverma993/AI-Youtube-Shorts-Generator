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
          "shorts": [...],               # ONLY successfully rendered clips,
                                         # each with clip_url (local path) +
                                         # FTC-compliant metadata
          "failed_clips": [...],         # highlights whose render failed,
                                         # each with its "error" string
        }

    Raises:
        RuntimeError: if the source has no speech, the highlight generator
            returns nothing, or EVERY clip fails to render.
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

    cropped = crop_highlights_local(source_path, top, aspect_ratio=aspect_ratio, out_dir=output_dir)

    # crop_highlights_local NEVER raises — it records per-clip failures as
    # {"clip_url": None, "error": ...} so one bad highlight can't sink the
    # batch. That means the caller MUST separate them out: leaving the
    # placeholders in "shorts" made a run where every single render failed
    # look like a success (campaign_runner counted len(shorts), marked the URL
    # processed for good, and shipped zero files).
    rendered = [c for c in cropped if c.get("clip_url")]
    failed_clips = [c for c in cropped if not c.get("clip_url")]

    if failed_clips:
        print(f"[pipeline] ⚠ {len(failed_clips)}/{len(cropped)} clip(s) failed to render:",
              flush=True)
        for c in failed_clips:
            print(f"[pipeline]    - {c.get('title', '(untitled)')}: {c.get('error')}", flush=True)

    if not rendered:
        first_error = failed_clips[0].get("error") if failed_clips else "unknown error"
        raise RuntimeError(
            f"All {len(cropped)} clip(s) failed to render — no shorts produced. "
            f"First error: {first_error}"
        )

    # Only rendered clips go to the LLM: metadata for a file that does not
    # exist is wasted quota and pollutes the upload queue.
    shorts = generate_metadata(rendered, transcript=transcript, context_block=context_metadata)

    write_metadata_sidecars(shorts)

    return {
        "source_video_url": source_path,
        "transcript": transcript,
        "highlights": all_highlights,
        "shorts": shorts,
        # Rendering losses stay visible to the artifact/summary layer instead
        # of masquerading as successful clips.
        "failed_clips": failed_clips,
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
