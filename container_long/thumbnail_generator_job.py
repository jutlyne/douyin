from __future__ import annotations

import io
import json
import os
import sys
import time
from typing import Any

from google import genai
from google.cloud import storage
from google.genai import types
from PIL import Image, ImageDraw, ImageFont, ImageOps


PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "global")
MODEL_NAME = os.environ.get("THUMBNAIL_MODEL", "gemini-2.5-flash-image")
CANVAS_SIZE = (1280, 720)
MAX_YOUTUBE_BYTES = 2 * 1024 * 1024
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/ttf-dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansCondensed-Bold.ttf",
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _project_id() -> str:
    return _required_env("GCP_PROJECT_ID")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Expected gs:// URI: {uri}")
    bucket, separator, name = uri[5:].partition("/")
    if not separator or not bucket or not name:
        raise ValueError(f"Invalid gs:// URI: {uri}")
    return bucket, name


def _blob(client: storage.Client, uri: str) -> storage.Blob:
    bucket_name, blob_name = _parse_gs_uri(uri)
    return client.bucket(bucket_name).blob(blob_name)


def _load_json(client: storage.Client, uri: str) -> dict:
    data = _blob(client, uri).download_as_text(encoding="utf-8")
    return json.loads(data or "{}")


def _write_json(client: storage.Client, uri: str, value: dict) -> None:
    if not uri:
        return
    _blob(client, uri).upload_from_string(
        json.dumps(value, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


def _part_number() -> int:
    try:
        return max(1, int(float(_env("PART_NUMBER", "1"))))
    except ValueError:
        return 1


def _story_context(metadata: dict) -> tuple[str, str, str]:
    title = str(metadata.get("title_vi") or "").strip()
    description = str(metadata.get("description_vi") or "").strip()
    cliffhanger: dict = {}
    source_parts = metadata.get("source_parts") or []
    if source_parts and isinstance(source_parts[0], dict):
        cliffhanger = source_parts[0].get("cliffhanger") or {}
    if not cliffhanger:
        cliffhanger = metadata.get("cliffhanger") or {}
    cliffhanger_text = " — ".join(
        value
        for value in (
            str(cliffhanger.get("title_vi") or "").strip(),
            str(cliffhanger.get("reason_vi") or "").strip(),
        )
        if value
    )
    return title[:300], description[:1600], cliffhanger_text[:900]


def _generation_prompt(metadata: dict, part_number: int) -> str:
    title, description, cliffhanger = _story_context(metadata)
    return "\n".join(
        [
            "Use case: ads-marketing",
            "Asset type: high-click-through YouTube thumbnail artwork, 16:9",
            (
                "Primary request: create a brand-new dramatic Chinese donghua "
                f"illustration for Part {part_number} of this story."
            ),
            (
                "Input image role: visual identity reference for the main hero only. "
                "Keep his recognizable white panda meme face, black ears, simple black "
                "facial features, mandatory wide brown straw hat, and ancient Chinese robe. "
                "Change his pose and scene to match this Part."
            ),
            f"Story title: {title or 'Xuyên không Đại Đường'}",
            f"Story content: {description or 'The hero uses modern knowledge in ancient China.'}",
            f"Ending hook: {cliffhanger or 'A dangerous new conflict is about to begin.'}",
            (
                "Scene: portray the strongest confrontation or revelation from the story "
                "content; hero very large in the foreground; supporting characters and "
                "ancient Chang'an setting behind him."
            ),
            (
                "Style: polished Chinese donghua/anime key art, extreme clickbait energy, "
                "orange-red fire against electric blue-purple lightning, strong rim light, "
                "high contrast, thick clean outlines, cinematic depth."
            ),
            (
                "Composition: keep the upper 22 percent visually simple for a headline and "
                "leave the bottom-left corner clear for a Part badge; faces large and readable "
                "on mobile."
            ),
            (
                "Constraints: artwork only. Do not draw any letters, words, numbers, logos, "
                "watermarks, subtitles, duration badges, calligraphy, or illegible pseudo-text."
            ),
        ]
    )


def _generated_image_bytes(
    *,
    reference_bytes: bytes,
    reference_mime_type: str,
    prompt: str,
) -> tuple[bytes, str]:
    client = genai.Client(
        vertexai=True,
        project=_project_id(),
        location=VERTEX_REGION,
        http_options=types.HttpOptions(api_version="v1"),
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[
                    types.Part.from_bytes(
                        data=reference_bytes,
                        mime_type=reference_mime_type,
                    ),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio="16:9"),
                ),
            )
            for part in response.parts or []:
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    data = inline.data
                    if isinstance(data, str):
                        import base64

                        data = base64.b64decode(data)
                    return bytes(data), str(inline.mime_type or "image/png")
            raise RuntimeError("Vertex image response did not contain image bytes")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= 3:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"Thumbnail image generation failed: {last_error}")


