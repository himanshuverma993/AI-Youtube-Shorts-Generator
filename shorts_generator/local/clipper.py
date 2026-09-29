"""Local clipping: ffmpeg subclip + OpenCV face-aware vertical crop.

Two stages per highlight:
  1. Cut the source video to [start, end] with ffmpeg (re-encoded, audio kept).
  2. Reframe the cut to the target aspect ratio. For 9:16 we slide a vertical
     window horizontally across the frame to keep faces centred (Haar
     cascade — same approach as the original repo, no external models).
"""
import math
import os
import subprocess
from typing import Dict, List, Optional, Tuple

from ..config import FFMPEG_TIMEOUT_SECONDS, OUTPUT_DIR


def _run_ffmpeg(cmd: list, what: str, timeout: float = FFMPEG_TIMEOUT_SECONDS) -> None:
    """Run ffmpeg with a HARD timeout and a readable error.

    ``subprocess.run(cmd, check=True)`` with no timeout is a hang waiting to
    happen: ffmpeg blocks forever on a truncated moov atom or a stream it
    cannot demux, and the job then burns its entire 240-minute budget with no
    output at all. On timeout the child is killed (and SIGKILLed if it
    ignores SIGTERM) so no orphan ffmpeg keeps holding the file open.

    stderr is captured rather than inherited so the raised message actually
    contains ffmpeg's reason instead of it scrolling past in the job log.
    """
    try:
        proc = subprocess.run(
            cmd, check=False, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg timed out after {timeout:.0f}s during {what} — "
            f"input is probably corrupt or unseekable"
        ) from e
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        tail = detail.splitlines()[-1][:300] if detail else "no stderr output"
        raise RuntimeError(f"ffmpeg failed during {what} (exit {proc.returncode}): {tail}")

# A reframe writer fed a nonsense frame rate fails to open and then silently
# swallows every frame, producing a 0-byte video and a baffling ffmpeg mux
# error three steps later. Clamp to something a container can actually store.
FPS_MIN, FPS_MAX, FPS_DEFAULT = 1.0, 240.0, 30.0

# Anything shorter than this cannot be a real short; it is a symptom of a
# broken highlight window (inverted times, clamped-to-nothing chunk math).
MIN_SUBCLIP_SECONDS = 0.5

# Haar detection is the single most expensive thing in the render loop
# (~40-80 ms per 720p frame on a 2-core runner). Running it on EVERY frame of
# a 40-second 30 fps clip costs 1200 detections ≈ 60-90 s per clip, purely to
# re-discover a face that moves a few pixels. We detect on a stride and let
# the existing exponential smoothing carry the crop between detections —
# visually identical, roughly 5x faster, and it keeps a 3-clip render well
# inside the job's time budget.
FACE_DETECT_STRIDE = 5


def _sane_fps(raw, default: float = FPS_DEFAULT) -> float:
    """Coerce OpenCV's CAP_PROP_FPS into a value VideoWriter will accept.

    ``cap.get()`` happily returns 0.0, NaN, a negative, or an absurd value for
    containers with a broken/variable frame rate. The old ``raw or 30.0`` only
    caught 0.0 — NaN is truthy, so it sailed straight through into VideoWriter.
    """
    try:
        fps = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(fps) or fps < FPS_MIN or fps > FPS_MAX:
        return default
    return fps



