#!/usr/bin/env python3
"""Offline self-test for the face-detector guard in local/clipper.py.

Background: the runner resolved the unpinned ``opencv-python-headless>=4.8.0``
to **5.0.0.93**. OpenCV 5 removed ``cv2.CascadeClassifier`` outright and ships
an EMPTY ``cv2/data/`` directory, so the old unconditional

    cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

raised ``AttributeError: module 'cv2' has no attribute 'CascadeClassifier'``
and would have killed the 9:16 reframe on every clip.

requirements.txt now pins ``<5``. This test additionally guarantees the
*runtime* guard never regresses: a missing detector must degrade to a static
centre crop, never raise.

    python3 scripts/selftest_clipper.py     # exit 0 = all assertions pass

No network. The render assertions are skipped automatically when ffmpeg or
PyAV are unavailable.
"""
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILS = 0
SKIPS = 0


def check(cond, label):
    global FAILS
    print(("   PASS  " if cond else "   FAIL  ") + label)
    if not cond:
        FAILS += 1


def skip(label):
    global SKIPS
    SKIPS += 1
    print("   SKIP  " + label)


try:
    import cv2
except ImportError:
    print("cv2 not installed — nothing to test")
    sys.exit(0)

from shorts_generator.local.clipper import _load_face_cascade, _ratio

print("=" * 78)
print(f"opencv version under test: {cv2.__version__}")

print("\nT1: aspect-ratio parsing")
check(abs(_ratio("9:16") - 0.5625) < 1e-9, "'9:16' -> 0.5625")
check(abs(_ratio("1:1") - 1.0) < 1e-9, "'1:1' -> 1.0")
check(abs(_ratio("garbage") - 0.5625) < 1e-9, "malformed input falls back to 9:16")

print("\nT2: real OpenCV in this env")
major = int(str(cv2.__version__).split(".")[0])
cascade = _load_face_cascade(cv2)
if major < 5:
    check(cascade is not None, f"OpenCV {cv2.__version__} (<5) supplies a working Haar cascade")
else:
    check(cascade is None, f"OpenCV {cv2.__version__} (>=5) degrades to None instead of raising")

print("\nT3: simulated OpenCV 5 — the exact runner breakage")
saved_cc = getattr(cv2, "CascadeClassifier", None)
saved_data = getattr(cv2, "data", None)
saved_ver = cv2.__version__
try:
    if saved_cc is not None:
        del cv2.CascadeClassifier
    cv2.__version__ = "5.0.0"

    raised = False
    try:  # what the pre-fix line did
        cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except AttributeError:
        raised = True
    check(raised, "pre-fix call still reproduces AttributeError (bug is real)")

    got = "no-exception"
    try:
        got = _load_face_cascade(cv2)
    except Exception as e:  # noqa: BLE001
        got = e
    check(got is None, "guarded loader returns None and does NOT raise")
finally:
    if saved_cc is not None:
        cv2.CascadeClassifier = saved_cc
    if saved_data is not None:
        cv2.data = saved_data
    cv2.__version__ = saved_ver

print("\nT4: empty/missing cascade directory")


class _NoData:
    haarcascades = "/definitely/not/here/"


saved_data = cv2.data
try:
    cv2.data = _NoData()
    check(_load_face_cascade(cv2) is None, "missing cascade file -> None, no raise")
finally:
    cv2.data = saved_data

print("\nT5: end-to-end 9:16 render (needs ffmpeg + PyAV)")
have_ffmpeg = shutil.which("ffmpeg") is not None
try:
    import av  # noqa: F401
    have_av = True
except ImportError:
    have_av = False

if not (have_ffmpeg and have_av):
    skip(f"ffmpeg={'yes' if have_ffmpeg else 'no'} pyav={'yes' if have_av else 'no'}")
else:
    import subprocess
    import tempfile
    import av
    from shorts_generator.local.clipper import _reframe_vertical

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc=size=1280x720:rate=25:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", src], check=True)
        out = os.path.join(td, "out.mp4")
        _reframe_vertical(src, out, "9:16")
        check(os.path.exists(out), "reframe produced an output file")
        c = av.open(out)
        v = next(s for s in c.streams if s.type == "video")
        w, h = v.codec_context.width, v.codec_context.height
        check(abs(w / h - 0.5625) <= 0.02, f"output {w}x{h} ratio={w / h:.4f} is 9:16 (tol 0.02)")
        check(any(s.type == "audio" for s in c.streams), "audio was muxed back in")
        c.close()
        check(not [p for p in os.listdir(td) if p.endswith(".silent.mp4")],
              "temp .silent.mp4 cleaned up")

print("\n" + "=" * 78)
print(f"RESULT: {'ALL CHECKS PASSED' if FAILS == 0 else str(FAILS) + ' CHECK(S) FAILED'}"
      + (f"  ({SKIPS} skipped)" if SKIPS else ""))
sys.exit(1 if FAILS else 0)