def _font_path() -> str:
    for candidate in FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("DejaVu Sans Bold font was not found in the image")


def _fit_font(
    draw: ImageDraw.ImageDraw,
    *,
    text: str,
    max_width: int,
    start_size: int,
    min_size: int,
    stroke_width: int,
) -> ImageFont.FreeTypeFont:
    path = _font_path()
    for size in range(start_size, min_size - 1, -2):
        font = ImageFont.truetype(path, size=size)
        box = draw.textbbox(
            (0, 0), text, font=font, stroke_width=stroke_width
        )
        if box[2] - box[0] <= max_width:
            return font
    return ImageFont.truetype(path, size=min_size)


def _centered_x(
    draw: ImageDraw.ImageDraw,
    *,
    text: str,
    font: ImageFont.FreeTypeFont,
    stroke_width: int,
) -> int:
    box = draw.textbbox(
        (0, 0), text, font=font, stroke_width=stroke_width
    )
    width = box[2] - box[0]
    return max(8, (CANVAS_SIZE[0] - width) // 2)


def _badge_text(part_number: int) -> str:
    """Return the optional custom badge, or the normal per-part label."""
    return _env("THUMBNAIL_BADGE_TEXT").upper() or f"PHẦN {part_number}"


def _overlay_text(image_bytes: bytes, *, part_number: int) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as source:
        canvas = ImageOps.fit(
            source.convert("RGB"),
            CANVAS_SIZE,
            method=Image.Resampling.LANCZOS,
        )
    overlay = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    gradient = ImageDraw.Draw(overlay)
    for y in range(190):
        alpha = max(0, int(205 * (1 - y / 190)))
        gradient.line((0, y, CANVAS_SIZE[0], y), fill=(0, 0, 0, alpha))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    headline = _env("THUMBNAIL_HEADLINE", "XUYÊN KHÔNG ĐẠI ĐƯỜNG").upper()
    headline_font = _fit_font(
        draw,
        text=headline,
        max_width=1230,
        start_size=100,
        min_size=54,
        stroke_width=13,
    )
    x = _centered_x(
        draw,
        text=headline,
        font=headline_font,
        stroke_width=13,
    )
    y = 8
    draw.text(
        (x, y),
        headline,
        font=headline_font,
        fill=(255, 255, 255),
        stroke_width=15,
        stroke_fill=(255, 255, 255),
    )
    draw.text(
        (x, y),
        headline,
        font=headline_font,
        fill=(255, 205, 0),
        stroke_width=9,
        stroke_fill=(0, 0, 0),
    )

    badge_text = _badge_text(part_number)
    badge_font = _fit_font(
        draw,
        text=badge_text,
        max_width=500,
        start_size=76,
        min_size=50,
        stroke_width=5,
    )
    badge_box = draw.textbbox(
        (0, 0), badge_text, font=badge_font, stroke_width=5
    )
    badge_width = badge_box[2] - badge_box[0] + 48
    badge_height = badge_box[3] - badge_box[1] + 38
    left = 18
    top = CANVAS_SIZE[1] - badge_height - 18
    right = left + badge_width
    bottom = top + badge_height
    draw.rounded_rectangle(
        (left - 7, top - 7, right + 7, bottom + 7),
        radius=30,
        fill=(255, 255, 255),
    )
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=25,
        fill=(20, 225, 15),
        outline=(0, 0, 0),
        width=7,
    )
    draw.text(
        (left + 24, top + 10 - badge_box[1]),
        badge_text,
        font=badge_font,
        fill=(255, 255, 255),
        stroke_width=5,
        stroke_fill=(0, 0, 0),
    )

    rgb = canvas.convert("RGB")
    for quality in (92, 88, 84, 80, 76, 72):
        output = io.BytesIO()
        rgb.save(output, format="JPEG", quality=quality, optimize=True)
        encoded = output.getvalue()
        if len(encoded) <= MAX_YOUTUBE_BYTES:
            return encoded
    raise RuntimeError("Generated thumbnail could not be compressed below 2 MB")


