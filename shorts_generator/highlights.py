"""Find the most viral-worthy highlights in a transcript.

Logic ported from ViralVadoo's transcript_analysis/highlight_generator.py:
  - content-type / density detection
  - chunking for long videos with overlap
  - virality-criteria prompt
  - score-based dedupe with overlap suppression

The LLM call is pluggable via the `llm_fn` argument; it defaults to
:func:`groq_client.call_llm` — Groq's hosted Llama with an automatic
Cerebras failover, so a down free tier never stops the run.
"""
import json
import re
from typing import Callable, Dict, List, Optional

from .groq_client import call_llm


LLMFn = Callable[[str], str]


CONTENT_TYPE_PROMPT = """Analyze this video transcript sample and classify the content type.
Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.
Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).
Respond with JSON only: {"content_type": "...", "density": "..."}"""


VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by 2026 algorithm impact):
1. SHARE-TO-DM TRIGGER (top-weighted Instagram Reels signal) — humor, relatable banter, or shocking facts with high emotional polarity; the moments a viewer immediately sends to a friend via DM ("this is literally you")
2. INSTANT HOOK — an opening frame that is already an emotional peak, conflict, punchline, or question (beats the "Viewed vs Swiped Away" filter)
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""


HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who has studied thousands of viral clips and reverse-engineered the 2026 YouTube Shorts and Instagram Reels ranking algorithms. You know exactly how retention ("Viewed vs Swiped Away"), completion rate (APV), and DM-shares are weighted.

{virality_criteria}

Content type: {content_type} | Density: {density}

Your task: identify the most viral-worthy highlights from the transcript.

