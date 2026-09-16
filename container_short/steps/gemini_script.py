"""B3 — Gemini (Vertex AI) qua 2 bước:

  find_highlight(full_video)  → chọn đoạn cao trào (start,end) trên video gốc.
  transcribe_segment(clip)    → bóc lời thoại CLIP ĐÃ CẮT + dịch tiếng Việt + title/desc/hashtag.

Cắt trước rồi mới đưa clip ngắn cho Gemini giúp dịch sát & timestamp chuẩn hơn so
với phân tích nguyên video dài. Dùng SDK chính thức google-genai (structured output).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from pydantic import BaseModel


# ---- schema bước 1: highlight -------------------------------------------------
class _HighlightSchema(BaseModel):
    start: float
    end: float


# ---- schema bước 2: dub -------------------------------------------------------
class _Line(BaseModel):
    start: float
    end: float
    text_vi: str


class _DubSchema(BaseModel):
    dialogue: list[_Line]
    title_vi: str
    description_vi: str = ""
    hashtags: list[str] = []


class _OcrTranslationLine(BaseModel):
    index: int
    text_vi: str


class _OcrTranslationSchema(BaseModel):
    translations: list[_OcrTranslationLine]
    title_vi: str
    description_vi: str = ""
    hashtags: list[str] = []


class _VisualSubtitleLine(BaseModel):
    start: float
    end: float
    text_zh: str


class _VisualSubtitleSchema(BaseModel):
    cues: list[_VisualSubtitleLine]


class _SpeechTimingLine(BaseModel):
    index: int
    start: float
    end: float


class _SpeechTimingSchema(BaseModel):
    timings: list[_SpeechTimingLine]


@dataclass
class DialogueLine:
    start: float
    end: float
    text_vi: str


@dataclass
class Highlight:
    start: float
    end: float


@dataclass
class ScriptResult:
    dialogue: list[DialogueLine]
    title_vi: str = ""
    description_vi: str = ""
    hashtags: list[str] = field(default_factory=list)


def _make_client(project_id: str, region: str, service_account_path: str | None):
    from google import genai  # type: ignore

    credentials = None
    if service_account_path:
        from google.oauth2 import service_account  # type: ignore

        credentials = service_account.Credentials.from_service_account_file(
            service_account_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    return genai.Client(
        vertexai=True, project=project_id, location=region, credentials=credentials
    )


def _generate(client, model, parts, schema, timeout):
    from google.genai import types  # type: ignore

    resp = client.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(
            temperature=0.4,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
            max_output_tokens=32768,
            response_mime_type="application/json",
            response_schema=schema,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = getattr(resp, "parsed", None)
    if parsed is None:
        import json

        parsed = schema(**json.loads(resp.text))
    return parsed


# ---- bước 1 -------------------------------------------------------------------
def find_highlight(
    *,
    video_gs_uri: str,
    project_id: str,
    region: str,
    service_account_path: str | None,
    model: str = "gemini-2.5-pro",
    target_seconds: int = 40,
    speed: float = 1.0,
    mime_type: str = "video/mp4",
    timeout: int = 300,
) -> Highlight:
    from google.genai import types  # type: ignore

    source_window = int(round(target_seconds * speed))
    prompt = (
        f"Xem video Douyin này và chọn MỘT đoạn cao trào / hấp dẫn nhất để làm YouTube Short, "
        f"độ dài khoảng {source_window} giây (theo thời gian video gốc). "
        f"Chỉ trả start/end tính bằng giây."
    )
    client = _make_client(project_id, region, service_account_path)
    p = _generate(
        client, model,
        [types.Part.from_uri(file_uri=video_gs_uri, mime_type=mime_type), prompt],
        _HighlightSchema, timeout,
    )
    return Highlight(start=float(p.start), end=float(p.end))


# ---- bước 2 -------------------------------------------------------------------
def transcribe_segment(
    *,
    clip_gs_uri: str,
    project_id: str,
    region: str,
    service_account_path: str | None,
    model: str = "gemini-2.5-pro",
    mime_type: str = "video/mp4",
    timeout: int = 300,
    ocr_cues=None,
) -> ScriptResult:
    from google.genai import types  # type: ignore

    cue_instruction = ""
    if ocr_cues:
        cue_data = [
            {
                "index": index,
                "start": round(float(cue.start), 3),
                "end": round(float(cue.end), 3),
                "text_zh": cue.text_zh,
            }
            for index, cue in enumerate(ocr_cues)
        ]
        cue_instruction = f"""

