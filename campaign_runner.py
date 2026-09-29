"""Headless campaign runner — process a list of YouTube URLs sequentially.

Designed for the GitHub Actions campaign workflow
(.github/workflows/run_campaign.yml): logs to stdout, keeps a
``processed_urls.txt`` ledger (persisted between runs via the workflow
cache) so scheduled reruns only pick up NEW URLs.

Usage:
    python campaign_runner.py --urls-file campaign/urls.txt --num-clips 3

URL file format: one URL per line; blank lines and lines starting with '#'
are ignored. A URL is appended to the processed ledger only on success, so
failures are retried on the next scheduled run — up to MAX_FAILED_ATTEMPTS
times. A URL that keeps failing every single run would otherwise burn
download CPU and Groq Whisper/Llama quota forever, so after the third
strike it is skipped (recorded in campaign/failed_urls.txt).
"""
import argparse
import json
import os
import sys
import time
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from shorts_generator import generate_shorts
from shorts_generator.config import OUTPUT_DIR

# A URL that fails this many campaign runs is declared permanently broken
# (e.g. no speech, geo-blocked, deleted) and skipped from then on — the
# ledger stops the 4x/day cron from re-burning quota on hopeless sources.
MAX_FAILED_ATTEMPTS = 3

# Failures that say "the runner had a bad day", NOT "this URL is hopeless".
# Striking these out would permanently retire a perfectly good video because
# YouTube happened to bot-check a datacenter IP, or because Groq had a blip.
# They are logged and retried forever; only content-level failures count as
# strikes. Matched case-insensitively against str(exception).
TRANSIENT_ERROR_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "cookies-from-browser",
    "refused every innertube client",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "max retries exceeded",
    "remote end closed connection",
    "ssl",
    "429",
    "too many requests",
    "rate limit",
    "500 server error",
    "502",
    "503",
    "504",
    "service unavailable",
    "internal server error",
)


def is_transient_failure(exc: BaseException) -> bool:
    """True when a failure is infrastructure/anti-bot rather than the URL.

    Smart quotes are flattened first: yt-dlp writes "you\u2019re not a bot"
    with U+2019, which a plain substring test would miss.
    """
    msg = str(exc).replace("\u2019", "'").replace("\u2018", "'").lower()
    return any(marker in msg for marker in TRANSIENT_ERROR_MARKERS)


