"""YouTube auto-upload — Phase 3 (Fix 4A).

Turns rendered clips into posted YouTube uploads: resumable-upload REST flow
over plain stdlib HTTP (no heavy google-api client on the runner), wrapped in
a quota-aware queue so the FREE 10,000-units/day budget is never exceeded
(videos.insert = 1,600 units each ⇒ a hard daily ceiling of 6 by default).

DESIGN GUARANTEES:
  * DRY-RUN DEFAULT — UPLOAD_ENABLED=false until the operator flips it;
    privacy defaults to "private" so nothing appears publicly by accident.
  * QUOTA-AWARE — per-run cap (UPLOAD_MAX_PER_RUN) × daily ledger; the rest
    of the clips simply stay queued for the next cron tick.
  * 3-STRIKE ETHOS — like failed URLs, a clip failing UPLOAD_MAX_ATTEMPTS
    upload tries is dropped with a warning instead of silently re-burning
    quota forever.
  * FEEDBACK CLOSURE — every successful upload auto-registers into
    campaign/posting_registry.json, so the Phase-2 feedback loop starts
    measuring it with zero manual work.
  * NEVER BLOCKS GENERATION — an upload failure marks the queue item and the
    campaign run continues; clipping is a separate subsystem.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .config import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    UPLOAD_DAILY_CAP,
    UPLOAD_ENABLED,
    UPLOAD_MAX_ATTEMPTS,
    UPLOAD_MAX_PER_RUN,
    YT_CATEGORY_ID,
    YT_PRIVACY,
    YT_REFRESH_TOKEN,
)
from .feedback import _http_json, _mint_access_token, register_post

QUEUE_PATH = os.path.join("campaign", "upload_queue.json")
LEDGER_PATH = os.path.join("campaign", "upload_ledger.json")

_RESUMABLE_INIT_URL = "https://upload.youtube.com/upload/youtube/v3/videos"
_HTTP_TIMEOUT = 120
_SNIPPET_PARTS = "snippet,status"


def uploads_configured() -> bool:
    """True only when the operator opted in AND the OAuth trio exists."""
    return bool(UPLOAD_ENABLED and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and YT_REFRESH_TOKEN)


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ---------------------------------------------------------------------------
# Daily quota ledger (persisted via workflow cache — 4 runs/day aware)
# ---------------------------------------------------------------------------
def _load_ledger(path: str = LEDGER_PATH) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"days": {}}


def _uploads_done_today(path: str = LEDGER_PATH) -> int:
    return int(_load_ledger(path).get("days", {}).get(_today_utc(), 0))


def _bump_ledger(n: int = 1, path: str = LEDGER_PATH) -> None:
    ledger = _load_ledger(path)
    day = _today_utc()
    ledger["days"][day] = int(ledger["days"].get(day, 0)) + n
    # keep ledger tiny — only recent days matter (quota window is daily)
    for stale in [d for d in ledger["days"] if d < day][: -7]:
        ledger["days"].pop(stale, None)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=1)


def upload_budget_remaining(run_cap: int = UPLOAD_MAX_PER_RUN,
                            daily_cap: int = UPLOAD_DAILY_CAP,
                            ledger_path: str = LEDGER_PATH) -> int:
    """How many uploads are still allowed THIS run (run cap ∩ daily cap)."""
    return max(0, min(run_cap, daily_cap - _uploads_done_today(ledger_path)))


# ---------------------------------------------------------------------------
# Upload queue
# ---------------------------------------------------------------------------
def _load_queue(path: str = QUEUE_PATH) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"items": []}


def _save_queue(queue: Dict, path: str = QUEUE_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=1)


def enqueue_uploads(shorts: List[Dict], source_url: str = "", path: str = QUEUE_PATH) -> int:
    """Append rendered shorts to the upload queue (deduped by clip path,
    ranked best-first by the virality score). Returns how many were added."""
    queue = _load_queue(path)
    known = {it.get("clip_path") for it in queue["items"]}
    added = 0
    for short in sorted(shorts, key=lambda s: int(s.get("score", 0)), reverse=True):
        clip_path = short.get("clip_url")
        yt = (short.get("metadata") or {}).get("youtube") or {}
        if not clip_path or clip_path in known:
            continue
        queue["items"].append({
            "clip_path": clip_path,
            "youtube": {
                "title": yt.get("title", ""),
                "description": yt.get("description", ""),
                "hashtags": list(yt.get("hashtags") or []),
            },
            "score": int(short.get("score", 0)),
            "hook_line": short.get("hook_sentence", ""),
            "source_url": source_url,
            "clip_start": short.get("start_time"),
            "clip_end": short.get("end_time"),
            "attempts": 0,
            "enqueued_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        })
        known.add(clip_path)
        added += 1
    if added:
        _save_queue(queue, path)
        print(f"[upload] queued {added} clip(s) for auto-upload", flush=True)
    return added


# ---------------------------------------------------------------------------
# Resumable-upload REST flow (stdlib only)
# ---------------------------------------------------------------------------
def _http_request(url: str, method: str = "GET", headers: Optional[Dict] = None,
                  body: bytes = b"", form: Optional[Dict] = None,
                  params: Optional[Dict] = None):
    """Split-out transport so tests can patch it. Returns (status, headers, body)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    hdrs = dict(headers or {})
    data = body
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, method=method, data=data or None, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:  # surface API errors as data, not exceptions
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, dict(e.headers or {}), payload


