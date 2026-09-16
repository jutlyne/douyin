from __future__ import annotations

from pydantic import BaseModel, Field

from container_long.models import LongDialogueLine
from container_long.text_cleanup import normalize_long_text_value, normalize_long_vi_text


class _LongLine(BaseModel):
    start: float
    end: float
    text_zh: str
    text_vi: str


class _LongChunkSchema(BaseModel):
    lines: list[_LongLine]
    summary_vi: str = ""


class _CompilationMetadataSchema(BaseModel):
    title_vi: str
    description_vi: str
    hashtags: list[str] = Field(default_factory=list)


class _AdSpanSchema(BaseModel):
    start: float
    end: float
    reason_vi: str = ""
    confidence: float = 0.0


class _AdDetectionSchema(BaseModel):
    spans: list[_AdSpanSchema] = Field(default_factory=list)


class _CliffhangerSelectionSchema(BaseModel):
    boundary_index: int
    title_vi: str = ""
    reason_vi: str = ""
    confidence: float = 0.0


def select_cliffhanger_boundary(
    *,
    candidates: list[dict],
    project_id: str,
    region: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 300,
) -> dict:
    """Choose a story-hook boundary from analyzed chunk summaries."""
    import json

    from google.genai import types  # type: ignore

    if not candidates:
        raise ValueError("cliffhanger candidates are required")
    timeline = "\n".join(
        (
            f"boundary_index={int(item['boundary_index'])}; "
            f"source_end={float(item['source_end']):.1f}s; "
            f"summary_vi={str(item.get('summary_vi') or '').strip()}"
        )
        for item in candidates
    )
    prompt = f"""Select the best ending boundary for PART 1 of a Vietnamese
YouTube story video. End at a strong, unresolved cliffhanger that makes viewers
want PART 2: a revelation, imminent danger, confrontation, major decision, new
evidence, reversal, or unanswered question. Do not choose a fully resolved
scene. Judge story strength only; do not prefer any particular timestamp.
Return only one boundary_index from the supplied list, a
short attractive Vietnamese title, a concise Vietnamese reason, and confidence
from 0 to 1.

Candidate boundaries:
{timeline}
"""
    client = _client(project_id, region)
    response = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0.2,
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
            max_output_tokens=4096,
            response_mime_type="application/json",
            response_schema=_CliffhangerSelectionSchema,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = response.parsed
    if parsed is None:
        parsed = _CliffhangerSelectionSchema(**json.loads(response.text))
    return {
        "boundary_index": int(parsed.boundary_index),
        "title_vi": normalize_long_vi_text(parsed.title_vi),
        "reason_vi": normalize_long_vi_text(parsed.reason_vi),
        "confidence": max(0.0, min(1.0, float(parsed.confidence))),
    }