def read_urls(path: str) -> list:
    """Active (non-comment, non-blank) URLs from the campaign file."""
    if not os.path.exists(path):
        return []
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def read_processed(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_processed(path: str, url: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def read_failed_attempts(path: str) -> dict:
    """Map of url → failed-attempt count, from a tab-separated ledger."""
    attempts = {}
    if not os.path.exists(path):
        return attempts
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or "\t" not in line:
                continue
            url, _, count = line.rpartition("\t")
            try:
                attempts[url] = int(count)
            except ValueError:
                continue
    return attempts


def bump_failed_attempt(path: str, url: str, count: int) -> None:
    """Renew the attempt count for a URL (rewrites the small ledger)."""
    attempts = read_failed_attempts(path)
    attempts[url] = count
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for u, c in attempts.items():
            f.write(f"{u}\t{c}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless clipping campaign runner")
    parser.add_argument("--urls-file", default="campaign/urls.txt", help="One YouTube URL per line")
    parser.add_argument("--processed-log", default="campaign/processed_urls.txt",
                        help="Ledger of successfully processed URLs")
    parser.add_argument("--failed-log", default="campaign/failed_urls.txt",
                        help="Ledger of failed-attempt counts (3 strikes → URL skipped)")
    parser.add_argument("--num-clips", type=int, default=3)
    parser.add_argument("--aspect-ratio", default="9:16")
    parser.add_argument("--format", default="720")
    parser.add_argument("--language", default=None)
    args = parser.parse_args()

    urls = read_urls(args.urls_file)
    processed = read_processed(args.processed_log)
    attempts = read_failed_attempts(args.failed_log)
    skipped = [u for u in urls
               if u not in processed and attempts.get(u, 0) >= MAX_FAILED_ATTEMPTS]
    todo = [u for u in urls
            if u not in processed and attempts.get(u, 0) < MAX_FAILED_ATTEMPTS]

    print("=" * 72, flush=True)
    # time.gmtime() + %Z prints an EMPTY zone on Linux — say UTC explicitly.
    print(f"[campaign] {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC", flush=True)
    print(f"[campaign] urls file: {args.urls_file} ({len(urls)} total, {len(processed)} done, "
          f"{len(skipped)} skipped after {MAX_FAILED_ATTEMPTS} strikes, {len(todo)} to do)", flush=True)
    for u in skipped:
        print(f"[campaign] ⏭  skipping permanently-failing URL: {u}", flush=True)
    print("=" * 72, flush=True)

    # Pre-flight: fail FAST (before any download burns CPU/quota) when real
    # work is queued but NO LLM path can possibly work. Tiers: Groq key →
    # Cerebras key → local llama.cpp (tier-3 works with zero keys by default).
    if todo:
        from shorts_generator.config import GROQ_API_KEY, cerebras_fallback_available, local_llm_enabled
        if not (GROQ_API_KEY or cerebras_fallback_available() or local_llm_enabled()):
            print(
                "[campaign] FATAL: work is queued but no LLM path is usable.\n"
                "  Pick ANY one (all free):\n"
                "    GROQ_API_KEY      — free at https://console.groq.com/keys\n"
                "    CEREBRAS_API_KEY  — free (1M tokens/day) at https://cloud.cerebras.ai\n"
                "    LOCAL_LLM=true    — no key at all: runs a small model on the runner's CPU",
                flush=True,
            )
            return 2
        print(
            f"[campaign] backends — Groq: {'✓' if GROQ_API_KEY else '✗'} | "
            f"Cerebras: {'✓' if cerebras_fallback_available() else '✗'} | "
            f"local CPU tier-3: {'✓ (zero-key doomsday mode)' if local_llm_enabled() else '✗'}",
            flush=True,
        )
        from shorts_generator.config import WHISPER_LOCAL_PINNED, LLM_LOCAL_PINNED
        print(
            f"[campaign] providers — whisper: {'📌 local-only (WHISPER_PROVIDER=local)' if WHISPER_LOCAL_PINNED else 'Groq → local fallback'} | "
            f"llm: {'📌 local-only (LLM_PROVIDER=local)' if LLM_LOCAL_PINNED else 'Groq → Cerebras → local'}",
            flush=True,
        )
        # Feedback-loop status line (never fatal — even a corrupt registry
        # must not stop clipping).
        try:
            from shorts_generator.feedback import feedback_configured, load_registry
            fb = (f"✓ ({len(load_registry()['posts'])} registered posts)"
                  if feedback_configured() else "off (OAuth trio not set — see scripts/oauth_local_setup.py)")
        except Exception:
            fb = "off (registry unreadable this run)"
        print(f"[campaign] feedback loop: {fb}", flush=True)
        try:
            from shorts_generator.uploader_youtube import uploads_configured
            up = (f"✓ ON (privacy={os.getenv('YT_PRIVACY', 'private')}, max {os.getenv('UPLOAD_MAX_PER_RUN', '3')}/run)"
                  if uploads_configured() else "off (UPLOAD_ENABLED=false or OAuth trio unset)")
        except Exception:
            up = "off (status unreadable this run)"
        print(f"[campaign] auto-upload: {up}", flush=True)
        try:
            from shorts_generator.uploader_instagram import ig_uploads_configured
            ig_up = ("✓ ON" if ig_uploads_configured() else "off (IG_UPLOAD_ENABLED=false or token/user-id unset)")
        except Exception:
            ig_up = "off (status unreadable this run)"
        print(f"[campaign] instagram upload: {ig_up}", flush=True)

    if not todo:
        print("[campaign] nothing to do — add URLs to the file and rerun.", flush=True)
        run_dir = None
    else:
        run_dir = os.path.join(OUTPUT_DIR, "campaign_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime()))
        os.makedirs(run_dir, exist_ok=True)

    ok, failed = 0, 0
    summaries = []
    for i, url in enumerate(todo, 1):
        print(f"\n[campaign] ({i}/{len(todo)}) {url}", flush=True)
        # Per-video clip dir — otherwise every URL overwrites the previous
        # one's short_*.mp4 files (files land next to this video's JSON).
        video_dir = os.path.join(run_dir, f"video_{i:03d}")
        try:
            result = generate_shorts(
                youtube_url=url,
                num_clips=args.num_clips,
                aspect_ratio=args.aspect_ratio,
                download_format=args.format,
                language=args.language,
                output_dir=video_dir,
            )
            json_path = os.path.join(run_dir, f"result_{i:03d}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            mark_processed(args.processed_log, url)
            ok += 1
            summaries.append({"url": url, "status": "ok", "json": json_path,
                              "clips_dir": video_dir,
                              "shorts": len(result.get("shorts", []))})
            # Phase 3: hand the fresh clips to the upload queue (no-op when
            # UPLOAD_ENABLED=false — queue file untouched, zero side effects).
            try:
                from shorts_generator.uploader_youtube import enqueue_uploads, uploads_configured
                if uploads_configured():
                    enqueue_uploads(result.get("shorts", []), source_url=url)
                from shorts_generator.uploader_instagram import enqueue_ig_uploads, ig_uploads_configured
                if ig_uploads_configured():
                    enqueue_ig_uploads(result.get("shorts", []), source_url=url)
            except Exception as ue:
                print(f"[campaign] ⚠ enqueue skipped ({ue}) — clips stay as artifacts", flush=True)
            print(f"[campaign] ✅ done → {json_path}", flush=True)
        except Exception as e:  # keep the campaign going; retried next run
            failed += 1
            transient = is_transient_failure(e)
            print(f"[campaign] ❌ failed: {e}", flush=True)

            if transient:
                # Infra/anti-bot problem — do NOT spend a strike, or a healthy
                # video gets retired for something that was never its fault.
                current = attempts.get(url, 0)
                summaries.append({"url": url, "status": "failed", "error": str(e),
                                  "failed_attempts": current, "transient": True})
                print(f"[campaign] transient/infra failure — no strike spent "
                      f"(still {current}/{MAX_FAILED_ATTEMPTS}); will retry next run",
                      flush=True)
            else:
                new_count = attempts.get(url, 0) + 1
                bump_failed_attempt(args.failed_log, url, new_count)
                attempts[url] = new_count
                remaining = MAX_FAILED_ATTEMPTS - new_count
                summaries.append({"url": url, "status": "failed", "error": str(e),
                                  "failed_attempts": new_count, "transient": False})
                if remaining > 0:
                    print(f"[campaign] will retry next run ({remaining} attempt(s) left before strike-out)", flush=True)
                else:
                    print(f"[campaign] strike {new_count}/{MAX_FAILED_ATTEMPTS} reached — URL permanently skipped from now on", flush=True)
            traceback.print_exc()

    uploads_summary = None
    if run_dir:
        summary_path = os.path.join(run_dir, "summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump({"run_dir": run_dir, "ok": ok, "failed": failed,
                       "skipped_strikeouts": skipped, "videos": summaries}, f, indent=2)

    # Phase 3: drain the upload queue within budget — runs even on queue-only
    # ticks (no new URLs) so backlogged clips still go out 4×/day.
    try:
        from shorts_generator.uploader_youtube import process_upload_queue, uploads_configured
        if uploads_configured():
            uploads_summary = process_upload_queue()
            dropped = uploads_summary.get("dropped_missing_file", 0)
            print(f"[campaign] uploads — {uploads_summary['uploaded']} posted, "
                  f"{uploads_summary['queued']} still queued, "
                  f"{uploads_summary['failed_attempts']} failed attempt(s)"
                  + (f", {dropped} dropped (clip files gone)" if dropped else ""), flush=True)
        from shorts_generator.uploader_instagram import ig_uploads_configured, process_ig_upload_queue
        if ig_uploads_configured():
            ig_summary = process_ig_upload_queue()
            ig_dropped = ig_summary.get("dropped_missing_file", 0)
            print(f"[campaign] ig uploads — {ig_summary['uploaded']} posted, "
                  f"{ig_summary['queued']} still queued, "
                  f"{ig_summary['failed_attempts']} failed attempt(s)"
                  + (f", {ig_dropped} dropped (clip files gone)" if ig_dropped else ""), flush=True)
    except Exception as ue:
        print(f"[campaign] ⚠ upload step skipped ({ue})", flush=True)

    print("\n" + "=" * 72, flush=True)
    print(f"[campaign] finished: {ok} ok, {failed} failed, {len(skipped)} skipped", flush=True)
    if run_dir:
        print(f"[campaign] run summary → {summary_path}", flush=True)
    print("=" * 72, flush=True)
    # exit 1 ONLY when there WAS queued work and nothing succeeded — a
    # queue-only/no-new-URLs tick (possibly with uploads drained) is a pass.
    if todo and ok == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