2026 RANKING RULES (MANDATORY — highlights that violate them MUST be rejected):
- 3-SECOND HOOK RULE: reject any highlight whose first 3 seconds contain silence, intro chatter, or slow build-up. The first frame MUST open on an immediate emotional peak, conflict, punchline, or question. Clips that fail the "Viewed vs Swiped Away" filter get zero distribution, no matter how good the rest is.
- HIGH-SHARE FACTOR (Meta DM trigger): prioritize segments with high emotional polarity — humor, relatable banter, shocking facts — that viewers are mathematically likely to share via direct message. DM-shares are the single most valuable ranking boost.
- DURATION LOCK: every highlight MUST be strictly between 20 and 40 seconds (end_time − start_time ≥ 20.0 and ≤ 40.0). Do not return anything shorter or longer. This window maximizes completion rate (>90% APV), which the algorithm weights above raw watch time.
- Never cut mid-sentence or mid-thought — each clip must feel complete and self-contained
- Clips must not overlap significantly with each other
- Score 0-100 on viral potential (not general quality)
- {num_clips_instruction}
- For each highlight, identify the single best "hook_sentence" — the opening line that would make someone stop scrolling
- Explain in one sentence why this clip is viral ("virality_reason")

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"score":int,"hook_sentence":"string","virality_reason":"string"}}]}}"""


CHUNK_SIZE_SECONDS = 1200       # 20-min chunks for long videos
LONG_VIDEO_THRESHOLD = 1800     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = 60
MAX_HIGHLIGHT_API_ATTEMPTS = 3

# 2026 DURATION LOCK (see HIGHLIGHT_SYSTEM_PROMPT): clips must be 20–40s.
# The prompt instructs the model, and _sanitize_highlights ENFORCES it in
# code — a clip outside the window is rejected (0.25s tolerance absorbs
# transcript timestamp jitter around the edges).
MIN_CLIP_SECONDS = 20.0
MAX_CLIP_SECONDS = 40.0
DURATION_TOLERANCE = 0.25


def _parse_json_loose(raw: str) -> Dict:
    """Models sometimes wrap JSON in markdown fences — strip and parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _sanitize_highlights(raw_highlights: object, duration: float) -> List[Dict]:
    """Normalize model output into the expected shape; skip invalid entries.

    Enforces the 2026 duration lock in code: a candidate whose (clamped)
    length falls outside 20–40s (±0.25s jitter tolerance) is dropped, so a
    non-compliant model can't slip long clips through the pipeline.
    """
    if not isinstance(raw_highlights, list):
        return []

    max_end = duration if duration > 0 else float("inf")
    cleaned: List[Dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue

        start = _coerce_float(item.get("start_time"), default=-1.0)
        end = _coerce_float(item.get("end_time"), default=-1.0)
        if start < 0 or end <= start:
            continue

        if max_end != float("inf"):
            start = min(start, max_end)
            end = min(end, max_end)
            if end <= start:
                continue

        clip_dur = end - start
        if clip_dur < MIN_CLIP_SECONDS - DURATION_TOLERANCE:
            continue  # too short — violates the 20s floor
        if clip_dur > MAX_CLIP_SECONDS + DURATION_TOLERANCE:
            continue  # too long — violates the 40s ceiling

        cleaned.append(
            {
                "title": str(item.get("title") or "Untitled Highlight").strip(),
                "start_time": start,
                "end_time": end,
                "score": max(0, min(100, _coerce_int(item.get("score"), default=0))),
                "hook_sentence": str(item.get("hook_sentence") or "").strip(),
                "virality_reason": str(item.get("virality_reason") or "").strip(),
            }
        )

    return cleaned


def detect_content_type(transcript: Dict, llm_fn: LLMFn = call_llm) -> Dict[str, str]:
    segments = transcript.get("segments", [])
    sample = " ".join(s["text"] for s in segments[:25])[:3000]
    prompt = f"{CONTENT_TYPE_PROMPT}\n\nTranscript sample:\n{sample}"
    try:
        raw = llm_fn(prompt)
        return _parse_json_loose(raw)
    except Exception:
        return {"content_type": "other", "density": "medium"}


def build_transcript_text(transcript: Dict) -> str:
    segments = transcript.get("segments", [])
    return "\n".join(f"[{s['start']:.1f}s] {s['text'].strip()}" for s in segments)


def chunk_transcript(transcript: Dict) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration", segments[-1]["end"] if segments else 0)
    chunks = []
    start = 0
    while start < duration:
        end = min(start + CHUNK_SIZE_SECONDS, duration)
        chunk_segs = [
            s for s in segments
            if s["start"] >= start and s["end"] <= end + CHUNK_OVERLAP_SECONDS
        ]
        if chunk_segs:
            chunk = dict(transcript)
            chunk["segments"] = chunk_segs
            chunk["duration"] = end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        start += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS
    return chunks


def call_highlight_api(
    transcript_text: str,
    content_info: Dict,
    duration: float,
    num_clips: int,
    is_chunk: bool = False,
    llm_fn: LLMFn = call_llm,
    context_block: Optional[str] = None,
) -> Dict:
    # Ask for ~2× the user's target so dedupe has headroom, but cap so the model
    # doesn't have to generate a huge JSON payload (which risks timeouts).
    # Headroom estimate uses 40s — the ceiling of the 20–40s duration lock.
    target = max(num_clips * 2, 5)
    natural_max = max(2 if is_chunk else 3, int(duration / 40))
    # Never demand more non-overlapping 20s clips than can physically exist in
    # this window — e.g. a 45s source can hold at most 2, not "at least 3".
    physical_max = max(1, int(duration // MIN_CLIP_SECONDS))
    min_clips = min(target, natural_max, physical_max, 8)
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        virality_criteria=VIRALITY_CRITERIA,
        content_type=content_info.get("content_type", "other"),
        density=content_info.get("density", "medium"),
        num_clips_instruction=f"Generate at least {min_clips} highlights",
    )
    # Optional Phase-1 Fix 2 trend block: framing bias only, injected between
    # the rules and the transcript — NEVER touching the transcript itself.
    trend_block = f"{context_block.strip()}\n\n" if context_block and context_block.strip() else ""
    base_prompt = f"{system}\n\n{trend_block}Transcript:\n{transcript_text}"
    prompt = base_prompt
    last_error = "unknown"

    for attempt in range(1, MAX_HIGHLIGHT_API_ATTEMPTS + 1):
        raw = llm_fn(prompt)
        try:
            parsed = _parse_json_loose(raw)
            highlights = _sanitize_highlights(parsed.get("highlights"), duration=duration)
            if highlights:
                return {"highlights": highlights}
            last_error = "no valid highlights in response"
        except Exception as e:
            last_error = str(e)

        if attempt < MAX_HIGHLIGHT_API_ATTEMPTS:
            print(
                f"[highlights] invalid model output on attempt {attempt}/{MAX_HIGHLIGHT_API_ATTEMPTS}; retrying",
                flush=True,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights' array."
                + " Each item must include: title, start_time, end_time, score, hook_sentence, virality_reason."
                + " No markdown fences, no commentary."
            )

    raise RuntimeError(
        f"Highlight generator produced invalid output after {MAX_HIGHLIGHT_API_ATTEMPTS} attempts: {last_error}"
    )


def dedupe_highlights(highlights: List[Dict]) -> List[Dict]:
    """Drop a highlight if it overlaps >50% with a higher-scoring one already kept."""
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)
    kept: List[Dict] = []
    for h in highlights:
        h_start = float(h["start_time"])
        h_end = float(h["end_time"])
        h_dur = h_end - h_start
        overlapping = False
        for k in kept:
            latest_start = max(h_start, float(k["start_time"]))
            earliest_end = min(h_end, float(k["end_time"]))
            overlap = earliest_end - latest_start
            if overlap > 0 and overlap > 0.5 * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(h)
    return kept


def get_highlights(
    transcript: Dict,
    num_clips: int = 3,
    llm_fn: Optional[LLMFn] = None,
    context_block: Optional[str] = None,
) -> Dict:
    """Main entry point — returns {highlights: [...]} sorted by score.

    `llm_fn` swaps the underlying LLM. Defaults to :func:`call_llm`
    (Groq with automatic Cerebras failover); pass a different callable to
    use another backend entirely. `context_block` (optional prebuilt block
    from trends.get_trend_block) is injected as framing-bias-only guidance.
    """
    llm_fn = llm_fn or call_llm
    duration = transcript.get("duration", 0)
    content_info = detect_content_type(transcript, llm_fn=llm_fn)
    print(f"[highlights] content={content_info.get('content_type')} density={content_info.get('density')} duration={duration:.0f}s", flush=True)

    if duration >= LONG_VIDEO_THRESHOLD:
        chunks = chunk_transcript(transcript)
        print(f"[highlights] long video — splitting into {len(chunks)} chunks", flush=True)
        all_highlights: List[Dict] = []
        for i, chunk in enumerate(chunks):
            offset = chunk.get("_offset", 0)
            # Show the model CHUNK-RELATIVE timestamps. The segment times in
            # `chunks` are absolute (t+offset in the source), but the duration
            # window _sanitize_highlights enforces is chunk-relative — feeding
            # absolute times made every non-first chunk clamp to end<=start
            # and get DROPPED (audit F3: only chunk-1 clips ever survived on
            # videos longer than LONG_VIDEO_THRESHOLD).
            rel_chunk = {
                **chunk,
                "segments": [
                    {**s, "start": float(s["start"]) - offset, "end": float(s["end"]) - offset}
                    for s in chunk["segments"]
                ],
            }
            text = build_transcript_text(rel_chunk)
            print(f"[highlights] chunk {i + 1}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            result = call_highlight_api(text, content_info, chunk["duration"], num_clips=num_clips, is_chunk=True, llm_fn=llm_fn, context_block=context_block)
            for h in result.get("highlights", []):
                h["start_time"] = float(h["start_time"]) + offset
                h["end_time"] = float(h["end_time"]) + offset
                all_highlights.append(h)
        highlights = dedupe_highlights(all_highlights)
    else:
        text = build_transcript_text(transcript)
        result = call_highlight_api(text, content_info, duration, num_clips=num_clips, llm_fn=llm_fn, context_block=context_block)
        highlights = dedupe_highlights(result.get("highlights", []))

    return {"highlights": highlights}
