"""Wrapper quanh ffmpeg/ffprobe cho pipeline short-maker.

Tất cả thao tác video/audio gom ở đây để pipeline.py gọi cho gọn.
ffmpeg lấy từ PATH (trong container cài qua apt); nếu không có thì thử
imageio-ffmpeg (binary kèm theo) để chạy local dev.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache


class FFmpegError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise FFmpegError(
            "Không tìm thấy ffmpeg (PATH) và imageio-ffmpeg cũng không có."
        ) from e


@lru_cache(maxsize=1)
def ffprobe_bin() -> str:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    # imageio-ffmpeg không kèm ffprobe; trong container apt cài cả hai.
    raise FFmpegError("Không tìm thấy ffprobe trên PATH.")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise FFmpegError(f"ffmpeg lỗi (exit {proc.returncode}):\n{tail}")


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #

def probe(path: str) -> dict:
    """Trả về dict thông tin format+streams từ ffprobe."""
    cmd = [
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe lỗi: {proc.stderr[-1000:]}")
    return json.loads(proc.stdout)


def duration_seconds(path: str) -> float:
    info = probe(path)
    return float(info.get("format", {}).get("duration", 0.0) or 0.0)


def video_dimensions(path: str) -> tuple[int, int]:
    for s in probe(path).get("streams", []):
        if s.get("codec_type") == "video":
            return int(s.get("width", 0)), int(s.get("height", 0))
    return (0, 0)


# --------------------------------------------------------------------------- #
# Video processing
# --------------------------------------------------------------------------- #

def make_vertical_video(
    src: str,
    dst: str,
    *,
    start: float,
    end: float,
    speed: float,
    fps: int = 30,
    width: int = 1080,
    height: int = 1920,
) -> None:
    """Cắt [start,end] → hflip + speed + blur-pad 9:16 → H.264, KHÔNG audio.

    Nguồn dù 9:16 hay không đều xử lý đồng nhất: foreground fit trọn, background
    là chính video phóng to + boxblur lấp 2 dải. setpts đổi tốc độ video.
    """
    vf = (
        f"[0:v]hflip,setpts=PTS/{speed},split=2[fg][bg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=40[bgb];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}[v]"
    )
    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
        "-an", "-filter_complex", vf, "-map", "[v]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst,
    ]
    _run(cmd)


def normalize_vertical_video(
    src: str,
    dst: str,
    *,
    fps: int = 30,
    width: int = 1080,
    height: int = 1920,
    crf: int = 23,
) -> None:
    """Normalize a full clip to a consistent vertical H.264 stream."""
    vf = (
        f"[0:v]split=2[fg][bg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=40[bgb];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}[v]"
    )
    cmd = [
        ffmpeg_bin(), "-y", "-i", src,
        "-an", "-filter_complex", vf, "-map", "[v]",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst,
    ]
    _run(cmd)


def normalize_landscape_video(
    src: str,
    dst: str,
    *,
    fps: int = 30,
    width: int = 1920,
    height: int = 1080,
    crf: int = 23,
) -> None:
    """Cắt dải 16:9 ở GIỮA của nguồn 9:16 (bỏ blur trên/dưới) → ngang 1920×1080.

    Nguồn Douyin là 9:16 nhưng nội dung thật là dải 16:9 chính giữa, blur thêm
    trên/dưới. Crop đúng dải đó rồi scale về 16:9 ngang (pad an toàn nếu lệch).
    """
    vf = (
        f"crop=iw:min(ih\\,iw*9/16),"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    )
    cmd = [
        ffmpeg_bin(), "-y", "-i", src,
        "-an", "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst,
    ]
    _run(cmd)


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        secs += 1
        centis -= 100
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_escape(text: str) -> str:
    return (
        text.replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r", " ")
        .replace("\n", "\\N")
    )


def _wrap_subtitle(text: str) -> tuple[str, int]:
    full = " ".join(text.strip().split())
    if not full:
        return "", 36

    font_size = 36
    if len(full) > 34:
        font_size = max(18, int(36 * 34 / len(full)))
    return full, font_size


def _wrap_subtitle_two_lines(text: str) -> str:
    """Split Vietnamese display text into at most two balanced ASS lines."""
    full = " ".join(text.strip().split())
    if not full:
        return ""

    words = full.split()
    if len(words) < 2 or len(full) <= 38:
        return full

    best_index = 1
    best_score = float("inf")
    for index in range(1, len(words)):
        left = " ".join(words[:index])
        right = " ".join(words[index:])
        score = abs(len(left) - len(right))
        if score < best_score:
            best_index = index
            best_score = score
    return (
        " ".join(words[:best_index])
        + "\\N"
        + " ".join(words[best_index:])
    )


def split_subtitle_phrases(text: str) -> list[str]:
    """Split translated text into sequential one-line phrases by punctuation."""
    full = " ".join(text.strip().split())
    if not full:
        return []
    phrases = [
        " ".join(part.split()).strip()
        for part in re.split(r"[,，、;；:：.!。！？?…]+", full)
    ]
    return [phrase for phrase in phrases if phrase]


def _sequential_subtitle_events(
    text: str,
    *,
    start: float,
    end: float,
) -> list[tuple[float, float, str]]:
    phrases = split_subtitle_phrases(text)
    if len(phrases) <= 1:
        return [(start, end, phrases[0] if phrases else text)]

    duration = max(0.0, end - start)
    weights = [max(1, len(phrase.replace(" ", ""))) for phrase in phrases]
    total_weight = sum(weights)
    cursor = start
    events: list[tuple[float, float, str]] = []
    for index, (phrase, weight) in enumerate(zip(phrases, weights)):
        phrase_end = (
            end
            if index == len(phrases) - 1
            else cursor + duration * weight / total_weight
        )
        events.append((cursor, phrase_end, phrase))
        cursor = phrase_end
    return events


def subtitle_lines_for_voice(
    lines,
    voice_sync: list[dict],
    *,
    speed: float,
    video_dur: float,
):
    """Copy dialogue lines onto the rendered Vietnamese voice timeline."""
    from .gemini_script import DialogueLine

    synced: list[DialogueLine] = []
    for index, line in enumerate(lines):
        if index < len(voice_sync):
            timing = voice_sync[index]
            start = float(timing["actual_voice_start"])
            end = start + float(timing["fitted_duration"])
        else:
            start = float(line.start) / speed
            end = float(line.end) / speed
        start = max(0.0, min(video_dur, start))
        end = max(start, min(video_dur, end))
        if end <= start or not line.text_vi.strip():
            continue
        synced.append(
            DialogueLine(start=start, end=end, text_vi=line.text_vi)
        )
    return synced


def shift_dialogue_timeline(
    lines,
    *,
    offset_seconds: float,
    speed: float,
) -> None:
    """Shift subtitle and TTS source timings together, preserving durations."""
    source_offset = float(offset_seconds) * float(speed)
    if not source_offset:
        return
    for line in lines:
        original_start = float(line.start)
        duration = max(0.01, float(line.end) - original_start)
        line.start = max(0.0, original_start + source_offset)
        line.end = line.start + duration


def stabilize_speech_timings(
    visual_cues,
    speech_timings: list[tuple[float, float]],
    *,
    max_early_seconds: float = 0.25,
    max_late_seconds: float = 0.15,
) -> list[tuple[float, float]]:
    """Clamp noisy audio alignment around each visual subtitle cue."""
    stabilized: list[tuple[float, float]] = []
    for cue, (speech_start, speech_end) in zip(
        visual_cues,
        speech_timings,
    ):
        visual_start = float(cue.start)
        lower = max(0.0, visual_start - max(0.0, max_early_seconds))
        upper = visual_start + max(0.0, max_late_seconds)
        duration = max(0.05, float(speech_end) - float(speech_start))
        start = min(upper, max(lower, float(speech_start)))
        stabilized.append((start, start + duration))
    return stabilized


def scheduled_voice_start(
    planned_start: float,
    previous_voice_end: float,
    *,
    align_dub_to_speech: bool,
) -> float:
    """Keep each aligned cue on its own timestamp without cascading delay."""
    del previous_voice_end, align_dub_to_speech
    return max(0.0, float(planned_start))


def write_ass_subtitles(
    lines,
    dst_ass: str,
    *,
    speed: float,
    video_dur: float,
    font_name: str = "Arial",
    font_size: int = 36,
    margin_v: int = 690,
    exact_timing: bool = False,
    sequential_punctuation: bool = False,
    play_res_x: int = 1080,
    play_res_y: int = 1920,
) -> int:
    """Create bold yellow Vietnamese ASS subtitles inside the foreground video."""
    events: list[str] = []
    for line in sorted(lines, key=lambda x: float(x.start)):
        text = getattr(line, "text_vi", "").strip()
        if not text:
            continue
        start = max(0.0, float(line.start)) / speed
        if exact_timing:
            end = min(video_dur, float(line.end) / speed)
        else:
            end = max(float(line.end), float(line.start) + 0.5) / speed
            end = min(video_dur, max(start + 0.4, end))
        if start >= video_dur or end <= start:
            continue
        timed_texts = (
            _sequential_subtitle_events(text, start=start, end=end)
            if sequential_punctuation
            else [(start, end, text)]
        )
        for event_start, event_end, event_text in timed_texts:
            if event_end <= event_start:
                continue
            if sequential_punctuation:
                wrapped = " ".join(event_text.strip().split())
                display_font_size = font_size
            elif exact_timing:
                wrapped = _wrap_subtitle_two_lines(event_text)
                display_font_size = max(font_size, 42)
            else:
                wrapped, event_font_size = _wrap_subtitle(event_text.upper())
                display_font_size = min(font_size, event_font_size)
            if not wrapped:
                continue
            events.append(
                "Dialogue: 0,"
                f"{_ass_time(event_start)},{_ass_time(event_end)},Default,,0,0,0,,"
                f"{{\\fs{display_font_size}}}{_ass_escape(wrapped)}"
            )

    header = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {play_res_x}
PlayResY: {play_res_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,1,2,110,110,{max(0, int(margin_v))},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(dst_ass, "w", encoding="utf-8") as f:
        f.write(header)
        if events:
            f.write("\n".join(events))
            f.write("\n")
    return len(events)


def _subtitle_filter_path(path: str) -> str:
    # FFmpeg filter args use ':' as an option separator, so Windows drive paths need escaping.
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def burn_subtitles(
    src_video: str,
    ass_path: str,
    dst_video: str,
    *,
    crf: int = 20,
) -> None:
    """Burn ASS subtitles directly, without a background blur."""
    filt = f"subtitles='{_subtitle_filter_path(ass_path)}'"
    cmd = [
        ffmpeg_bin(), "-y", "-i", src_video,
        "-vf", filt, "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst_video,
    ]
    _run(cmd)


def render_landscape_segment(
    src: str,
    dst: str,
    *,
    start: float,
    end: float,
    ass_path: str | None = None,
    fps: int = 30,
    width: int = 1920,
    height: int = 1080,
    crf: int = 18,
) -> None:
    """Render a long-video segment directly from raw source in one encode pass."""
    filt = (
        f"crop=iw:min(ih\\,iw*9/16),"
        f"scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    )
    if ass_path:
        filt += f",subtitles='{_subtitle_filter_path(ass_path)}'"
    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
        "-vf", filt, "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst,
    ]
    _run(cmd)


def pad_video_tail(
    src_video: str,
    dst_video: str,
    *,
    extra_seconds: float,
    crf: int = 23,
) -> None:
    """Extend a no-audio video by freezing the final frame."""
    if extra_seconds <= 0.01:
        cmd = [
            ffmpeg_bin(), "-y", "-i", src_video,
            "-an", "-c:v", "copy", "-movflags", "+faststart", dst_video,
        ]
    else:
        filt = f"tpad=stop_mode=clone:stop_duration={extra_seconds:.3f}"
        cmd = [
            ffmpeg_bin(), "-y", "-i", src_video,
            "-vf", filt, "-an",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst_video,
        ]
    _run(cmd)


def cut_segment(src: str, dst_mp4: str, *, start: float, end: float) -> None:
    """Cắt clip gốc [start,end] (CÓ tiếng, chưa xử lý) để Gemini bóc lời thoại.

    Re-encode nhanh để cắt chính xác theo giây (copy dễ lệch keyframe) và giảm
    dung lượng khi upload lên GCS cho Gemini.
    """
    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst_mp4,
    ]
    _run(cmd)


def concat_mp4(inputs: list[str], dst: str) -> None:
    """Concatenate already-normalized MP4 files without re-encoding."""
    if not inputs:
        raise FFmpegError("concat_mp4 requires at least one input")
    list_path = os.path.join(os.path.dirname(dst), "concat.txt")
    with open(list_path, "w", encoding="utf-8") as handle:
        for path in inputs:
            escaped = os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    cmd = [
        ffmpeg_bin(), "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy", "-movflags", "+faststart", dst,
    ]
    _run(cmd)


def remove_spans(
    src: str,
    dst: str,
    keep_segments: list[tuple[float, float]],
    *,
    crf: int = 18,
    preset: str = "medium",
    audio_bitrate: str = "192k",
) -> None:
    """Keep only ``keep_segments`` (seconds) of ``src``, concatenated in order.

    Frame-accurate re-encode: uses ``filter_complex`` trim/atrim + concat so the
    cut points land exactly on the requested seconds (stream copy would snap to
    keyframes). Source resolution/fps are preserved. Used by the ad-review job
    to remove promotional spans while keeping subtitle/dub timing aligned.
    """
    segments = [
        (float(start), float(end))
        for start, end in keep_segments
        if float(end) - float(start) > 1e-3
    ]
    if not segments:
        raise FFmpegError("remove_spans requires at least one keep segment")

    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, (start, end) in enumerate(segments):
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        filters.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append(
        "".join(concat_inputs)
        + f"concat=n={len(segments)}:v=1:a=1[vout][aout]"
    )
    filtergraph = ";".join(filters)

    # Large graphs can overflow the command line; pass via a script file.
    script_path = os.path.join(os.path.dirname(dst) or ".", "remove_spans.txt")
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write(filtergraph)
    cmd = [
        ffmpeg_bin(), "-y", "-i", src,
        "-filter_complex_script", script_path,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart", dst,
    ]
    _run(cmd)


def extract_audio(src: str, dst_wav: str, *, start: float, end: float) -> None:
    """Trích audio đoạn [start,end] → WAV 44100 stereo (đầu vào cho Demucs)."""
    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
        "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", dst_wav,
    ]
    _run(cmd)


def _atempo_chain(speed: float) -> str:
    """atempo chỉ nhận 0.5–2.0; ghép chuỗi nếu cần. Với 1.03–1.05 thì 1 bộ là đủ."""
    return f"atempo={speed:.4f}"


def fit_audio_duration(
    src_audio: str,
    dst_audio: str,
    *,
    target_seconds: float,
    max_speed: float = 1.8,
) -> float:
    """Speed up a long voice clip without trimming spoken words."""
    if target_seconds <= 0:
        raise FFmpegError("target_seconds must be > 0")

    dur = duration_seconds(src_audio)
    if dur <= 0:
        raise FFmpegError(f"Cannot measure audio duration: {src_audio}")

    output_args = (
        ["-c:a", "pcm_s16le", "-ar", "44100"]
        if dst_audio.lower().endswith(".wav")
        else ["-c:a", "libmp3lame", "-q:a", "3"]
    )
    if dur <= target_seconds:
        cmd = [
            ffmpeg_bin(), "-y", "-i", src_audio,
            *output_args, dst_audio,
        ]
    else:
        speed = min(max_speed, dur / target_seconds)
        cmd = [
            ffmpeg_bin(), "-y", "-i", src_audio,
            "-filter:a", _atempo_chain(speed),
            *output_args, dst_audio,
        ]
    _run(cmd)
    return duration_seconds(dst_audio)


def trim_tts_silence(
    src_audio: str,
    dst_audio: str,
    *,
    threshold_db: float = -50.0,
    keep_seconds: float = 0.02,
) -> float:
    """Remove TTS encoder silence at both ends with a consonant-safe pad."""
    edge_trim = (
        "silenceremove="
        "start_periods=1:"
        "start_duration=0.02:"
        f"start_threshold={threshold_db:.1f}dB:"
        f"start_silence={max(0.0, keep_seconds):.3f}"
    )
    # Trim the leading edge, reverse, trim the new leading edge (the original
    # tail), then reverse back. A direct stop_periods=1 would stop at the first
    # natural pause inside a sentence and truncate everything after it.
    filt = f"{edge_trim},areverse,{edge_trim},areverse"
    cmd = [
        ffmpeg_bin(), "-y", "-i", src_audio,
        "-filter:a", filt,
        "-c:a", "pcm_s16le", "-ar", "44100", dst_audio,
    ]
    _run(cmd)
    trimmed_dur = duration_seconds(dst_audio)
    if trimmed_dur <= 0.05:
        raise FFmpegError(f"TTS clip became empty after silence trim: {src_audio}")
    return trimmed_dur


def voice_clip_fit_target(
    *,
    delay_ms: int,
    duration: float,
    final_dur: float,
    max_overflow_seconds: float = 1.0,
    max_microfit_speed: float = 1.02,
) -> float | None:
    """Return a safe target duration when a voice clip barely exceeds video end.

    A tiny TTS overflow should not fail a long batch, and it should not be fixed
    by hard-truncating the translation.  For small overflows we speed up only the
    offending clip very slightly.  Larger overflows still fail so real timing
    problems remain visible.
    """
    start = max(0.0, int(delay_ms) / 1000.0)
    available = max(0.0, float(final_dur) - start)
    if duration <= available:
        return None
    overflow = duration - available
    if overflow > max_overflow_seconds or available <= 0.05:
        raise FFmpegError(
            f"Vietnamese dub ends at {start + duration:.3f}s, beyond video "
            f"duration {final_dur:.3f}s by {overflow:.3f}s; refusing unsafe fit"
        )
    speed = duration / available
    if speed > max_microfit_speed:
        raise FFmpegError(
            f"Vietnamese dub needs {speed:.3f}x to fit final {overflow:.3f}s; "
            f"above safe micro-fit limit {max_microfit_speed:.3f}x"
        )
    return available


def mux_synced(
    video_noaudio: str,
    voice_clips: list[tuple[str, int]],
    bgm_wav: str | None,
    dst: str,
    *,
    speed: float,
    bgm_gain_db: float,
    final_dur: float,
    max_microfit_speed: float = 1.02,
    max_microfit_overflow_seconds: float = 1.0,
) -> None:
    """Lồng tiếng khớp thời điểm: đặt từng clip voice tại delay tương ứng + nền gốc.

    voice_clips: list (đường_dẫn_mp3, delay_ms) — delay tính trên timeline ĐÃ tăng tốc.
    bgm_wav: audio gốc đoạn cắt (giữ nguyên nhạc nền + tiếng gốc), áp atempo={speed},
             hạ về bgm_gain_db (~-20dB) làm nền nhỏ. Voice giữ nguyên âm lượng (nổi rõ).
    Output: H.264 copy, AAC stereo 44100, cắt đúng final_dur.
    """
    adjusted_voice_clips: list[tuple[str, int]] = []
    for clip_index, (path, delay) in enumerate(voice_clips):
        dur = duration_seconds(path)
        target = voice_clip_fit_target(
            delay_ms=delay,
            duration=dur,
            final_dur=final_dur,
            max_overflow_seconds=max_microfit_overflow_seconds,
            max_microfit_speed=max_microfit_speed,
        )
        if target is None:
            adjusted_voice_clips.append((path, delay))
            continue
        base, _ = os.path.splitext(dst)
        fitted = f"{base}.voice-fit-{clip_index:03d}.wav"
        fitted_dur = fit_audio_duration(
            path,
            fitted,
            target_seconds=target,
            max_speed=max_microfit_speed,
        )
        print(
            "[ffmpeg] micro-fit voice clip "
            f"{clip_index}: {dur:.3f}s -> {fitted_dur:.3f}s "
            f"(target={target:.3f}s, delay={delay}ms, final={final_dur:.3f}s)",
            flush=True,
        )
        adjusted_voice_clips.append((fitted, delay))

    inputs: list[str] = ["-i", video_noaudio]
    parts: list[str] = []
    labels: list[str] = []
    idx = 1
    for path, delay in adjusted_voice_clips:
        inputs += ["-i", path]
        d = max(0, int(delay))
        parts.append(f"[{idx}:a]adelay={d}|{d},aresample=44100[v{idx}]")
        labels.append(f"[v{idx}]")
        idx += 1
    if bgm_wav:
        inputs += ["-i", bgm_wav]
        parts.append(
            f"[{idx}:a]{_atempo_chain(speed)},volume={bgm_gain_db}dB,aresample=44100[b]"
        )
        labels.append("[b]")
        idx += 1

    if not labels:
        # Không có audio nào → tạo track im lặng.
        filt = "anullsrc=channel_layout=stereo:sample_rate=44100[a]"
    elif len(labels) == 1:
        filt = ";".join(parts) + f";{labels[0]}anull[a]"
    else:
        filt = (
            ";".join(parts)
            + f";{''.join(labels)}amix=inputs={len(labels)}:normalize=0:"
            "duration=longest:dropout_transition=0[a]"
        )

    cmd = [
        ffmpeg_bin(), "-y", *inputs,
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[a]",
        "-t", f"{final_dur:.3f}",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", dst,
    ]
    _run(cmd)