def _initiate_resumable(access_token: str, snippet: Dict, status_body: Dict):
    """POST the metadata; return the upload session Location URI."""
    status, headers, body = _http_request(
        _RESUMABLE_INIT_URL,
        method="POST",
        params={"uploadType": "resumable", "part": _SNIPPET_PARTS},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(os.path.getsize(status_body["_file_path"])),
        },
        body=json.dumps({"snippet": snippet, "status": status_body["_status"]}).encode("utf-8"),
    )
    if status not in (200, 201):
        raise RuntimeError(f"resumable init failed ({status}): {body[:400]!r}")
    location = headers.get("Location") or headers.get("location")
    if not location:
        raise RuntimeError(f"resumable init returned no Location header: {headers}")
    return location


def _put_video_file(session_uri: str, clip_path: str) -> Dict:
    """Single-request PUT of the whole clip (our files are ≤ ~50 MB)."""
    with open(clip_path, "rb") as fh:
        payload = fh.read()
    status, headers, body = _http_request(
        session_uri,
        method="PUT",
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(payload)),
        },
        body=payload,
    )
    if status not in (200, 201):
        raise RuntimeError(f"video upload failed ({status}): {body[:400]!r}")
    return json.loads(body.decode("utf-8"))


def _build_snippet(meta: Dict, category_id: str = YT_CATEGORY_ID) -> Dict:
    """API snippet from our youtube metadata payload — disclosure-safe:
    the description already ends with #ad #sponsored (metadata.py enforces).
    Hashtag line is appended once at TAG level (audit F13: a description
    containing SOME tags must not cause the shared ones to repeat)."""
    description = (meta.get("description") or "").strip()
    present = {tok.rstrip(",.;:!\"'") for tok in description.split()}
    missing = [t for t in (meta.get("hashtags") or [])
               if str(t).strip() and str(t).rstrip(",.;:!\"'") not in present]
    if missing:
        description = f"{description}\n{' '.join(missing)}".strip()
    return {
        "title": (meta.get("title") or "Untitled short").strip()[:100],
        "description": description,
        "tags": [t.lstrip("#") for t in (meta.get("hashtags") or []) if str(t).strip()],
        "categoryId": category_id,
    }


def upload_clip(clip_path: str, meta: Dict, privacy: str = YT_PRIVACY,
                category_id: str = YT_CATEGORY_ID) -> str:
    """One full upload round-trip; returns the new YouTube video id."""
    if not os.path.exists(clip_path):
        raise RuntimeError(f"clip file missing: {clip_path}")
    token = _mint_access_token()
    status_body = {
        "_file_path": clip_path,
        "_status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    session = _initiate_resumable(token, _build_snippet(meta, category_id), status_body)
    resource = _put_video_file(session, clip_path)
    video_id = resource.get("id")
    if not video_id:
        raise RuntimeError(f"upload finished but response had no video id: {resource}")
    return video_id


# ---------------------------------------------------------------------------
# Queue processing (end of campaign run, or standalone queue-only run)
# ---------------------------------------------------------------------------
def process_upload_queue(queue_path: str = QUEUE_PATH,
                         ledger_path: str = LEDGER_PATH,
                         registry_path=os.path.join("campaign", "posting_registry.json")) -> Dict:
    """Upload queued clips within budget; register successes; strike out repeats."""
    result = {"uploaded": 0, "queued": 0, "failed_attempts": 0, "skipped_no_budget": 0,
              "dropped_missing_file": 0}
    if not uploads_configured():
        return result

    queue = _load_queue(queue_path)
    budget = upload_budget_remaining(ledger_path=ledger_path)
    remaining: List[Dict] = []
    for item in sorted(queue.get("items", []), key=lambda i: int(i.get("score", 0)), reverse=True):
        if budget <= 0:
            remaining.append(item)
            result["skipped_no_budget"] += 1
            continue
        # Runner disk is ephemeral (audit F8): a clip queued in an earlier
        # run may be gone — that's not an upload failure, drop without a
        # strike (the workflow cache now persists queued short_*.mp4 files,
        # so this only fires after cache eviction).
        if not os.path.exists(item.get("clip_path") or ""):
            result["dropped_missing_file"] += 1
            print(f"[upload] 🗑 {os.path.basename(item.get('clip_path', '?'))} gone with the runner — dropped, no strike", flush=True)
            continue
        try:
            video_id = upload_clip(item["clip_path"], item.get("youtube") or {})
            register_post(
                platform="youtube",
                video_id=video_id,
                title=(item.get("youtube") or {}).get("title", ""),
                hook_line=item.get("hook_line", ""),
                hashtags=(item.get("youtube") or {}).get("hashtags"),
                duration_seconds=(
                    float(item["clip_end"]) - float(item["clip_start"])
                    if item.get("clip_start") is not None and item.get("clip_end") is not None
                    else None
                ),
                source_url=item.get("source_url", ""),
                clip_start=item.get("clip_start"),
                clip_end=item.get("clip_end"),
                path=registry_path,
            )
            _bump_ledger(1, ledger_path)
            budget -= 1
            result["uploaded"] += 1
            print(f"[upload] ✅ https://youtu.be/{video_id} — \"{(item.get('youtube') or {}).get('title','')[:50]}\"", flush=True)
            time.sleep(1.5)  # gentle spacing on the free API
        except Exception as e:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            result["failed_attempts"] += 1
            if item["attempts"] >= UPLOAD_MAX_ATTEMPTS:
                print(f"[upload] ❌ {os.path.basename(item.get('clip_path','?'))} failed {item['attempts']}× "
                      f"(last: {e}) — struck out of the queue", flush=True)
            else:
                print(f"[upload] ⚠ {os.path.basename(item.get('clip_path','?'))} failed ({e}); "
                      f"will retry next run", flush=True)
                remaining.append(item)

    queue["items"] = remaining
    result["queued"] = len(remaining)
    _save_queue(queue, queue_path)
    return result