OCR đã đọc được các cụm phụ đề Trung sau:
{json.dumps(cue_data, ensure_ascii=False)}

BẮT BUỘC trả đúng {len(cue_data)} phần tử dialogue theo đúng thứ tự cue OCR.
Giữ nguyên chính xác start/end của từng cue; chỉ dịch text_zh tương ứng sang text_vi.
"""

    prompt = """Đây là một CLIP ngắn đã được cắt từ video Douyin (tiếng Trung). Bạn là biên dịch viên lồng tiếng Trung → Việt.

Nhiệm vụ:
1. BÓC TOÀN BỘ lời thoại / lời nói của nhân vật trong clip này và DỊCH SÁT sang tiếng Việt
   tự nhiên (đúng nghĩa lời nhân vật nói — KHÔNG tóm tắt, KHÔNG thêm lời dẫn/bình luận).
   Bản dịch phải đầy đủ ý và nói tự nhiên. Không rút gọn hoặc bỏ chi tiết chỉ để
   ép vừa thời lượng; pipeline sẽ tự xử lý nhịp lồng tiếng.
   Trả danh sách "dialogue", mỗi phần tử gồm start, end (giây TÍNH TRONG CLIP NÀY) và text_vi
   là bản dịch của đúng câu đó. Sắp xếp theo thứ tự thời gian.
   Nếu hình ảnh đã có phụ đề tiếng Trung in sẵn, BẮT BUỘC ưu tiên thời điểm từng cụm phụ đề
   Trung xuất hiện và biến mất để đặt start/end cho bản dịch tương ứng. Mỗi dialogue phải
   tương ứng một cụm phụ đề Trung; không gộp nhiều cụm liên tiếp thành một câu dài.
2. Tạo metadata YouTube Shorts:
   - `title_vi`: đúng MỘT tiêu đề dưới 60 ký tự, CỰC GIẬT GÂN/hài hước, tạo tò mò mạnh
     khiến người xem phải bấm vào ngay. Dùng các đòn bẩy cảm xúc: tình huống bất ngờ/twist,
     con số hoặc chi tiết sốc trong clip, câu hỏi gây tò mò, hoặc cụm gây cấn kiểu
     "không ngờ", "ai ngờ", "cái kết", "sự thật là". Ưu tiên 1–2 emoji hợp ngữ cảnh ở đầu
     hoặc cuối nếu tăng độ thu hút. TUYỆT ĐỐI phải đúng nội dung clip (không bịa, không
     hứa hẹn thứ clip không có); không chứa hashtag, không viết HOA toàn bộ, không đặt dấu chấm cuối.
   - `description_vi`: 1–2 câu ngắn, giật gân và khơi gợi tò mò để giữ chân người xem
     (gợi mở "điều gì xảy ra tiếp theo" nhưng không spoiler hết); không chứa hashtag.
   - `hashtags`: 3–6 hashtag liên quan, ưu tiên tên nhân vật/chủ đề/tác phẩm; không
     nhét hashtag vào title hoặc description vì pipeline sẽ nối chúng vào cuối mô tả.