def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg fast-seek cut → re-encoded mp4 with audio.

    -ss/-t are placed BEFORE -i (input seeking): ffmpeg jumps to the nearest
    keyframe at/before the target and decodes forward — frame-accurate on any
    modern ffmpeg when re-encoding, and 10–50x faster than output seeking on
    long sources (which would decode from t=0 for every single clip).
    """
    # Reject a nonsense window up front. The old `max(0.001, end - start)`
    # turned an inverted or zero-length highlight into a 1-millisecond cut
    # that decoded to zero frames and failed much later with an opaque error.
    if not (math.isfinite(start) and math.isfinite(end)):
        raise RuntimeError(f"non-finite clip window {start}→{end}")
    if start < 0:
        raise RuntimeError(f"negative clip start {start:.3f}s")
    duration = end - start
    if duration < MIN_SUBCLIP_SECONDS:
        raise RuntimeError(
            f"clip window {start:.3f}s→{end:.3f}s is {duration:.3f}s — "
            f"below the {MIN_SUBCLIP_SECONDS}s floor a real clip needs"
        )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", source_path,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    _run_ffmpeg(cmd, f"cut {start:.1f}s→{end:.1f}s")
    return out_path


def _load_face_cascade(cv2):
    """Return a Haar face detector, or None if this OpenCV cannot supply one.

    OpenCV 5.0 removed ``cv2.CascadeClassifier`` and ships an empty
    ``cv2/data/`` directory, so the old unconditional call raised
    ``AttributeError`` and killed the whole render. requirements.txt now pins
    ``<5``, but a future wheel resolution must degrade to a static centre crop
    instead of destroying an otherwise-good run — so this never raises.
    """
    version = getattr(cv2, "__version__", "?")
    if not hasattr(cv2, "CascadeClassifier"):
        print(f"[clip] ⚠ OpenCV {version} has no CascadeClassifier — face tracking OFF, "
              "using a static centre crop (pin opencv-python-headless<5 to restore it)",
              flush=True)
        return None

    cascade_dir = getattr(getattr(cv2, "data", None), "haarcascades", "") or ""
    path = os.path.join(cascade_dir, "haarcascade_frontalface_default.xml")
    if not cascade_dir or not os.path.exists(path):
        print(f"[clip] ⚠ OpenCV {version} ships no bundled Haar cascades — face tracking OFF, "
              "using a static centre crop", flush=True)
        return None

    try:
        cascade = cv2.CascadeClassifier(path)
        if cascade.empty():
            raise RuntimeError("cascade loaded empty")
    except Exception as e:  # noqa: BLE001 - never let detector setup kill a render
        print(f"[clip] ⚠ Haar cascade unusable ({e}) — face tracking OFF, centre crop",
              flush=True)
        return None
    return cascade


def _reframe_vertical(in_path: str, out_path: str, aspect_ratio: str) -> str:
    """Crop the cut clip to the target aspect ratio, tracking faces if possible."""
    try:
        import cv2  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "opencv-python is required for --mode local. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e

    target_ratio = _ratio(aspect_ratio)
    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {in_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = _sane_fps(cap.get(cv2.CAP_PROP_FPS))

    # isOpened() can be True while the properties are still garbage (broken
    # header, 0-byte cut). Without this, src_w / src_h raises ZeroDivisionError
    # and the traceback points at arithmetic instead of at the real cause.
    if src_w <= 0 or src_h <= 0:
        cap.release()
        raise RuntimeError(
            f"unreadable frame size {src_w}x{src_h} in {in_path} — the cut step "
            "probably produced an empty/corrupt file"
        )

    # Compute the largest crop that fits inside the frame at the target ratio.
    if target_ratio < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target_ratio)
    crop_w = max(2, crop_w - (crop_w % 2))
    crop_h = max(2, crop_h - (crop_h % 2))

    face_cascade = _load_face_cascade(cv2)

    silent_path = out_path + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(silent_path, fourcc, fps, (crop_w, crop_h))
    # A VideoWriter that failed to open accepts write() calls and discards
    # them, so the failure only surfaces later as an inscrutable ffmpeg error.
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(
            f"OpenCV could not open a VideoWriter for {silent_path} "
            f"({crop_w}x{crop_h} @ {fps}fps, mp4v)"
        )

    frames_written = 0
    last_center: Optional[Tuple[int, int]] = None
    smoothing = 0.15  # how aggressively to chase a new face position
    # RESOURCE SAFETY: cap/writer used to be released by two bare statements
    # after the loop. Any exception raised inside the loop — a cvtColor on a
    # malformed frame, an OOM on a huge resolution, a KeyboardInterrupt —
    # skipped both releases, leaking the decoder's file descriptor and its
    # internal buffers. Across a multi-video campaign that is a real fd/memory
    # leak, so both now live in a finally.
    try:
        frame_index = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            faces = ()
            # Detect on a stride; smoothing carries the crop in between.
            if face_cascade is not None and frame_index % FACE_DETECT_STRIDE == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            frame_index += 1
            if len(faces) > 0:
                # Pick the largest face — usually the speaker.
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                cx = x + w // 2
                cy = y + h // 2
                if last_center is None:
                    last_center = (cx, cy)
                else:
                    lx, ly = last_center
                    last_center = (
                        int(lx + (cx - lx) * smoothing),
                        int(ly + (cy - ly) * smoothing),
                    )
            if last_center is None:
                last_center = (src_w // 2, src_h // 2)

            cx, cy = last_center
            x0 = max(0, min(src_w - crop_w, cx - crop_w // 2))
            y0 = max(0, min(src_h - crop_h, cy - crop_h // 2))
            cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w]
            writer.write(cropped)
            frames_written += 1
    finally:
        cap.release()
        writer.release()

    # Zero frames means the mux below would emit a 0-length or malformed clip
    # that still "succeeds". Fail loudly here instead of shipping a dud short.
    if frames_written == 0:
        if os.path.exists(silent_path):
            os.remove(silent_path)
        raise RuntimeError(f"no frames could be decoded from {in_path}")

    # Mux audio from the cut clip back onto the silent reframed video.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", silent_path,
        "-i", in_path,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-map", "0:v:0", "-map", "1:a:0?",
        "-shortest",
        out_path,
    ]
    try:
        _run_ffmpeg(cmd, "audio mux")
    finally:
        # Audit hygiene: the temp silent file must never be orphaned, even
        # when the mux fails (a 30-second 720p leftover is ~10–20 MB).
        if os.path.exists(silent_path):
            os.remove(silent_path)
    return out_path


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
) -> str:
    """Cut + reframe one highlight, returning the local mp4 path."""
    cut_path = out_path + ".cut.mp4"
    try:
        _cut_subclip(source_path, start_time, end_time, cut_path)
        _reframe_vertical(cut_path, out_path, aspect_ratio)
    except BaseException:
        # A HALF-WRITTEN out_path is worse than no file at all: the caller
        # records the clip as failed, but the corrupt mp4 stays on disk, gets
        # swept into the artifact bundle, and — if it happens to be named
        # short_01.mp4 — is indistinguishable from a real clip to every glob
        # in this repo. Remove it on every failure path.
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        raise
    finally:
        if os.path.exists(cut_path):
            os.remove(cut_path)
    return out_path


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
) -> List[Dict]:
    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"short_{i:02d}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
            )
            results.append({**h, "clip_url": out_path})
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
