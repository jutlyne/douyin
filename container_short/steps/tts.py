"""B6 — Google Cloud Text-to-Speech: kịch bản VN → voice.mp3.

Có chế độ "khít thời lượng": tổng hợp 1 lần ở speaking_rate=1.0, đo độ dài, rồi
nếu lệch nhiều thì tổng hợp lại với speaking_rate điều chỉnh để xấp xỉ target.
"""

from __future__ import annotations

from .ffmpeg_ops import duration_seconds


def _synthesize(text: str, voice: str, rate: float, dst_mp3: str, sa_path: str | None) -> None:
    from google.cloud import texttospeech as tts  # type: ignore

    if sa_path:
        client = tts.TextToSpeechClient.from_service_account_file(sa_path)
    else:
        client = tts.TextToSpeechClient()

    lang_code = "-".join(voice.split("-")[:2]) or "vi-VN"
    response = client.synthesize_speech(
        input=tts.SynthesisInput(text=text),
        voice=tts.VoiceSelectionParams(language_code=lang_code, name=voice),
        audio_config=tts.AudioConfig(
            audio_encoding=tts.AudioEncoding.MP3,
            speaking_rate=rate,
            sample_rate_hertz=44100,
        ),
    )
    with open(dst_mp3, "wb") as f:
        f.write(response.audio_content)


def synthesize_to_fit(
    text: str,
    dst_mp3: str,
    *,
    target_seconds: float,
    voice: str = "vi-VN-Wavenet-B",
    service_account_path: str | None = None,
    base_rate: float = 1.25,
    max_rate: float = 1.8,
) -> float:
    """Tổng hợp voice-over đọc ở tốc độ base_rate. Trả về độ dài thực tế (giây).

    Đọc ở base_rate cố định (mặc định 1.25 — giống style reup). CHỈ tăng tốc thêm
    nếu lời đọc dài hơn clip (target_seconds) để không vượt quá thời lượng video;
    nếu ngắn hơn thì giữ nguyên (phần đuôi đã có audio nền gốc lấp).
    """
    if not text.strip():
        raise ValueError("script_vi rỗng — không thể TTS.")

    _synthesize(text, voice, base_rate, dst_mp3, service_account_path)
    dur = duration_seconds(dst_mp3)
    if dur <= 0 or target_seconds <= 0:
        return dur

    if dur > target_seconds:  # lời đọc dài hơn clip → tăng tốc cho khít
        rate = min(max_rate, base_rate * (dur / target_seconds))
        _synthesize(text, voice, rate, dst_mp3, service_account_path)
        return duration_seconds(dst_mp3)
    return dur


def synthesize_once(
    text: str,
    dst_mp3: str,
    *,
    voice: str = "vi-VN-Wavenet-B",
    service_account_path: str | None = None,
    rate: float = 1.25,
) -> float:
    """Synthesize one short voice clip and return its duration."""
    if not text.strip():
        raise ValueError("text empty - cannot synthesize TTS.")
    _synthesize(text, voice, rate, dst_mp3, service_account_path)
    return duration_seconds(dst_mp3)
