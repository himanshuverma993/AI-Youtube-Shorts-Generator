"""Local CPU transcription via faster-whisper — the ZERO-API fallback.

When Groq's hosted Whisper is down or out of free-tier quota, transcription
keeps going on nothing but runner CPU. The return shape is identical to the
Groq path (``{duration, segments[start,end,text]}``), so the rest of the
pipeline can't tell which backend produced the transcript.

Model choice (env ``LOCAL_WHISPER_MODEL``):
  tiny   ~30 MB, fastest, weakest English-only-ish accuracy
  base   ~45 MB, okay for clear speech
  small  ~460 MB, DEFAULT — best quality/speed balance on a 4-core runner
  medium ~1.5 GB, noticeably better, ~2x slower than small
On the 4-core ubuntu-latest runner, ``small`` int8 processes roughly
7–8 minutes per hour of podcast audio.
"""
from functools import lru_cache
from typing import Dict, Optional

from ..config import LOCAL_WHISPER_COMPUTE, LOCAL_WHISPER_DEVICE, LOCAL_WHISPER_MODEL


def _resolve_compute_type() -> str:
    if LOCAL_WHISPER_COMPUTE:
        return LOCAL_WHISPER_COMPUTE
    return "float16" if LOCAL_WHISPER_DEVICE == "cuda" else "int8"


@lru_cache(maxsize=4)
def _model(model_name: str, device: str, compute_type: str):
    """Process-wide cached WhisperModel (first call downloads the weights)."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is required for the local Whisper fallback. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e
    print(
        f"[whisper/local] loading model={model_name} device={device} compute={compute_type} "
        "(first call may download weights)",
        flush=True,
    )
    return WhisperModel(model_name, device=device, compute_type=compute_type)


def transcribe_whisper_local(audio_path: str, language: Optional[str] = None) -> Dict:
    """Transcribe one audio file locally. ``language`` is ISO-639-1 or None
    for auto-detect — same contract as the Groq transcription call."""
    model = _model(LOCAL_WHISPER_MODEL, LOCAL_WHISPER_DEVICE, _resolve_compute_type())
    segments_iter, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        condition_on_previous_text=False,
    )

    segments = []
    for s in segments_iter:
        segments.append(
            {
                "start": float(s.start),
                "end": float(s.end),
                "text": (s.text or "").strip(),
            }
        )

    duration = float(getattr(info, "duration", 0.0)) or (segments[-1]["end"] if segments else 0.0)
    print(
        f"[whisper/local] {len(segments)} segments, {duration:.0f}s of audio "
        f"(model={LOCAL_WHISPER_MODEL}, device={LOCAL_WHISPER_DEVICE})",
        flush=True,
    )
    return {"duration": duration, "segments": segments}
