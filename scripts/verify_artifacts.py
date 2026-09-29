#!/usr/bin/env python3
"""Verify a downloaded `gh run download` artifact bundle from the clipping campaign.

Checks what can actually be proven from the files on disk and refuses to guess
about anything else: every unprovable item is recorded under "unverified" in the
report instead of being silently passed.

What it proves per rendered clip (``short_*.mp4``):
  * file exists and is non-empty
  * duration inside the pipeline's DURATION LOCK window (default 20-40s,
    enforced by shorts_generator/highlights.py)
  * a video stream exists, and its pixel aspect ratio matches the target
    (default 9:16) within tolerance
  * an audio stream exists (a silent clip renders fine but is worthless)
  * the ``<clip>.youtube.json`` / ``<clip>.instagram.json`` metadata sidecars
    written by pipeline.write_metadata_sidecars exist and are well-formed
  * YouTube title length, Instagram fold-line (first caption line) <= 60 chars,
    hashtag counts, and the FTC disclosure tags enforced by metadata.py
  * text integrity: UTF-8 replacement chars / classic mojibake sequences are a
    hard FAIL; Devanagari-vs-Latin script mix is REPORTED, never failed, because
    Hinglish titles are legitimate output for a Hindi source.

Probing strategy: ffprobe when it is on PATH (authoritative), otherwise a small
stdlib MP4 box parser (moov/mvhd + trak/tkhd/hdlr). If neither can read a file,
the clip's media checks land in "unverified" rather than being assumed good.

Usage:
    python3 scripts/verify_artifacts.py <artifacts_dir> [--json-out report.json]

Exit codes: 0 = PASS, 1 = FAIL, 2 = NO_ARTIFACTS (nothing to verify).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "verify_artifacts/1"

# Mirrors highlights.py "DURATION LOCK: strictly between 20 and 40 seconds".
DEFAULT_MIN_SECONDS = 20.0
DEFAULT_MAX_SECONDS = 40.0
# Rendering crops to integer even pixel sizes, so allow a small ratio drift.
DEFAULT_RATIO_TOLERANCE = 0.02
# metadata.py: IG_HOOK_LINE_MAX_CHARS and _clip_title(cap=60).
IG_HOOK_LINE_MAX_CHARS = 60
YT_TITLE_HARD_MAX = 60
YT_TITLE_TARGET_MIN = 40
HASHTAG_MIN, HASHTAG_MAX = 3, 5

# Classic UTF-8-decoded-as-latin-1 debris plus the Unicode replacement char.
MOJIBAKE_PATTERNS = [
    "\ufffd",          # U+FFFD REPLACEMENT CHARACTER
    "à¤", "à¥",        # Devanagari bytes read as latin-1
    "Ã¢", "Ã©", "Ã¨", "Ã¯", "Â ", "Â·", "â€™", "â€œ", "â€\x9d", "â€“",
]


# --------------------------------------------------------------------------- #
# media probing
# --------------------------------------------------------------------------- #
def _ffprobe_path() -> Optional[str]:
    return shutil.which("ffprobe")


def probe_with_ffprobe(path: Path, exe: str) -> Dict[str, Any]:
    """Authoritative probe. Raises on failure so the caller can fall back."""
    out = subprocess.run(
        [exe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", []) or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = None
    fmt_dur = (data.get("format") or {}).get("duration")
    if fmt_dur not in (None, "N/A"):
        try:
            duration = float(fmt_dur)
        except (TypeError, ValueError):
            duration = None
    if duration is None and video and video.get("duration") not in (None, "N/A"):
        try:
            duration = float(video["duration"])
        except (TypeError, ValueError):
            duration = None

    return {
        "probe": "ffprobe",
        "duration_s": duration,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "nb_streams": len(streams),
    }


def _iter_boxes(buf: bytes, start: int, end: int):
    """Yield (box_type, payload_start, payload_end) for an MP4 box region."""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        btype = buf[pos + 4:pos + 8].decode("latin-1", "replace")
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield btype, pos + header, pos + size
        pos += size


def probe_with_builtin(path: Path) -> Dict[str, Any]:
    """Minimal stdlib MP4 parser: mvhd duration + per-track tkhd/hdlr.

    Good enough for files ffmpeg just wrote. Returns has_video/has_audio from
    the handler-type boxes and display size from the video track's tkhd.
    """
    buf = path.read_bytes()
    n = len(buf)
    moov: Optional[Tuple[int, int]] = None
    for btype, s, e in _iter_boxes(buf, 0, n):
        if btype == "moov":
            moov = (s, e)
            break
    if moov is None:
        raise ValueError("no moov box (not a readable MP4)")

    duration = None
    width = height = None
    has_video = has_audio = False
    video_codec = audio_codec = None
    tracks = 0

    for btype, s, e in _iter_boxes(buf, *moov):
        if btype == "mvhd":
            version = buf[s]
            if version == 1:
                timescale = struct.unpack(">I", buf[s + 20:s + 24])[0]
                dur = struct.unpack(">Q", buf[s + 24:s + 32])[0]
            else:
                timescale = struct.unpack(">I", buf[s + 12:s + 16])[0]
                dur = struct.unpack(">I", buf[s + 16:s + 20])[0]
            if timescale:
                duration = dur / timescale
        elif btype == "trak":
            tracks += 1
            handler = None
            tk_w = tk_h = None
            codec = None
            for t, ts, te in _iter_boxes(buf, s, e):
                if t == "tkhd":
                    # FullBox(4) + [v1: 8+8+4+4+8=32 | v0: 4+4+4+4+4=20]
                    # + reserved(8) + layer/alt/volume/reserved(8) + matrix(36)
                    version = buf[ts]
                    off = ts + (88 if version == 1 else 76)
                    if off + 8 <= te:
                        w_fixed, h_fixed = struct.unpack(">II", buf[off:off + 8])
                        tk_w, tk_h = w_fixed >> 16, h_fixed >> 16
                elif t == "mdia":
                    for m, ms, me in _iter_boxes(buf, ts, te):
                        if m == "hdlr" and ms + 12 <= me:
                            handler = buf[ms + 8:ms + 12].decode("latin-1", "replace")
                        elif m == "minf":
                            for mi, mis, mie in _iter_boxes(buf, ms, me):
                                if mi != "stbl":
                                    continue
                                for st, sts, ste in _iter_boxes(buf, mis, mie):
                                    if st == "stsd" and sts + 16 <= ste:
                                        codec = buf[sts + 12:sts + 16].decode("latin-1", "replace")
            if handler == "vide":
                has_video = True
                video_codec = codec
                if tk_w and tk_h:
                    width, height = tk_w, tk_h
            elif handler == "soun":
                has_audio = True
                audio_codec = codec

    return {
        "probe": "builtin",
        "duration_s": duration,
        "width": width,
        "height": height,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "has_video": has_video,
        "has_audio": has_audio,
        "nb_streams": tracks,
    }


def probe_media(path: Path, exe: Optional[str]) -> Dict[str, Any]:
    errors = []
    if exe:
        try:
            return probe_with_ffprobe(path, exe)
        except Exception as e:  # noqa: BLE001 - fall back to the builtin parser
            errors.append(f"ffprobe: {e}")
    try:
        info = probe_with_builtin(path)
        if errors:
            info["probe_notes"] = errors
        return info
    except Exception as e:  # noqa: BLE001
        errors.append(f"builtin: {e}")
        return {"probe": "none", "probe_errors": errors, "duration_s": None,
                "width": None, "height": None, "video_codec": None,
                "audio_codec": None, "has_video": None, "has_audio": None}


# --------------------------------------------------------------------------- #
# text integrity
# --------------------------------------------------------------------------- #
def script_profile(text: str) -> Dict[str, Any]:
    """Count Devanagari vs Latin letters and flag mojibake. Never judges style."""
    deva = latin = other_letters = digits = 0
    for ch in text or "":
        if ch.isdigit():
            digits += 1
            continue
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            other_letters += 1
            continue
        if "DEVANAGARI" in name:
            deva += 1
        elif "LATIN" in name:
            latin += 1
        else:
            other_letters += 1

    hits = sorted({p for p in MOJIBAKE_PATTERNS if p and p in (text or "")})
    letters = deva + latin + other_letters
    if letters == 0:
        script = "none"
    elif deva and latin:
        script = "mixed_deva_latin"
    elif deva:
        script = "devanagari"
    elif latin:
        script = "latin"
    else:
        script = "other"

    return {
        "devanagari_letters": deva,
        "latin_letters": latin,
        "other_letters": other_letters,
        "digits": digits,
        "script": script,
        "devanagari_ratio": round(deva / letters, 3) if letters else 0.0,
        "mojibake_hits": hits,
        "has_replacement_char": "\ufffd" in (text or ""),
    }


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []
        self.unverified: List[Dict[str, Any]] = []

    def add(self, scope: str, name: str, status: str, detail: str = "",
            **extra: Any) -> None:
        entry = {"scope": scope, "check": name, "status": status}
        if detail:
            entry["detail"] = detail
        entry.update(extra)
        if status == "unverified":
            self.unverified.append(entry)
        self.checks.append(entry)

    def counts(self) -> Dict[str, int]:
        out = {"pass": 0, "fail": 0, "warn": 0, "info": 0, "unverified": 0}
        for c in self.checks:
            out[c["status"]] = out.get(c["status"], 0) + 1
        return out


def read_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def check_hashtags(rep: Report, scope: str, platform: str, tags: Any,
                   ftc_tags: List[str]) -> None:
    if not isinstance(tags, list):
        rep.add(scope, f"{platform}.hashtags_is_list", "fail",
                f"expected a list, got {type(tags).__name__}")
        return
    clean = [t for t in tags if isinstance(t, str) and t.strip()]
    rep.add(scope, f"{platform}.hashtag_count", "pass" if HASHTAG_MIN <= len(clean) <= HASHTAG_MAX + len(ftc_tags) else "warn",
            f"{len(clean)} tags (expect {HASHTAG_MIN}-{HASHTAG_MAX} topical + {len(ftc_tags)} FTC)",
            value=len(clean), tags=clean)
    missing = [t for t in ftc_tags if t not in clean]
    rep.add(scope, f"{platform}.ftc_disclosure_tags", "pass" if not missing else "fail",
            "all present" if not missing else f"missing {missing}", missing=missing)


def check_text(rep: Report, scope: str, field: str, text: str) -> Dict[str, Any]:
    prof = script_profile(text)
    if prof["has_replacement_char"] or prof["mojibake_hits"]:
        rep.add(scope, f"{field}.text_integrity", "fail",
                f"encoding damage: {prof['mojibake_hits'] or ['U+FFFD']}", **prof)
    else:
        rep.add(scope, f"{field}.text_integrity", "pass", "no mojibake / replacement chars",
                script=prof["script"], devanagari_ratio=prof["devanagari_ratio"])
    rep.add(scope, f"{field}.script_profile", "info",
            f"{prof['script']} (deva={prof['devanagari_letters']}, latin={prof['latin_letters']})",
            **prof)
    return prof


def verify_clip(rep: Report, clip: Path, root: Path, exe: Optional[str],
                args: argparse.Namespace, ftc_tags: List[str]) -> Dict[str, Any]:
    scope = str(clip.relative_to(root))
    rec: Dict[str, Any] = {"clip": scope, "bytes": None}

    size = clip.stat().st_size
    rec["bytes"] = size
    rep.add(scope, "file_non_empty", "pass" if size > 0 else "fail", f"{size} bytes", value=size)
    if size == 0:
        return rec

    info = probe_media(clip, exe)
    rec.update(info)

    # duration
    dur = info.get("duration_s")
    if dur is None:
        rep.add(scope, "duration_window", "unverified",
                f"could not read duration ({info.get('probe')})")
    else:
        ok = args.min_seconds <= dur <= args.max_seconds
        rep.add(scope, "duration_window", "pass" if ok else "fail",
                f"{dur:.2f}s (lock {args.min_seconds:g}-{args.max_seconds:g}s)",
                value=round(dur, 3))

    # aspect ratio
    w, h = info.get("width"), info.get("height")
    if not w or not h:
        rep.add(scope, "aspect_ratio", "unverified",
                f"could not read frame size ({info.get('probe')})")
    else:
        actual = w / h
        ok = abs(actual - args.target_ratio) <= args.ratio_tolerance
        rep.add(scope, "aspect_ratio", "pass" if ok else "fail",
                f"{w}x{h} = {actual:.4f} (target {args.aspect_ratio} = {args.target_ratio:.4f}, tol {args.ratio_tolerance})",
                width=w, height=h, ratio=round(actual, 4))
        rec["resolution"] = f"{w}x{h}"

    # streams
    for kind in ("video", "audio"):
        present = info.get(f"has_{kind}")
        if present is None:
            rep.add(scope, f"has_{kind}_stream", "unverified", "probe unavailable")
        else:
            rep.add(scope, f"has_{kind}_stream", "pass" if present else "fail",
                    f"{kind} codec={info.get(f'{kind}_codec')}")

    # metadata sidecars
    base = clip.with_suffix("")
    for platform, required in (("youtube", ("title", "description", "hashtags")),
                               ("instagram", ("caption", "hashtags"))):
        side = Path(f"{base}.{platform}.json")
        sscope = f"{scope} [{platform}]"
        if not side.exists():
            rep.add(sscope, "sidecar_exists", "fail", f"missing {side.name}")
            continue
        rep.add(sscope, "sidecar_exists", "pass", side.name)
        data, err = read_json(side)
        if err or not isinstance(data, dict):
            rep.add(sscope, "sidecar_parses", "fail", err or "not a JSON object")
            continue
        rep.add(sscope, "sidecar_parses", "pass", "valid JSON object")
        for field in required:
            if field not in data:
                rep.add(sscope, f"{field}.present", "fail", "field missing")
        rec.setdefault("metadata", {})[platform] = data

        if platform == "youtube":
            title = str(data.get("title") or "")
            rep.add(sscope, "title.non_empty", "pass" if title.strip() else "fail",
                    f"{len(title)} chars", value=title)
            if title:
                rep.add(sscope, "title.hard_max_60",
                        "pass" if len(title) <= YT_TITLE_HARD_MAX else "fail",
                        f"{len(title)} chars (cap {YT_TITLE_HARD_MAX})", value=len(title))
                rep.add(sscope, "title.target_40_60",
                        "pass" if YT_TITLE_TARGET_MIN <= len(title) <= YT_TITLE_HARD_MAX else "warn",
                        f"{len(title)} chars (prompt asks {YT_TITLE_TARGET_MIN}-{YT_TITLE_HARD_MAX})",
                        value=len(title))
                check_text(rep, sscope, "title", title)
            desc = str(data.get("description") or "")
            rep.add(sscope, "description.non_empty", "pass" if desc.strip() else "fail",
                    f"{len(desc)} chars")
            if desc:
                check_text(rep, sscope, "description", desc)
        else:
            cap = str(data.get("caption") or "")
            rep.add(sscope, "caption.non_empty", "pass" if cap.strip() else "fail",
                    f"{len(cap)} chars")
            if cap:
                first = cap.splitlines()[0] if cap.splitlines() else ""
                rep.add(sscope, "caption.fold_line_max_60",
                        "pass" if len(first) <= IG_HOOK_LINE_MAX_CHARS else "fail",
                        f"first line {len(first)} chars (cap {IG_HOOK_LINE_MAX_CHARS}): {first!r}",
                        value=len(first))
                check_text(rep, sscope, "caption", cap)
        check_hashtags(rep, sscope, platform, data.get("hashtags"), ftc_tags)

    return rec


def verify_result_json(rep: Report, path: Path, root: Path) -> Dict[str, Any]:
    scope = str(path.relative_to(root))
    data, err = read_json(path)
    rec: Dict[str, Any] = {"file": scope}
    if err or not isinstance(data, dict):
        rep.add(scope, "result_json_parses", "fail", err or "not a JSON object")
        return rec
    rep.add(scope, "result_json_parses", "pass", "valid JSON object")

    shorts = data.get("shorts")
    if not isinstance(shorts, list):
        rep.add(scope, "shorts_array", "fail", "no 'shorts' array")
        return rec
    rep.add(scope, "shorts_array", "pass", f"{len(shorts)} entries", value=len(shorts))
    rec["shorts"] = len(shorts)

    # pipeline.generate_shorts reports highlights whose render failed here
    # instead of leaving them in "shorts" with a null clip_url. Surface them:
    # a run can be green overall and still have silently lost clips.
    failed_clips = data.get("failed_clips")
    if isinstance(failed_clips, list) and failed_clips:
        rec["failed_clips"] = len(failed_clips)
        for fc in failed_clips:
            if isinstance(fc, dict):
                rep.add(scope, "clip_render_failure", "warn",
                        f"{fc.get('title', '(untitled)')}: {str(fc.get('error'))[:200]}")
    elif failed_clips is None:
        rep.add(scope, "failed_clips_key", "info",
                "no 'failed_clips' key (pre-fix pipeline, or nothing failed)")
    else:
        rep.add(scope, "clip_render_failure", "pass", "no clips failed to render")

    transcript = data.get("transcript")
    if isinstance(transcript, dict):
        segs = transcript.get("segments")
        lang = transcript.get("language")
        rec["transcript_language"] = lang
        rec["transcript_segments"] = len(segs) if isinstance(segs, list) else None
        rep.add(scope, "transcript.segments", "pass" if isinstance(segs, list) and segs else "fail",
                f"{len(segs) if isinstance(segs, list) else 0} segments, language={lang!r}")
        if isinstance(segs, list) and segs:
            joined = " ".join(str(s.get("text", "")) for s in segs[:80])
            check_text(rep, scope, "transcript_sample", joined)
    else:
        rep.add(scope, "transcript.present", "unverified", "no transcript object in result JSON")

    hooks = []
    durations = []
    for i, s in enumerate(shorts):
        if not isinstance(s, dict):
            continue
        sscope = f"{scope} [short {i}]"
        hook = str(s.get("hook_sentence") or "")
        hooks.append(hook)
        rep.add(sscope, "hook_sentence.non_empty", "pass" if hook.strip() else "fail",
                f"{len(hook)} chars", value=hook)
        if hook:
            check_text(rep, sscope, "hook_sentence", hook)
        try:
            d = float(s.get("end_time", 0)) - float(s.get("start_time", 0))
            durations.append(round(d, 2))
        except (TypeError, ValueError):
            rep.add(sscope, "timing.parsable", "fail", "start_time/end_time not numeric")
    rec["hooks"] = hooks
    rec["planned_durations_s"] = durations
    return rec


def verify_summary(rep: Report, path: Path, root: Path) -> Dict[str, Any]:
    scope = str(path.relative_to(root))
    data, err = read_json(path)
    if err or not isinstance(data, dict):
        rep.add(scope, "summary_parses", "fail", err or "not a JSON object")
        return {"file": scope}
    rep.add(scope, "summary_parses", "pass", "valid JSON object")
    ok, failed = data.get("ok"), data.get("failed")
    rep.add(scope, "campaign_result", "pass" if ok and not failed else ("fail" if failed else "warn"),
            f"ok={ok} failed={failed} skipped={len(data.get('skipped_strikeouts') or [])}")
    for v in data.get("videos") or []:
        if isinstance(v, dict) and v.get("status") == "failed":
            rep.add(scope, "video_error", "fail",
                    f"{v.get('url')}: {str(v.get('error'))[:400]}")
    return {"file": scope, "ok": ok, "failed": failed, "raw": data}


# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("artifacts_dir", help="directory produced by `gh run download -D ...`")
    p.add_argument("--json-out", default=None, help="write the machine-readable report here")
    p.add_argument("--aspect-ratio", default="9:16", help="expected aspect ratio (default 9:16)")
    p.add_argument("--min-seconds", type=float, default=DEFAULT_MIN_SECONDS)
    p.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    p.add_argument("--ratio-tolerance", type=float, default=DEFAULT_RATIO_TOLERANCE)
    p.add_argument("--ftc-tags", default=os.getenv("FTC_DISCLOSURE_TAGS", "#ad #sponsored"),
                   help="space-separated disclosure tags that must appear on every clip")
    p.add_argument("--expect-clips", type=int, default=None,
                   help="fail unless exactly this many clips are present")
    args = p.parse_args()

    try:
        w, h = args.aspect_ratio.split(":")
        args.target_ratio = float(w) / float(h)
    except Exception:  # noqa: BLE001
        print(f"error: bad --aspect-ratio {args.aspect_ratio!r}", file=sys.stderr)
        return 2

    root = Path(args.artifacts_dir).expanduser()
    if not root.exists():
        print(f"NO_ARTIFACTS: {root} does not exist", file=sys.stderr)
        report = {"schema": SCHEMA, "verdict": "NO_ARTIFACTS",
                  "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                  "artifacts_root": str(root),
                  "error": "artifacts directory does not exist",
                  "totals": {"clips": 0}, "checks": [], "unverified": []}
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
            print(f"report → {args.json_out}")
        return 2

    ftc_tags = [t for t in args.ftc_tags.split() if t]
    exe = _ffprobe_path()
    rep = Report()

    clips = sorted(root.rglob("short_*.mp4"))
    results = sorted(root.rglob("result_*.json")) + sorted(root.rglob("result.json"))
    summaries = sorted(root.rglob("summary.json"))

    print("=" * 74)
    print(f"verify_artifacts — {root}")
    print(f"probe backend: {'ffprobe (' + exe + ')' if exe else 'builtin stdlib MP4 parser (ffprobe not on PATH)'}")
    print(f"found: {len(clips)} clip(s), {len(results)} result json, {len(summaries)} summary json")
    print("=" * 74)

    if not exe:
        rep.add("environment", "ffprobe_available", "warn",
                "ffprobe not on PATH — media facts come from the builtin MP4 parser")

    summary_recs = [verify_summary(rep, s, root) for s in summaries]
    result_recs = [verify_result_json(rep, r, root) for r in results]
    clip_recs = [verify_clip(rep, c, root, exe, args, ftc_tags) for c in clips]

    if args.expect_clips is not None:
        rep.add("bundle", "expected_clip_count",
                "pass" if len(clips) == args.expect_clips else "fail",
                f"{len(clips)} found, expected {args.expect_clips}")
    if not clips:
        rep.add("bundle", "clips_present", "fail", "no short_*.mp4 anywhere in the bundle")

    counts = rep.counts()
    durations = [c.get("duration_s") for c in clip_recs if c.get("duration_s") is not None]
    verdict = "NO_ARTIFACTS" if not clips and not results and not summaries else (
        "FAIL" if counts["fail"] else "PASS")

    report = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "artifacts_root": str(root),
        "probe_backend": "ffprobe" if exe else "builtin",
        "expectations": {
            "aspect_ratio": args.aspect_ratio,
            "target_ratio": round(args.target_ratio, 4),
            "ratio_tolerance": args.ratio_tolerance,
            "duration_lock_s": [args.min_seconds, args.max_seconds],
            "ftc_tags": ftc_tags,
            "ig_fold_line_max_chars": IG_HOOK_LINE_MAX_CHARS,
            "yt_title_chars": [YT_TITLE_TARGET_MIN, YT_TITLE_HARD_MAX],
        },
        "totals": {
            "clips": len(clips),
            "result_json": len(results),
            "summary_json": len(summaries),
            **{f"checks_{k}": v for k, v in counts.items()},
        },
        "duration_range_s": ([min(durations), max(durations)] if durations else None),
        "summaries": summary_recs,
        "results": result_recs,
        "clips": clip_recs,
        "checks": rep.checks,
        "unverified": rep.unverified,
        "verdict": verdict,
    }

    for c in rep.checks:
        if c["status"] in ("fail", "warn", "unverified"):
            print(f"  [{c['status'].upper():10}] {c['scope']} :: {c['check']} — {c.get('detail', '')}")
    print("-" * 74)
    print(f"checks: {counts['pass']} pass / {counts['fail']} fail / {counts['warn']} warn "
          f"/ {counts['unverified']} unverified / {counts['info']} info")
    if durations:
        print(f"clip duration range: {min(durations):.2f}s – {max(durations):.2f}s")
    print(f"VERDICT: {verdict}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report → {out}")

    return {"PASS": 0, "FAIL": 1, "NO_ARTIFACTS": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