""" + cue_instruction

    client = _make_client(project_id, region, service_account_path)
    p = _generate(
        client, model,
        [types.Part.from_uri(file_uri=clip_gs_uri, mime_type=mime_type), prompt],
        _DubSchema, timeout,
    )
    dialogue = [
        DialogueLine(start=float(l.start), end=float(l.end), text_vi=l.text_vi.strip())
        for l in p.dialogue
        if l.text_vi and l.text_vi.strip()
    ]
    if ocr_cues and len(dialogue) == len(ocr_cues):
        dialogue = [
            DialogueLine(
                start=float(cue.start),
                end=float(cue.end),
                text_vi=line.text_vi,
            )
            for cue, line in zip(ocr_cues, dialogue)
        ]
    return ScriptResult(
        dialogue=dialogue,
        title_vi=p.title_vi.strip(),
        description_vi=(p.description_vi or "").strip(),
        hashtags=list(p.hashtags or []),
    )


def transcribe_segment_with_ocr(
    *,
    clip_gs_uri: str,
    ocr_cues,
    project_id: str,
    region: str,
    service_account_path: str | None,
    model: str = "gemini-2.5-pro",
    mime_type: str = "video/mp4",
    timeout: int = 300,
    attempts: int = 3,
) -> ScriptResult | None:
    """Translate exactly one Vietnamese line per OCR cue, or return None."""
    if not ocr_cues:
        return None

    for _ in range(max(1, attempts)):
        result = transcribe_segment(
            clip_gs_uri=clip_gs_uri,
            project_id=project_id,
            region=region,
            service_account_path=service_account_path,
            model=model,
            mime_type=mime_type,
            timeout=timeout,
            ocr_cues=ocr_cues,
        )
        if len(result.dialogue) != len(ocr_cues):
            continue
        if any(not line.text_vi.strip() for line in result.dialogue):
            continue
        result.dialogue = [
            DialogueLine(
                start=float(cue.start),
                end=float(cue.end),
                text_vi=line.text_vi.strip(),
            )
            for cue, line in zip(ocr_cues, result.dialogue)
        ]
        return result
    return None


def translate_ocr_cues_strict(
    *,
    ocr_cues,
    project_id: str,
    region: str,
    service_account_path: str | None,
    model: str = "gemini-2.5-pro",
    timeout: int = 300,
    attempts: int = 3,
) -> ScriptResult:
    """Translate OCR text 1:1 without asking the model to re-transcribe video."""
    if not ocr_cues:
        raise ValueError("OCR did not produce any subtitle cues")

    cue_data = [
        {
            "index": index,
            "duration_seconds": round(
                max(0.1, float(cue.end) - float(cue.start)),
                2,
            ),
            "text_zh": str(cue.text_zh).strip(),
        }
        for index, cue in enumerate(ocr_cues)
    ]
    expected_indexes = list(range(len(cue_data)))
    prompt = f"""Bạn là biên dịch viên phụ đề Trung → Việt.

Dữ liệu đầu vào là các câu tiếng Trung đã được OCR trực tiếp từ phụ đề trong video:
{json.dumps(cue_data, ensure_ascii=False)}

Yêu cầu bắt buộc:
- Dịch đúng từng `text_zh`, sát nghĩa, không tóm tắt hoặc suy diễn.
- Trả chính xác {len(cue_data)} phần tử trong `translations`.
- Mỗi `index` xuất hiện đúng một lần, theo thứ tự 0 đến {len(cue_data) - 1}.
- Không gộp câu, không tách câu, không bỏ câu và không thêm lời dẫn.
- `text_vi` là bản dịch của đúng `text_zh` cùng index.
- `text_vi` phải dịch đầy đủ ý của `text_zh`, tự nhiên khi đọc thành lời.
- Không được rút gọn, lược bỏ chi tiết, tên riêng, con số hoặc quan hệ ý nghĩa
  chỉ để ép câu Việt vừa `duration_seconds`; pipeline xử lý thời lượng riêng.
