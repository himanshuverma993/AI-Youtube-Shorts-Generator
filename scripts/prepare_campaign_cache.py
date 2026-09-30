"""Create absent campaign ledgers before saving the Actions cache.

Run AFTER restore: never overwrite restored state. No source media or model
weights are manufactured just to make a cache save appear successful.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "campaign"


def prepare(root=ROOT):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("processed_urls.txt", "failed_urls.txt"):
        path = root / name
        if not path.exists():
            path.touch()
    defaults = {
        "posting_registry.json": {"posts": []},
        "feedback_stats.json": {"measured_posts": 0, "posts": []},
        "upload_queue.json": {"items": []},
        "upload_ledger.json": {"days": {}},
        "ig_upload_queue.json": {"items": []},
        "ig_token.json": {},
    }
    for name, value in defaults.items():
        path = root / name
        if not path.exists():
            # x mode: another writer cannot be overwritten accidentally.
            with path.open("x", encoding="utf-8") as file:
                json.dump(value, file)
                file.write("\n")


if __name__ == "__main__":
    prepare()
    print("[cache] campaign state paths ready (restored files preserved)")
