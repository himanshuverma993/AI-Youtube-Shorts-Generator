"""Per-clip platform-split upload metadata with enforced FTC disclosure.

PHASE-1 FIX 1: every clip gets TWO tuned payloads from ONE LLM call —
  * youtube   → 40–60 char title (curiosity gap front-loaded), description whose
                sentence #1 mirrors the clip's spoken keywords (search indexing),
                3–5 high-intent niche hashtags.
  * instagram → caption whose FIRST line (what survives the feed fold) is a
                ≤60-char hook — enforced in code, not trusted to the model —
                followed by a keyword-woven support line + CTA, 3–5 relevant
                hashtags (IG search indexes caption keywords in 2026).

FTC COMPLIANCE: both platform payloads are post-processed by
``_enforce_disclosure`` — "#ad #sponsored" is appended in code, never left to
the model, on every path including fallbacks.
"""
import json
from typing import Callable, Dict, List, Optional

from .config import FTC_DISCLOSURE_TAGS
from .groq_client import call_llm
from .highlights import _parse_json_loose

LLMFn = Callable[[str], str]

IG_HOOK_LINE_MAX_CHARS = 60   # the feed-fold line — hard-capped in code

METADATA_PROMPT = """You are a YouTube Shorts AND Instagram Reels SEO strategist writing upload metadata tuned for the 2026 ranking algorithms — keyword indexing drives discovery, retention drives distribution. You tune each platform differently because they rank differently.

For EACH clip below, write metadata for BOTH platforms following these MANDATORY rules:

YOUTUBE RULES:
1. NLP & SEARCH INDEXING: the FIRST sentence of "description" must read as a natural-language search query that directly mirrors the core keywords spoken in the clip's audio (see "spoken_excerpt") — exactly the way a viewer would type it into YouTube search. Follow it with ONE short supporting sentence. Never "In this clip" or generic filler.
2. TITLE HOOK: "title" must be 40–60 characters (count them), curiosity gap FRONT-LOADED — payoff keyword or tension in the first 5 words. Honest but impossible to scroll past; no clickbait lies, no ALL-CAPS.
3. "hashtags": 3–5 HIGH-INTENT NICHE tags — never generic reach-bait (#viral, #fyp, #shorts) and NEVER #ad/#sponsored (disclosure tags are appended automatically after you respond).

INSTAGRAM REELS RULES:
4. FOLD-SURVIVAL: only the caption's first line is visible in the feed before "…more". "caption"'s FIRST line must therefore be a self-contained hook of AT MOST 60 characters — a question, shock, or tension that forces the tap. (It will be hard-verified; longer hooks are wasted.)
5. KEYWORD WEAVE: Instagram search indexes caption KEYWORDS, so the second line is 1–2 sentences weaving this clip's niche keywords naturally (mirroring spoken_excerpt), ending with a light CTA where natural ("save this", "send to the friend who needs it"). NO emojis spam — at most 1–2.
6. "hashtags": 3–5 tightly-relevant topical hashtags (Instagram ranking favours relevance over volume in 2026; never generic, never #ad/#sponsored).

Do NOT put hashtags inside "description" or "caption" — they are appended automatically. Both payloads must describe THIS clip honestly; no bait topics not present in audio.

Clips (JSON — "spoken_excerpt" contains the actual spoken words; mine it for keywords):
{clips_json}

Respond ONLY with valid JSON (no markdown, no explanation):
{{"clips":[{{"youtube":{{"title":"string","description":"string","hashtags":["#tag1"]}},"instagram":{{"caption":"string","hashtags":["#tag1"]}}}}]}}

The "clips" array must have exactly {count} entries, in the same order as the input."""


def _enforce_disclosure(text: str) -> str:
    """Idempotently append the FTC disclosure tags to a description/caption.

    Every generated payload passes through here, so "#ad #sponsored" ends up
    on EVERY output — guaranteed in code, not by the model.

    Matching is case-insensitive and ignores trailing punctuation: a model
    that wrote "#Ad" or "...#sponsored." previously failed the exact-string
    membership test, so the tag was appended a SECOND time and the upload
    carried a visibly duplicated disclosure.
    """
    body = (text or "").strip()
    existing = {tok.lower().rstrip(",.;:!?\"'") for tok in body.split()}
    missing = [tag for tag in FTC_DISCLOSURE_TAGS.split()
               if tag.lower() not in existing]
    if not missing:
        return body
    return f"{body} {' '.join(missing)}".strip() if body else " ".join(missing)


def _spoken_excerpt(short: Dict, transcript: Optional[Dict], max_words: int = 50) -> str:
    """Pull the actual words spoken during this clip's time window (rule #1/#5)."""
    if not transcript:
        return ""
    start = float(short.get("start_time", 0.0))
    end = float(short.get("end_time", 0.0))
    words: List[str] = []
    for seg in transcript.get("segments", []):
        if float(seg.get("end", 0.0)) < start or float(seg.get("start", 0.0)) > end:
            continue
        words.extend(str(seg.get("text", "")).split())
        if len(words) >= max_words:
            break
    return " ".join(words[:max_words])