- `title_vi`: đúng một tiêu đề dưới 60 ký tự, có yếu tố giật gân, bất ngờ hoặc
  hài hước để tạo tò mò nhưng không xuyên tạc; không hashtag, không viết HOA toàn
  bộ và không đặt dấu chấm cuối.
- `description_vi`: tóm tắt hấp dẫn nội dung trong 1–2 câu ngắn, không hashtag.
- `hashtags`: 3–6 hashtag liên quan, ưu tiên tên nhân vật, tác phẩm và chủ đề.
- Tạo title/description/hashtags chỉ dựa trên nội dung các câu OCR trên.
"""

    client = _make_client(project_id, region, service_account_path)
    last_error = "unknown validation error"
    for _ in range(max(1, attempts)):
        parsed = _generate(
            client,
            model,
            [prompt],
            _OcrTranslationSchema,
            timeout,
        )
        translations = list(parsed.translations or [])
        indexes = [int(line.index) for line in translations]
        if indexes != expected_indexes:
            last_error = f"expected indexes {expected_indexes}, received {indexes}"
            continue
        if any(not line.text_vi.strip() for line in translations):
            last_error = "one or more Vietnamese translations are empty"
            continue

        dialogue = [
            DialogueLine(
                start=float(cue.start),
                end=float(cue.end),
                text_vi=translation.text_vi.strip(),
            )
            for cue, translation in zip(ocr_cues, translations)
        ]
        return ScriptResult(
            dialogue=dialogue,
            title_vi=parsed.title_vi.strip(),
            description_vi=(parsed.description_vi or "").strip(),
            hashtags=list(parsed.hashtags or []),
        )

    raise ValueError(
        "Gemini OCR translation failed strict 1:1 validation after "
        f"{max(1, attempts)} attempts: {last_error}"
    )


def align_ocr_cues_to_speech(
    *,
    clip_gs_uri: str,
    ocr_cues,
    project_id: str,
    region: str,
    service_account_path: str | None,
    model: str = "gemini-2.5-pro",
    mime_type: str = "video/mp4",
    timeout: int = 300,
    attempts: int = 3,
) -> list[tuple[float, float]]:
    """Listen to the Chinese speech and time each already-known OCR cue."""
    from google.genai import types  # type: ignore

    if not ocr_cues:
        raise ValueError("OCR did not produce any subtitle cues")

    cue_data = [
        {
            "index": index,
            "visual_start": round(float(cue.start), 3),
            "visual_end": round(float(cue.end), 3),
            "text_zh": str(cue.text_zh).strip(),
        }
        for index, cue in enumerate(ocr_cues)
    ]
    expected_indexes = list(range(len(cue_data)))
    prompt = f"""Nghe AUDIO tiếng Trung trong video và căn thời gian lời nói cho từng câu OCR.

Các câu OCR theo đúng thứ tự:
{json.dumps(cue_data, ensure_ascii=False)}

Yêu cầu bắt buộc:
- Trả chính xác {len(cue_data)} phần tử `timings`, index 0 đến {len(cue_data) - 1}.
- `start` là lúc âm tiết đầu tiên của đúng `text_zh` được nói.
- `end` là lúc âm tiết cuối cùng của đúng `text_zh` kết thúc.
- Timestamp tính bằng giây từ đầu clip.
- PHẢI nghe audio để căn; `visual_start/visual_end` chỉ là gợi ý tìm vùng,
  không được sao chép timestamp phụ đề nếu lời nói bắt đầu sớm hoặc kết thúc khác.