def detect_ad_spans(
    *,
    subtitles: list[dict],
    project_id: str,
    region: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 300,
) -> list[dict]:
    """Flag promotional/advertisement spans in the final subtitle timeline.

    Text-only Gemini call over the assembled Vietnamese subtitle lines. Returns
    a list of ``{start, end, reason_vi, confidence}`` spans (seconds). These are
    only *suggestions*: a human confirms the actual cut via Telegram. Prompt
    building and span normalization live in ``editing`` (pure, unit-tested).
    """
    from google.genai import types  # type: ignore

    from container_long.editing import (
        build_ad_detection_prompt,
        normalize_ad_spans,
    )

    lines = [
        item
        for item in (subtitles or [])
        if str(item.get("text_vi") or "").strip()
    ]
    if not lines:
        return []

    client = _client(project_id, region)
    response = client.models.generate_content(
        model=model,
        contents=[build_ad_detection_prompt(lines)],
        config=types.GenerateContentConfig(
            temperature=0.1,
            # gemini-2.5-pro is a thinking model; bound the thinking budget and
            # leave a large output budget so the JSON spans are never truncated.
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
            max_output_tokens=32768,
            response_mime_type="application/json",
            response_schema=_AdDetectionSchema,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = response.parsed
    if parsed is None:
        import json

        text = (response.text or "").strip()
        if not text:
            return []
        parsed = _AdDetectionSchema(**json.loads(text))

    return normalize_ad_spans([
        {
            "start": span.start,
            "end": span.end,
            "reason_vi": span.reason_vi,
            "confidence": span.confidence,
        }
        for span in parsed.spans
    ])


def detect_video_ad_spans(
    *,
    video_uri: str,
    project_id: str,
    region: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 900,
) -> list[dict]:
    """Inspect the complete audiovisual output for non-story promotions."""
    from google.genai import types  # type: ignore

    from container_long.editing import normalize_ad_spans

    if not str(video_uri or "").startswith("gs://"):
        raise ValueError("video_uri must be a gs:// URI")
    prompt = """Inspect the entire video, both visuals and audio, for inserted
advertisements or promotions unrelated to the story. Flag product or service
promotion, app-download prompts, sales pitches, QR codes, contact details,
channel promotion, follow/subscribe calls-to-action, sponsor segments, and
commercial interstitials.

Rules:
- Do not classify normal story dialogue as advertising merely because it
  mentions money, products, shops, gifts, or services.
- Do not classify a persistent source-platform watermark as an advertisement.
- Include the full contiguous ad span, using seconds from the video start.
- If there is no genuine advertisement, return spans=[].
- reason_vi must briefly explain the promotional evidence in Vietnamese.
"""
    client = _client(project_id, region)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_uri(file_uri=video_uri, mime_type="video/mp4"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=_AdDetectionSchema,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = response.parsed
    if parsed is None:
        import json

        text = (response.text or "").strip()
        if not text:
            return []
        parsed = _AdDetectionSchema(**json.loads(text))
    return normalize_ad_spans([
        {
            "start": span.start,
            "end": span.end,
            "reason_vi": span.reason_vi,
            "confidence": span.confidence,
        }
        for span in parsed.spans
    ])


def _client(project_id: str, region: str):
    from google import genai  # type: ignore

    return genai.Client(vertexai=True, project=project_id, location=region)


def _analyze_with_prompt(
    *,
    clip_uri: str,
    project_id: str,
    region: str,
    prompt: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 900,
) -> tuple[list[LongDialogueLine], str]:
    from google.genai import types  # type: ignore

    client = _client(project_id, region)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_uri(file_uri=clip_uri, mime_type="video/mp4"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
            max_output_tokens=65536,
            response_mime_type="application/json",
            response_schema=_LongChunkSchema,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = response.parsed
    if parsed is None:
        import json

        parsed = _LongChunkSchema(**json.loads(response.text))

    clean: list[LongDialogueLine] = []
    previous_start = -1.0
    for item in parsed.lines:
        start = max(0.0, float(item.start))
        end = float(item.end)
        text_zh = item.text_zh.strip()
        text_vi = normalize_long_vi_text(item.text_vi)
        if not text_vi or end <= start or start < previous_start:
            continue
        clean.append(LongDialogueLine(start, end, text_zh, text_vi))
        previous_start = start
    return clean, normalize_long_vi_text(parsed.summary_vi or "")


def analyze_chunk(
    *,
    clip_uri: str,
    project_id: str,
    region: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 900,
) -> tuple[list[LongDialogueLine], str]:
    """Transcribe, translate and time one source chunk."""
    prompt = """Analyze the full Chinese video clip for Vietnamese dubbing.

Mandatory requirements:
- Extract every spoken Chinese dialogue/narration line in order. Do not summarize dialogue.
- If there are Chinese burned-in subtitles, use them only to verify text; timing must come from the actual audio.
- Each item must have start/end in seconds from the beginning of this clip, original text_zh, and a natural complete Vietnamese text_vi translation.
- Do not merge lines that are separated by pauses or speaker changes.
- Timing must be increasing, end > start, and must not exceed the clip duration.
- summary_vi is a 1-2 sentence Vietnamese summary of the clip.
"""
    return _analyze_with_prompt(
        clip_uri=clip_uri,
        project_id=project_id,
        region=region,
        prompt=prompt,
        model=model,
        timeout=timeout,
    )


def analyze_gap_chunk(
    *,
    clip_uri: str,
    project_id: str,
    region: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 900,
) -> tuple[list[LongDialogueLine], str]:
    """Analyze a short gap retry clip without stretching lines over silence."""
    prompt = """Analyze this short retry clip for missing Chinese dialogue.

Mandatory requirements:
- Return ONLY clearly audible Chinese dialogue/narration from the audio.
- Do not infer dialogue from visuals, do not describe the scene as dialogue, and do not summarize.
- If there is no clearly audible dialogue, return lines=[].
- start/end are seconds from the beginning of this retry clip and must tightly bound the real speech only.
- Never extend end time through silent/action-only parts.
- Split by pauses, punctuation, or speaker changes. Each line is usually 0.3-8 seconds.
- text_zh is the original Chinese; text_vi is a complete natural Vietnamese translation.
"""
    return _analyze_with_prompt(
        clip_uri=clip_uri,
        project_id=project_id,
        region=region,
        prompt=prompt,
        model=model,
        timeout=timeout,
    )


def analyze_visual_gap_chunk(
    *,
    clip_uri: str,
    project_id: str,
    region: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 900,
) -> tuple[list[LongDialogueLine], str]:
    """Analyze visible Chinese subtitles/captions in a short retry clip."""
    prompt = """Analyze this short retry clip to recover missing Chinese on-screen subtitles or captions.

Mandatory requirements:
- Return ONLY Chinese subtitles/captions/text that visibly appear on screen in this clip.
- Do not infer dialogue from the scene, do not describe actions, and do not summarize.
- If no Chinese subtitle/caption/text is visible, return lines=[].
- start/end are seconds from the beginning of this retry clip. They must match the time the text is visible or being spoken.
- Split separate subtitle cards/lines into separate items. Each line is usually 0.3-8 seconds.
- text_zh is the visible Chinese text; text_vi is a complete natural Vietnamese translation.
"""
    return _analyze_with_prompt(
        clip_uri=clip_uri,
        project_id=project_id,
        region=region,
        prompt=prompt,
        model=model,
        timeout=timeout,
    )


def compilation_metadata(
    *,
    summaries: list[str],
    project_id: str,
    region: str,
    model: str = "gemini-2.5-pro",
    timeout: int = 300,
) -> dict:
    from google.genai import types  # type: ignore

    prompt = f"""Create Vietnamese YouTube metadata for one long compilation video based on these section summaries:
{summaries}

Requirements:
- title_vi: under 80 characters, attractive but accurate, no hashtags.
- description_vi: 2-4 Vietnamese sentences.
- hashtags: 3-8 relevant hashtags, without the # symbol.
"""
    client = _client(project_id, region)
    response = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=4096,
            response_mime_type="application/json",
            response_schema=_CompilationMetadataSchema,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = response.parsed
    if parsed is None:
        import json

        parsed = _CompilationMetadataSchema(**json.loads(response.text))
    return normalize_long_text_value(parsed.model_dump())