def _clip_brief(short: Dict, index: int, transcript: Optional[Dict] = None) -> Dict:
    duration = float(short.get("end_time", 0.0)) - float(short.get("start_time", 0.0))
    return {
        "index": index,
        "working_title": short.get("title", ""),
        "hook": short.get("hook_sentence", ""),
        "why_viral": short.get("virality_reason", ""),
        "seconds": round(duration, 1),
        "spoken_excerpt": _spoken_excerpt(short, transcript),
    }


def _clip_title(text: str, cap: int = 60) -> str:
    """Hard-cap a title at `cap` chars, trimming to a word boundary."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap].rsplit(" ", 1)[0].rstrip(" ,;:-") or text[:cap]


def _cap_ig_hook_line(caption: str) -> str:
    """Instagram fold-survival enforcer: shorten ONLY the caption's first line
    to ≤60 chars if the model overran, keeping the rest of the caption intact.
    Cut at a word boundary and make the truncation read intentional."""
    lines = (caption or "").splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    if len(first) > IG_HOOK_LINE_MAX_CHARS:
        first = _clip_title(first, cap=IG_HOOK_LINE_MAX_CHARS).rstrip(".") + "…"
        lines[0] = first
    return "\n".join(lines).strip()


def _clean_tags(tags_obj) -> List[str]:
    """Sanitize a hashtag list and guarantee the FTC disclosure tags are in it."""
    tags = [str(t).strip() for t in tags_obj or [] if isinstance(t, str) and str(t).strip()] if isinstance(tags_obj, list) else []
    for tag in FTC_DISCLOSURE_TAGS.split():
        if tag not in tags:
            tags.append(tag)
    return tags


def _fallback_metadata(short: Dict, transcript: Optional[Dict] = None) -> Dict:
    """Deterministic no-LLM metadata for BOTH platforms — still FTC-compliant."""
    title = _clip_title(short.get("title") or "Viral Clip")
    hook = (short.get("hook_sentence") or "").strip()
    youtube = {
        "title": title,
        "description": _enforce_disclosure(f"{title}. {hook}".strip()),
        "hashtags": list(FTC_DISCLOSURE_TAGS.split()),
    }
    instagram = {
        "caption": _enforce_disclosure(_cap_ig_hook_line(f"{hook or title}\n{title}.")),
        "hashtags": list(FTC_DISCLOSURE_TAGS.split()),
    }
    return {"youtube": youtube, "instagram": instagram}


def _build_platform_payloads(item: Dict, short: Dict) -> Dict:
    """Normalize one LLM clip item into the enforced two-platform payload."""
    yt_in = item.get("youtube") if isinstance(item.get("youtube"), dict) else {}
    ig_in = item.get("instagram") if isinstance(item.get("instagram"), dict) else {}

    youtube = {
        "title": _clip_title(str(yt_in.get("title") or short.get("title") or "Viral Clip")),
        "description": _enforce_disclosure(str(yt_in.get("description") or "")),
        "hashtags": _clean_tags(yt_in.get("hashtags")),
    }
    instagram = {
        "caption": _enforce_disclosure(_cap_ig_hook_line(str(ig_in.get("caption") or ""))),
        "hashtags": _clean_tags(ig_in.get("hashtags")),
    }
    return {"youtube": youtube, "instagram": instagram}


def generate_metadata(
    shorts: List[Dict],
    llm_fn: Optional[LLMFn] = None,
    transcript: Optional[Dict] = None,
    context_block: Optional[str] = None,
) -> List[Dict]:
    """Attach platform-split, FTC-compliant ``metadata`` to each short.

    Each short gains:
        short["metadata"] = {
          "youtube":   {"title": str, "description": str, "hashtags": list},
          "instagram": {"caption": str, "hashtags": list},
        }
    both post-processed in code: title ≤60, IG fold-line ≤60, disclosure tags
    appended idempotently. ``context_block`` (optional prebuilt block from
    trends.get_trend_block) is appended to the prompt as style guidance only.
    """
    llm_fn = llm_fn or call_llm

    briefs = [_clip_brief(s, i, transcript) for i, s in enumerate(shorts)]
    prompt = METADATA_PROMPT.format(
        clips_json=json.dumps(briefs, indent=2),
        count=len(briefs),
    )
    if context_block and context_block.strip():
        prompt = f"{prompt}\n\n{context_block.strip()}"

    generated: List[Optional[Dict]] = [None] * len(shorts)
    try:
        raw = llm_fn(prompt)
        parsed = _parse_json_loose(raw)
        items = parsed.get("clips")
        if not isinstance(items, list):
            raise ValueError("response had no 'clips' array")
        for i, item in enumerate(items[: len(shorts)]):
            if isinstance(item, dict):
                generated[i] = item
    except Exception as e:
        print(f"[metadata] LLM generation failed ({e}); using FTC-compliant fallbacks", flush=True)

    out: List[Dict] = []
    for i, short in enumerate(shorts):
        item = generated[i]
        if item is None:
            meta = _fallback_metadata(short, transcript)
        else:
            meta = _build_platform_payloads(item, short)

        out.append({**short, "metadata": meta})
        yt = meta["youtube"]
        ig = meta["instagram"]
        print(f"[metadata] #{i + 1} YT {yt['title']!r} | IG {ig['caption'].splitlines()[0]!r}", flush=True)

    return out
