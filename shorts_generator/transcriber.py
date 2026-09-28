"""Transcription — Groq hosted Whisper (primary) + local faster-whisper (fallback).

Pipeline:  media file → ffmpeg extracts a 16 kHz mono mp3 → Whisper
(``verbose_json`` + segment timestamps) → ``{duration, segments[start,end,text]}``.

Backend contract: Groq first (free tier, fast, large-v3-quality). If Groq
hard-fails — outage, exhausted audio-seconds quota, bad key — the same audio
is transcribed locally by faster-whisper on plain CPU, so the pipeline keeps
working with ZERO working API. A circuit breaker skips Groq for
``WHISPER_CIRCUIT_BREAK_SECONDS`` after the first failure (then re-probes).

Groq caps free-tier uploads at ~25 MiB per request, so when an extracted
audio track exceeds ``GROQ_MAX_AUDIO_BYTES`` it is split into fixed-length
chunks and the per-chunk segments are stitched back with offset timestamps
(the chunk loop uses the same failover per chunk — first chunk failing opens
the circuit, remaining chunks go straight to local).

Results are cached as ``.srt`` files next to the output dir — re-running a
campaign over the same sources does not re-spend quota or CPU.
"""
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import (
    AUDIO_BITRATE,
    AUDIO_CHUNK_SECONDS,
    AUDIO_SAMPLE_RATE,
    GROQ_MAX_AUDIO_BYTES,
    LOCAL_WHISPER_MODEL,
    OUTPUT_DIR,
    WHISPER_CIRCUIT_BREAK_SECONDS,
    WHISPER_FALLBACK_ENABLED,
    WHISPER_LOCAL_PINNED,
)
from .groq_client import transcribe_audio_groq
from .local.whisper import transcribe_whisper_local


# ---------------------------------------------------------------------------
# .srt transcript cache (shared with reruns of the same source file)
# ---------------------------------------------------------------------------
def _transcript_cache_path(media_path: str) -> Path:
    cache_dir = Path(OUTPUT_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / (Path(media_path).stem + ".srt")


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + (millis / 1000.0)


def _write_srt_cache(media_path: str, transcript: Dict) -> Path:
    cache_path = _transcript_cache_path(media_path)
    lines = []
    for idx, segment in enumerate(transcript.get("segments", []), start=1):
        start = _format_srt_timestamp(float(segment["start"]))
        end = _format_srt_timestamp(float(segment["end"]))
        text = str(segment.get("text", "")).strip().replace("\r", "").replace("\n", " ")
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    cache_path.write_text("\n".join(lines), encoding="utf-8")
    return cache_path


def _load_srt_cache(cache_path: Path) -> Dict:
    content = cache_path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return {"duration": 0.0, "segments": []}

    segments = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip("﻿") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[0].split("-->", 1)]
        text = "\n".join(lines[1:]).strip()
        segments.append(
            {
                "start": _parse_srt_timestamp(start_raw),
                "end": _parse_srt_timestamp(end_raw),
                "text": text,
            }
        )

    duration = segments[-1]["end"] if segments else 0.0
    return {"duration": duration, "segments": segments}


