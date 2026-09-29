"""Exponential-backoff HTTP transport for every external API call.

WHY THIS MODULE EXISTS
----------------------
Both uploaders and the feedback loop talked to Google / Facebook through a
bare ``urllib.request.urlopen`` with a timeout and **no retry at all**:

  * ``uploader_youtube._http_request``  — one shot, only ``HTTPError`` caught
  * ``uploader_instagram._http_json``   — one shot, only ``HTTPError`` caught
  * ``feedback._http_json``             — one shot, **nothing** caught

The YouTube Data API, the YouTube Analytics API, Google's OAuth token
endpoint and the Facebook Graph API all return 500/502/503/504 routinely
under load, and a datacenter runner sees connection resets and DNS blips on
top of that. With no retry layer, a single transient blip propagated all the
way up to the queue loop, which counted it as an **upload failure** and spent
one of the item's three strikes. Three unlucky ticks and a perfectly good
clip was struck out of the queue and never posted.

WHAT THIS GIVES
---------------
``request_with_retry`` retries on exactly the conditions that are worth
retrying and on nothing else:

  RETRY   408 Request Timeout, 429 Too Many Requests, 5xx server errors,
          plus transport-level ``URLError`` / ``socket.timeout`` /
          ``ssl.SSLError`` / ``ConnectionError``
  RAISE   every other 4xx (401 bad token, 403 quota exceeded, 400 bad
          request) — retrying those just burns time and quota

Backoff is exponential with full jitter, honours a server ``Retry-After``
header when present (clamped — a server asking us to sleep for a day must
not hang a 4-hour job), and is bounded by BOTH an attempt count and a total
wall-clock budget.
"""
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

__all__ = [
    "request_with_retry",
    "RetryPolicy",
    "HttpResult",
    "RETRYABLE_STATUS",
]

# Statuses worth trying again. 408/429 are explicit "come back later"
# signals; 5xx means the far side broke, not us.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 509})

# Transport-level failures that mean "the network had a bad moment".
# NOTE: urllib.error.HTTPError is a *subclass* of URLError, so it must be
# screened out before treating a URLError as a transport failure.
_TRANSPORT_ERRORS: Tuple[type, ...] = (
    urllib.error.URLError,
    socket.timeout,
    ssl.SSLError,
    ConnectionError,       # covers ConnectionReset/Aborted/Refused
    TimeoutError,
)

# A hostile or buggy server can send `Retry-After: 86400`. Sleeping that long
# inside a 240-minute job is indistinguishable from a hang.
MAX_RETRY_AFTER_SECONDS = 120.0


class RetryPolicy:
    """Tunable retry budget. Defaults suit a free-tier API on a CI runner."""

    __slots__ = ("attempts", "base_delay", "max_delay", "total_budget", "timeout")

    def __init__(self, attempts: int = 4, base_delay: float = 2.0,
                 max_delay: float = 60.0, total_budget: float = 300.0,
                 timeout: float = 120.0):
        # A policy of "0 attempts" would silently never issue the request at
        # all, so the floor is 1.
        self.attempts = max(1, int(attempts))
        self.base_delay = max(0.1, float(base_delay))
        self.max_delay = max(self.base_delay, float(max_delay))
        self.total_budget = max(0.0, float(total_budget))
        self.timeout = max(1.0, float(timeout))


DEFAULT_POLICY = RetryPolicy()


