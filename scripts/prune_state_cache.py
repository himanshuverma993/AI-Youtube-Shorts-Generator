"""Delete rendered clips that nothing is waiting to upload.

Why this exists
---------------
The "Save campaign state" cache persists

    output/campaign_*/video_*/short_*.mp4

because an item sitting in an upload queue is referenced by file PATH, and the
runner disk is ephemeral — drop the .mp4 and the next tick finds a queue full
of missing files.

But the cache key is ``campaign-state-<run_id>``, i.e. a NEW entry every run,
and the glob is unfiltered. So every tick re-uploads the entire accumulated
clip history into a fresh cache entry. With uploads OFF nothing ever leaves the
queues, the entry grows monotonically, and GitHub's 10 GB per-repository cache
limit starts evicting by least-recently-used — including the tier-3 model
weights, which then get re-downloaded on the next run. Four ticks a day turns
that into permanent thrash.

This script keeps only the clips some queue actually references. Everything
else is already safe in the run's uploaded artifact, which is the durable copy.

Run it AFTER the artifact upload and BEFORE the cache save.

    python scripts/prune_state_cache.py            # prune
    python scripts/prune_state_cache.py --dry-run  # just report
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUES = (
    os.path.join(REPO, "campaign", "upload_queue.json"),
    os.path.join(REPO, "campaign", "ig_upload_queue.json"),
)
CLIP_GLOB = os.path.join(REPO, "output", "campaign_*", "video_*", "short_*.mp4")

# Keys a queue item may use to point at its rendered file.
_PATH_KEYS = ("clip_url", "clip_path", "path", "file", "video_path")


def _referenced_paths() -> set:
    """Absolute paths of every clip still referenced by an upload queue."""
    keep = set()
    for qpath in QUEUES:
        if not os.path.exists(qpath):
            continue
        try:
            with open(qpath, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # A queue we cannot read is a queue we cannot prune against.
            # Keeping everything is wasteful; deleting everything could strand
            # a pending upload. Wasteful is the recoverable failure.
            print(f"[prune] ⚠ unreadable queue {qpath}: {exc} — keeping all clips",
                  flush=True)
            return None
        items = data.get("items") if isinstance(data, dict) else data
        for item in items or []:
            if not isinstance(item, dict):
                continue
            for key in _PATH_KEYS:
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    keep.add(os.path.abspath(val))
    return keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    clips = sorted(glob.glob(CLIP_GLOB))
    if not clips:
        print("[prune] no rendered clips on disk — nothing to do", flush=True)
        return 0

    keep = _referenced_paths()
    total_mb = sum(os.path.getsize(c) for c in clips) / (1024 * 1024)

    if keep is None:                      # unreadable queue → keep everything
        print(f"[prune] keeping all {len(clips)} clip(s), {total_mb:.0f} MB",
              flush=True)
        return 0

    doomed = [c for c in clips if os.path.abspath(c) not in keep]
    freed = sum(os.path.getsize(c) for c in doomed) / (1024 * 1024)

    print(f"[prune] {len(clips)} clip(s) on disk ({total_mb:.0f} MB); "
          f"{len(keep)} referenced by an upload queue; "
          f"{len(doomed)} unreferenced ({freed:.0f} MB)", flush=True)

    if args.dry_run:
        for c in doomed[:20]:
            print(f"[prune]   would delete {os.path.relpath(c, REPO)}", flush=True)
        return 0

    removed = 0
    for c in doomed:
        try:
            os.remove(c)
            removed += 1
        except OSError as exc:
            print(f"[prune] ⚠ could not delete {c}: {exc}", flush=True)

    # Drop the now-empty video_*/ and campaign_*/ shells so the cache does not
    # carry thousands of empty directories.
    for pattern in (os.path.join(REPO, "output", "campaign_*", "video_*"),
                    os.path.join(REPO, "output", "campaign_*")):
        for d in sorted(glob.glob(pattern), reverse=True):
            if os.path.isdir(d) and not os.listdir(d):
                try:
                    os.rmdir(d)
                except OSError:
                    pass

    print(f"[prune] ✅ removed {removed} unreferenced clip(s), freed {freed:.0f} MB "
          f"— the uploaded artifact keeps the durable copy", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