# ---------------------------------------------------------------------------
# Audio extraction (ffmpeg must be installed — the deploy workflow does this)
# ---------------------------------------------------------------------------
def _extract_audio(media_path: str, out_path: str) -> str:
    """Downmix to a small mono mp3 — plenty for Whisper, tiny to upload."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", media_path,
        "-vn",
        "-ac", "1",
        "-ar", AUDIO_SAMPLE_RATE,
        "-codec:a", "libmp3lame",
        "-b:a", AUDIO_BITRATE,
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _split_audio(audio_path: str, chunk_dir: str) -> List[str]:
    """Split an audio file into AUDIO_CHUNK_SECONDS chunks (re-encoded mp3s)."""
    pattern = os.path.join(chunk_dir, "chunk_%05d.mp3")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", audio_path,
        "-f", "segment",
        "-segment_time", str(AUDIO_CHUNK_SECONDS),
        "-codec:a", "libmp3lame",
        "-ac", "1",
        "-ar", AUDIO_SAMPLE_RATE,
        "-b:a", AUDIO_BITRATE,
        pattern,
    ]
    subprocess.run(cmd, check=True)
    chunks = sorted(
        os.path.join(chunk_dir, name)
        for name in os.listdir(chunk_dir)
        if name.startswith("chunk_") and name.endswith(".mp3")
    )
    if not chunks:
        raise RuntimeError(f"ffmpeg segmenting produced no chunks for {audio_path}")
    return chunks


def _segments_from_verbose(payload: Dict, offset: float = 0.0) -> List[Dict]:
    segments = []
    for s in payload.get("segments") or []:
        segments.append(
            {
                "start": float(s.get("start", 0.0)) + offset,
                "end": float(s.get("end", 0.0)) + offset,
                "text": (s.get("text") or "").strip(),
            }
        )
    return segments


# ---------------------------------------------------------------------------
# Failover orchestrator: Groq Whisper (primary) → local faster-whisper.
# Same contract as the LLM side — a dead free tier never stops the run.
# Circuit breaker trips after a Groq failure so the rest of the run (and any
# remaining audio chunks) transcribes locally instead of paying retry-sleep
# per chunk; Groq is re-probed after the cooldown.
# ---------------------------------------------------------------------------
_whisper_down_until: float = 0.0


def _groq_whisper_healthy() -> bool:
    return time.time() >= _whisper_down_until


def _trip_whisper_breaker() -> None:
    global _whisper_down_until
    _whisper_down_until = time.time() + WHISPER_CIRCUIT_BREAK_SECONDS


def _transcribe_unified(audio_path: str, language: Optional[str], offset: float = 0.0):
    """Returns ``(duration, segments)`` — identical shape from BOTH backends.

    ``offset`` shifts timestamps for pre-split chunks (segment i starts at
    i × AUDIO_CHUNK_SECONDS in the original media)."""
    if WHISPER_LOCAL_PINNED:
        # 📌 Pinned local mode: no cloud whisper call is ever attempted, not
        # even a "try" — every audio second runs on this machine's CPU.
        print("[transcribe] 📌 WHISPER_PROVIDER=local — faster-whisper on CPU "
              "(cloud whisper bypassed by design)", flush=True)
        data = transcribe_whisper_local(audio_path, language=language)
        return _shift_local(data, offset)
    if WHISPER_FALLBACK_ENABLED and not _groq_whisper_healthy():
        print("[transcribe] Groq Whisper circuit open — using local faster-whisper", flush=True)
        data = transcribe_whisper_local(audio_path, language=language)
        return _shift_local(data, offset)

    try:
        payload = transcribe_audio_groq(audio_path, language=language)
    except Exception as e:
        if not WHISPER_FALLBACK_ENABLED:
            raise
        _trip_whisper_breaker()
        print(
            f"[transcribe] ⚠ Groq Whisper failed ({e}); FAILING OVER to local "
            f"faster-whisper (model={LOCAL_WHISPER_MODEL}, cpu) — Groq skipped "
            f"for {WHISPER_CIRCUIT_BREAK_SECONDS}s",
            flush=True,
        )
        data = transcribe_whisper_local(audio_path, language=language)
        return _shift_local(data, offset)

    segments = _segments_from_verbose(payload, offset=offset)
    duration = float(payload.get("duration") or 0.0)
    if duration:
        duration += offset
    else:
        duration = segments[-1]["end"] if segments else 0.0
    return duration, segments


def _shift_local(data: Dict, offset: float):
    """Apply chunk offset to a local-whisper result and return (duration, segments)."""
    segments = [
        {
            "start": float(s["start"]) + offset,
            "end": float(s["end"]) + offset,
            "text": s["text"],
        }
        for s in data.get("segments", [])
    ]
    duration = float(data.get("duration") or 0.0) + offset if data.get("duration") else (
        segments[-1]["end"] if segments else 0.0
    )
    return duration, segments


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def transcribe(media_path: str, language: Optional[str] = None) -> Dict:
    """Transcribe a local media file with Groq Whisper (with .srt caching)."""
    cache_path = _transcript_cache_path(media_path)
    if cache_path.exists() and cache_path.stat().st_mtime >= os.path.getmtime(media_path):
        try:
            cached = _load_srt_cache(cache_path)
        except (OSError, ValueError):
            # Audit F5: a .srt written by a killed process (truncated
            # timestamps) must not crash the run — delete and re-transcribe.
            cached = None
        if cached and cached["segments"] and cached["duration"] > 0.0:
            print(
                f"[transcribe] reusing cached transcript: {cache_path} "
                f"({len(cached['segments'])} segments)",
                flush=True,
            )
            return cached
        print(f"[transcribe] cache empty/invalid/corrupt, deleting: {cache_path}", flush=True)
        cache_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="shorts_audio_") as tmp:
        audio_path = os.path.join(tmp, "audio.mp3")
        _extract_audio(media_path, audio_path)
        size = os.path.getsize(audio_path)

        mode = "local faster-whisper 📌" if WHISPER_LOCAL_PINNED else "Groq → local fallback"
        if size <= GROQ_MAX_AUDIO_BYTES or WHISPER_LOCAL_PINNED:
            print(f"[transcribe] transcribing {size / 1e6:.1f} MB ({mode})", flush=True)
            duration, segments = _transcribe_unified(audio_path, language)
        else:
            print(
                f"[transcribe] {size / 1e6:.1f} MB exceeds Groq upload cap "
                f"({GROQ_MAX_AUDIO_BYTES / 1e6:.0f} MB) — chunking every {AUDIO_CHUNK_SECONDS}s",
                flush=True,
            )
            chunk_dir = os.path.join(tmp, "chunks")
            os.makedirs(chunk_dir, exist_ok=True)
            chunks = _split_audio(audio_path, chunk_dir)
            segments = []
            for i, chunk_path in enumerate(chunks):
                offset = i * AUDIO_CHUNK_SECONDS
                print(f"[transcribe] chunk {i + 1}/{len(chunks)} (offset {offset}s)", flush=True)
                _, chunk_segments = _transcribe_unified(chunk_path, language, offset=offset)
                segments.extend(chunk_segments)
            duration = segments[-1]["end"] if segments else 0.0

    print(f"[transcribe] {len(segments)} segments, {duration:.0f}s of audio", flush=True)
    transcript = {"duration": duration, "segments": segments}
    cache_path = _write_srt_cache(media_path, transcript)
    print(f"[transcribe] wrote cache: {cache_path}", flush=True)
    return transcript