class HttpResult:
    """A completed HTTP exchange — status, headers and raw body."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: Dict, body: bytes):
        self.status = int(status)
        self.headers = headers or {}
        self.body = body or b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self, limit: Optional[int] = None) -> str:
        raw = self.body.decode("utf-8", "replace")
        return raw[:limit] if limit else raw


def _header(headers: Dict, name: str) -> Optional[str]:
    """Case-insensitive header lookup (urllib preserves the server's casing)."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return value
    return None


def retry_after_seconds(headers: Dict) -> Optional[float]:
    """Parse and CLAMP a Retry-After header (seconds form only)."""
    raw = _header(headers, "Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER_SECONDS))
    except (TypeError, ValueError):
        # The HTTP-date form is legal but rare on these APIs; falling back to
        # our own exponential backoff is strictly safer than mis-parsing it.
        return None


def _sleep_for(attempt: int, policy: RetryPolicy, hinted: Optional[float]) -> float:
    """Full-jitter exponential backoff, or the server's hint when given."""
    if hinted is not None:
        return hinted + random.uniform(0.0, 1.0)
    ceiling = min(policy.base_delay * (2 ** (attempt - 1)), policy.max_delay)
    return random.uniform(policy.base_delay * 0.5, ceiling)


def _redact(url: str) -> str:
    """Strip the query string before a URL ever reaches a log line.

    The Facebook ``fb_exchange_token`` grant and Google's OAuth calls carry
    ``client_secret`` / refresh tokens as query parameters, and these logs are
    public on a public repository.
    """
    return url.split("?", 1)[0]


def request_with_retry(
    url: str,
    method: str = "GET",
    *,
    headers: Optional[Dict] = None,
    body: bytes = b"",
    form: Optional[Dict] = None,
    params: Optional[Dict] = None,
    policy: RetryPolicy = DEFAULT_POLICY,
    label: str = "",
    opener=None,
) -> HttpResult:
    """Perform an HTTP request, retrying transient failures with backoff.

    Returns an :class:`HttpResult` for any response the server actually sent,
    including non-retryable 4xx — callers decide what a given status means.
    Raises the last transport exception only when every attempt failed to get
    a response at all.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    hdrs = dict(headers or {})
    data = body
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")

    tag = label or f"{method} {_redact(url)}"
    started = time.monotonic()
    last_exc: Optional[BaseException] = None
    last_result: Optional[HttpResult] = None

    for attempt in range(1, policy.attempts + 1):
        hinted: Optional[float] = None
        try:
            req = urllib.request.Request(url, method=method,
                                         data=data or None, headers=hdrs)
            _open = opener or urllib.request.urlopen
            with _open(req, timeout=policy.timeout) as resp:
                result = HttpResult(resp.status, dict(resp.headers), resp.read())
            if result.status not in RETRYABLE_STATUS:
                return result                      # success or a hard 4xx
            last_result = result
            hinted = retry_after_seconds(result.headers)
            reason = f"HTTP {result.status}"
        except urllib.error.HTTPError as e:
            # An error response IS a response — read it, then decide.
            try:
                payload = e.read()
            except Exception:
                payload = b""
            result = HttpResult(e.code, dict(e.headers or {}), payload)
            if e.code not in RETRYABLE_STATUS:
                return result                      # 400/401/403 — do not retry
            last_result = result
            hinted = retry_after_seconds(result.headers)
            reason = f"HTTP {e.code}"
        except _TRANSPORT_ERRORS as e:
            last_exc = e
            reason = f"{type(e).__name__}: {e}"

        if attempt >= policy.attempts:
            break
        delay = _sleep_for(attempt, policy, hinted)
        elapsed = time.monotonic() - started
        if elapsed + delay > policy.total_budget:
            print(f"[http] {tag}: {reason} — retry budget "
                  f"({policy.total_budget:.0f}s) exhausted, giving up", flush=True)
            break
        print(f"[http] {tag}: {reason} (attempt {attempt}/{policy.attempts}) — "
              f"retrying in {delay:.1f}s", flush=True)
        time.sleep(delay)

    if last_result is not None:
        return last_result          # exhausted retries on a 5xx/429
    raise RuntimeError(
        f"{tag} failed after {policy.attempts} attempt(s): {last_exc}"
    ) from last_exc


def json_request(
    url: str,
    method: str = "GET",
    *,
    headers: Optional[Dict] = None,
    body: bytes = b"",
    form: Optional[Dict] = None,
    params: Optional[Dict] = None,
    policy: RetryPolicy = DEFAULT_POLICY,
    label: str = "",
) -> Dict:
    """``request_with_retry`` + JSON decode, raising on any non-2xx.

    The error message deliberately uses the REDACTED url: these messages end
    up in public CI logs and the query string can hold a client_secret.
    """
    result = request_with_retry(url, method, headers=headers, body=body,
                                form=form, params=params, policy=policy,
                                label=label)
    if not result.ok:
        raise RuntimeError(
            f"HTTP {result.status} for {_redact(url)}: {result.text(500)}"
        )
    raw = result.text().strip()
    if not raw:
        return {}
    import json as _json
    return _json.loads(raw)
