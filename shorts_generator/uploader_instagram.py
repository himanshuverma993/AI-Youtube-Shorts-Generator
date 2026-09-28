"""Instagram Reels auto-upload — Phase 4 (Fix 4B).

Posts rendered clips to Instagram Reels via the Graph API Content Publishing
flow (container → processing poll → publish). Same $0 discipline as YouTube:

  * OPT-IN — IG_UPLOAD_ENABLED=false by default; everything below inert.
  * TOKEN ROLLING — seed a 60-day long-lived user token as IG_ACCESS_TOKEN;
    the runner refreshes it via the fb_exchange_token grant and stores the
    roll-forward token in campaign/ig_token.json (workflow cache persists),
    so the token effectively never expires while runs stay weekly-or-better.
  * PUBLIC-URL HOSTING — IG's container API needs a public media URL; clips
    are staged as a GitHub Release asset (tag from IG_UPLOAD_STAGING_TAG) in
    this repo — requires the repo to be PUBLIC — uploaded with the built-in
    GITHUB_TOKEN, and deleted after a successful publish (or on strike-out).
  * SAME RITUALS — score-ranked queue, 3-strike drops, auto-register into
    posting_registry.json (platform=instagram) so Phase-2/3B feedback sees it.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .config import (
    IG_ACCESS_TOKEN,
    IG_APP_ID,
    IG_APP_SECRET,
    IG_CONTAINER_POLL_SECONDS,
    IG_CONTAINER_TIMEOUT_SECONDS,
    IG_MAX_ATTEMPTS,
    IG_UPLOAD_ENABLED,
    IG_UPLOAD_MAX_PER_RUN,
    IG_UPLOAD_STAGING_TAG,
    IG_USER_ID,
)
from .feedback import register_post

IG_QUEUE_PATH = os.path.join("campaign", "ig_upload_queue.json")
IG_TOKEN_STORE = os.path.join("campaign", "ig_token.json")

_GRAPH = "https://graph.facebook.com/v21.0"
_HTTP_TIMEOUT = 60


def ig_uploads_configured() -> bool:
    """Opt-in + (seed token or a cached rolled token) + IG user id."""
    return bool(IG_UPLOAD_ENABLED and IG_USER_ID and (IG_ACCESS_TOKEN or _stored_token()))


# ---------------------------------------------------------------------------
# Token management (roll-forward long-lived user tokens)
# ---------------------------------------------------------------------------
def _stored_token(store_path: Optional[str] = IG_TOKEN_STORE) -> Optional[str]:
    if not store_path:
        return None
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("token")
        if token:
            return token
    except (OSError, ValueError):
        pass
    return None


def _save_token(token: str, store_path: Optional[str] = IG_TOKEN_STORE) -> None:
    if not store_path:
        return
    os.makedirs(os.path.dirname(store_path) or ".", exist_ok=True)
    with open(store_path, "w", encoding="utf-8") as f:
        json.dump({"token": token, "saved_epoch": time.time(),
                   "saved_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}, f)


def ig_access_token(store_path: str = IG_TOKEN_STORE) -> str:
    """Best available token: rolled store first, env seed as fallback."""
    token = _stored_token(store_path) or IG_ACCESS_TOKEN
    if not token:
        raise RuntimeError("No Instagram token — seed IG_ACCESS_TOKEN (60-day long-lived user token)")
    return token


def roll_token_forward(store_path: str = IG_TOKEN_STORE) -> Optional[str]:
    """Exchange current long-lived token for a fresh 60-day one, best-effort.
    Returns the new token or None (caller keeps using the old one)."""
    token = _stored_token(store_path)
    if token:
        try:
            saved = json.load(open(store_path, encoding="utf-8"))
            if time.time() - float(saved.get("saved_epoch", 0)) < 30 * 86400:
                return token  # fresh enough — don't churn exchanges
        except Exception:
            pass
    token = token or IG_ACCESS_TOKEN
    if not token:
        return None
    if not (IG_APP_ID and IG_APP_SECRET):
        # fb_exchange_token requires the app's credentials; without them the
        # seed token just runs out its 60 days (documented re-seed ritual).
        return token
    try:
        data = _graph_json("GET", "/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": IG_APP_ID,
            "client_secret": IG_APP_SECRET,
            "fb_exchange_token": token,
        })
        new_token = data.get("access_token")
        if new_token:
            _save_token(new_token, store_path)
            print("[ig] token rolled forward (60-day)", flush=True)
            return new_token
    except Exception as e:
        print(f"[ig] token roll skipped ({e}); continuing with current token", flush=True)
    return token


# ---------------------------------------------------------------------------
# stdlib HTTP helpers (Graph + GitHub) — split out for test patching
# ---------------------------------------------------------------------------
def _http_json(url: str, method: str = "GET", params: Optional[Dict] = None,
               headers: Optional[Dict] = None, body: bytes = b"") -> Dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method=method, data=body or None, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} for {url}: {payload}") from e


def _graph_json(method: str, path: str, params: Optional[Dict] = None,
                token: Optional[str] = None, headers: Optional[Dict] = None,
                post_fields: Optional[Dict] = None) -> Dict:
    hdrs = dict(headers or {})
    body = b""
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if post_fields is not None:
        body = urllib.parse.urlencode(post_fields).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    return _http_json(f"{_GRAPH}{path}", method=method, params=params, headers=hdrs, body=body)


# ---------------------------------------------------------------------------
# Public-URL staging via a GitHub Release (repo must be PUBLIC)
# ---------------------------------------------------------------------------
def _gh_headers() -> Dict:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN unavailable — IG staging needs it (workflow injects it)")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY not set (IG staging only runs in Actions)")
    return repo


def _staging_release() -> Dict:
    """Get (or create) the rolling staging release."""
    repo, tag = _repo(), IG_UPLOAD_STAGING_TAG
    base = f"https://api.github.com/repos/{repo}"
    try:
        return _http_json(f"{base}/releases/tags/{urllib.parse.quote(tag)}", headers=_gh_headers())
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    return _http_json(f"{base}/releases", method="POST", headers=_gh_headers(), body=json.dumps({
        "tag_name": tag, "name": "IG media staging (auto)",
        "prerelease": True, "draft": False,
        "generate_release_notes": False,
    }).encode("utf-8"))


def stage_clip_public(clip_path: str) -> Dict:
    """Upload clip as a release asset; returns {url, asset_id, release}."""
    release = _staging_release()
    name = os.path.basename(clip_path)
    upload_url = release["upload_url"].split("{")[0]
    with open(clip_path, "rb") as fh:
        data = fh.read()
    asset = _http_json(
        upload_url,
        method="POST",
        params={"name": name},
        headers={**_gh_headers(), "Content-Type": "video/mp4"},
        body=data,
    )
    url = asset.get("browser_download_url")
    if not url:
        raise RuntimeError(f"asset upload returned no browser_download_url: {asset}")
    return {"url": url, "asset_id": asset["id"], "release": release}


def _delete_asset(asset_id: int, release: Dict) -> None:
    repo = _repo()
    _http_json(f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}",
               method="DELETE", headers=_gh_headers())


# ---------------------------------------------------------------------------
# IG container → publish flow
# ---------------------------------------------------------------------------
def _build_ig_caption(meta: Dict) -> str:
    """Caption + hashtag line, deduped at TAG level (a tag the caption already
    contains — e.g. FTC's #ad #sponsored — is never double-printed)."""
    caption = (meta.get("caption") or "").strip()
    present = set(caption.split())
    missing = [t for t in (meta.get("hashtags") or []) if str(t).strip() and t not in present]
    if missing:
        caption = f"{caption}\n\n{' '.join(missing)}".strip()
    return caption


def _create_container(media_url: str, caption: str, token: str, user_id: str) -> str:
    data = _graph_json("POST", f"/{user_id}/media", token=token, post_fields={
        "media_type": "REELS",
        "video_url": media_url,
        "caption": caption,
        "share_to_feed": "true",
    })
    cid = data.get("id")
    if not cid:
        raise RuntimeError(f"container create returned no id: {data}")
    return cid


def _wait_container_ready(creation_id: str, token: str) -> None:
    deadline = time.time() + IG_CONTAINER_TIMEOUT_SECONDS
    while time.time() < deadline:
        data = _graph_json("GET", f"/{creation_id}", params={"fields": "status_code"}, token=token)
        status = data.get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container {creation_id} reported {status}")
        time.sleep(IG_CONTAINER_POLL_SECONDS)
    raise RuntimeError(f"IG container {creation_id} not ready after {IG_CONTAINER_TIMEOUT_SECONDS}s")


def _publish_container(creation_id: str, token: str, user_id: str) -> str:
    data = _graph_json("POST", f"/{user_id}/media_publish", token=token,
                       post_fields={"creation_id": creation_id})
    media_id = data.get("id")
    if not media_id:
        raise RuntimeError(f"publish returned no media id: {data}")
    return media_id


def upload_reel(clip_path: str, meta: Dict, token_store: str = IG_TOKEN_STORE) -> str:
    """One full IG round-trip. Returns the IG media id. Asset is cleaned up
    afterwards regardless (success or the caller's exception path)."""
    if not os.path.exists(clip_path):
        raise RuntimeError(f"clip file missing: {clip_path}")
    token = ig_access_token(token_store)
    caption = _build_ig_caption(meta)
    staged = stage_clip_public(clip_path)
    try:
        cid = _create_container(staged["url"], caption, token, IG_USER_ID)
        _wait_container_ready(cid, token)
        media_id = _publish_container(cid, token, IG_USER_ID)
        print(f"[ig] ✅ https://instagram.com/reel/{media_id}", flush=True)
        return media_id
    finally:
        try:
            _delete_asset(staged["asset_id"], staged["release"])
        except Exception as e:
            print(f"[ig] ⚠ staging asset cleanup skipped ({e})", flush=True)


# ---------------------------------------------------------------------------
# Queue (same rituals as the YouTube queue)
# ---------------------------------------------------------------------------
def _load_queue(path: str = IG_QUEUE_PATH) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"items": []}


