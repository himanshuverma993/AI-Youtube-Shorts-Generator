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

from ..config import OUTPUT_DIR

# A reframe writer fed a nonsense frame rate fails to open and then silently
# swallows every frame, producing a 0-byte video and a baffling ffmpeg mux
# error three steps later. Clamp to something a container can actually store.
FPS_MIN, FPS_MAX, FPS_DEFAULT = 1.0, 240.0, 30.0


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
    duration = max(0.001, end - start)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", source_path,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True)
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
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        faces = ()
        if face_cascade is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
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
        subprocess.run(cmd, check=True)
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