- Không dịch, không gộp câu, không tách câu, không đổi thứ tự.
- Timing phải tăng dần và mỗi end phải lớn hơn start.
"""

    client = _make_client(project_id, region, service_account_path)
    last_error = "unknown validation error"
    for _ in range(max(1, attempts)):
        parsed = _generate(
            client,
            model,
            [
                types.Part.from_uri(file_uri=clip_gs_uri, mime_type=mime_type),
                prompt,
            ],
            _SpeechTimingSchema,
            timeout,
        )
        timings = list(parsed.timings or [])
        indexes = [int(line.index) for line in timings]
        if indexes != expected_indexes:
            last_error = f"expected indexes {expected_indexes}, received {indexes}"
            continue

        clean = [(max(0.0, float(line.start)), float(line.end)) for line in timings]
        if any(end <= start for start, end in clean):
            last_error = "one or more speech timings have end <= start"
            continue
        if any(clean[index][0] < clean[index - 1][0] for index in range(1, len(clean))):
            last_error = "speech timings are not ordered"
            continue
        # Reject wildly unrelated timestamps while allowing normal subtitle lag.
        if any(
            abs(start - float(cue.start)) > 2.0
            for (start, _), cue in zip(clean, ocr_cues)
        ):
            last_error = "speech timing is more than 2s away from its visual cue"
            continue
        clip_upper_bound = max(float(cue.end) for cue in ocr_cues) + 2.0
        if any(end > clip_upper_bound for _, end in clean):
            last_error = "speech timing extends beyond the clip"
            continue
        return clean

    raise ValueError(
        "Gemini speech alignment failed after "
        f"{max(1, attempts)} attempts: {last_error}"
    )


def extract_visible_subtitle_cues(
    *,
    clip_gs_uri: str,
    project_id: str,
    region: str,
    service_account_path: str | None,
    model: str = "gemini-2.5-pro",
    mime_type: str = "video/mp4",
    timeout: int = 300,
    attempts: int = 3,
):
    """Use Gemini vision as OCR for burned-in Chinese subtitle text only."""
    from google.genai import types  # type: ignore

    from .subtitle_ocr import SubtitleCue

    prompt = """Đọc PHỤ ĐỀ TIẾNG TRUNG ĐƯỢC IN TRỰC TIẾP TRÊN HÌNH của clip này.

Đây là tác vụ OCR hình ảnh, không phải phiên âm âm thanh và không phải dịch.

Yêu cầu bắt buộc:
- Chỉ chép nguyên văn chữ Trung nhìn thấy trong vùng phụ đề của video.
- Không dùng lời nói để đoán hoặc sửa câu; không thêm chữ không nhìn thấy.
- Bỏ qua watermark, username, logo, nút giao diện và chữ trang trí.
- Mỗi lần nội dung phụ đề đổi thì tạo đúng một cue.
- `start` là lúc dòng chữ xuất hiện, `end` là lúc biến mất hoặc đổi nội dung.
- Timestamp tính bằng giây từ đầu clip này.
- `text_zh` giữ nguyên chữ Hán và dấu câu; không dịch sang ngôn ngữ khác.
- Không tạo cue cho frame không có phụ đề.
- Sắp xếp cue theo thời gian tăng dần.
"""
    client = _make_client(project_id, region, service_account_path)
    last_error = "unknown validation error"
    for _ in range(max(1, attempts)):
        parsed = _generate(
            client,
            model,
            [
                types.Part.from_uri(
                    file_uri=clip_gs_uri,
                    mime_type=mime_type,
                ),
                prompt,
            ],
            _VisualSubtitleSchema,
            timeout,
        )
        clean = []
        for line in parsed.cues or []:
            text = line.text_zh.strip()
            start = max(0.0, float(line.start))
            end = float(line.end)
            if not text or end <= start:
                continue
            clean.append(SubtitleCue(start=start, end=end, text_zh=text))
        clean.sort(key=lambda cue: cue.start)

        if not clean:
            last_error = "model returned zero valid visual subtitle cues"
            continue
        if any(
            clean[index].start < clean[index - 1].start
            for index in range(1, len(clean))
        ):
            last_error = "visual subtitle cues are not ordered"
            continue
        return clean

    raise ValueError(
        "Gemini visual subtitle OCR failed after "
        f"{max(1, attempts)} attempts: {last_error}"
    )