def _save_queue(queue: Dict, path: str = IG_QUEUE_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=1)


def enqueue_ig_uploads(shorts: List[Dict], source_url: str = "", path: str = IG_QUEUE_PATH) -> int:
    """Queue the clips' INSTAGRAM payloads when IG upload is enabled."""
    queue = _load_queue(path)
    known = {it.get("clip_path") for it in queue["items"]}
    added = 0
    for short in sorted(shorts, key=lambda s: int(s.get("score", 0)), reverse=True):
        clip_path = short.get("clip_url")
        ig = (short.get("metadata") or {}).get("instagram") or {}
        if not clip_path or clip_path in known:
            continue
        queue["items"].append({
            "clip_path": clip_path,
            "instagram": {"caption": ig.get("caption", ""), "hashtags": list(ig.get("hashtags") or [])},
            "hook_line": short.get("hook_sentence", ""),
            "score": int(short.get("score", 0)),
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
        print(f"[ig] queued {added} reel(s) for auto-upload", flush=True)
    return added


def process_ig_upload_queue(queue_path: str = IG_QUEUE_PATH,
                            registry_path=os.path.join("campaign", "posting_registry.json"),
                            token_store: str = IG_TOKEN_STORE) -> Dict:
    """Drain the IG queue within per-run budget; register + clean up successes."""
    result = {"uploaded": 0, "queued": 0, "failed_attempts": 0}
    if not ig_uploads_configured():
        return result

    queue = _load_queue(queue_path)
    budget = IG_UPLOAD_MAX_PER_RUN
    remaining: List[Dict] = []
    for item in sorted(queue.get("items", []), key=lambda i: int(i.get("score", 0)), reverse=True):
        if budget <= 0:
            remaining.append(item)
            continue
        try:
            media_id = upload_reel(item["clip_path"], item.get("instagram") or {}, token_store=token_store)
            register_post(
                platform="instagram",
                video_id=str(media_id),
                caption=(item.get("instagram") or {}).get("caption", ""),
                hook_line=item.get("hook_line", ""),
                hashtags=(item.get("instagram") or {}).get("hashtags"),
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
            budget -= 1
            result["uploaded"] += 1
        except Exception as e:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            result["failed_attempts"] += 1
            if item["attempts"] >= IG_MAX_ATTEMPTS:
                print(f"[ig] ❌ {os.path.basename(item.get('clip_path','?'))} failed "
                      f"{item['attempts']}× (last: {e}) — struck out", flush=True)
            else:
                print(f"[ig] ⚠ {os.path.basename(item.get('clip_path','?'))} failed ({e}); retry next run", flush=True)
                remaining.append(item)

    queue["items"] = remaining
    result["queued"] = len(remaining)
    _save_queue(queue, queue_path)
    return result