def _run() -> dict:
    metadata_uri = _required_env("METADATA_URI")
    reference_uri = _required_env("REFERENCE_IMAGE_URI")
    thumbnail_uri = _required_env("THUMBNAIL_URI")
    result_uri = _env("THUMBNAIL_RESULT_URI")
    part_number = _part_number()
    client = storage.Client(project=_project_id())
    thumbnail_blob = _blob(client, thumbnail_uri)
    if thumbnail_blob.exists() and not _as_bool(
        _env("THUMBNAIL_FORCE_REFRESH"), default=False
    ):
        thumbnail_blob.reload()
        result = {
            "event": "long.thumbnail.completed",
            "ok": True,
            "status": "reused",
            "thumbnail_uri": thumbnail_uri,
            "thumbnail_size": int(thumbnail_blob.size or 0),
            "part_number": part_number,
            "model": MODEL_NAME,
        }
        _write_json(client, result_uri, result)
        return result

    metadata = _load_json(client, metadata_uri)
    reference_blob = _blob(client, reference_uri)
    reference_blob.reload()
    reference_bytes = reference_blob.download_as_bytes()
    reference_mime_type = reference_blob.content_type or "image/png"
    prompt = _generation_prompt(metadata, part_number)
    artwork_bytes, artwork_mime_type = _generated_image_bytes(
        reference_bytes=reference_bytes,
        reference_mime_type=reference_mime_type,
        prompt=prompt,
    )
    thumbnail_bytes = _overlay_text(artwork_bytes, part_number=part_number)
    thumbnail_blob.upload_from_string(
        thumbnail_bytes,
        content_type="image/jpeg",
    )
    result = {
        "event": "long.thumbnail.completed",
        "ok": True,
        "status": "generated",
        "thumbnail_uri": thumbnail_uri,
        "thumbnail_size": len(thumbnail_bytes),
        "part_number": part_number,
        "headline": _env("THUMBNAIL_HEADLINE", "XUYÊN KHÔNG ĐẠI ĐƯỜNG"),
        "badge_text": _badge_text(part_number),
        "model": MODEL_NAME,
        "artwork_mime_type": artwork_mime_type,
        "reference_image_uri": reference_uri,
        "metadata_uri": metadata_uri,
        "prompt": prompt,
    }
    _write_json(client, result_uri, result)
    return result


def main() -> int:
    result_uri = _env("THUMBNAIL_RESULT_URI")
    try:
        result = _run()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[thumbnail-generator] FAILED: {exc}", flush=True)
        if result_uri:
            try:
                _write_json(
                    storage.Client(project=_project_id()),
                    result_uri,
                    {
                        "event": "long.thumbnail.failed",
                        "ok": False,
                        "status": "failed",
                        "thumbnail_uri": _env("THUMBNAIL_URI"),
                        "part_number": _part_number(),
                        "model": MODEL_NAME,
                        "error": str(exc),
                    },
                )
            except Exception as write_exc:  # noqa: BLE001
                print(
                    f"[thumbnail-generator] failed to write result: {write_exc}",
                    flush=True,
                )
        return 1


if __name__ == "__main__":
    sys.exit(main())
