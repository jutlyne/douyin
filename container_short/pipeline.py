"""Orchestrator: Douyin URL → YouTube Short tiếng Việt (final.mp4 + metadata).

Chạy được cả trong Cloud Run Job (qua short_job.py) lẫn local để test:
    python -m container_short.pipeline "<link douyin>" out.mp4 \
        --scratch gs://my-bucket/tmp --project my-proj --region us-central1

Các bước (xem plan): download → upload tmp4 lên GCS tạm cho Gemini xem →
Gemini trả highlight+kịch bản VN → ffmpeg (cut/flip/speed/blur-pad) →
Demucs lấy nhạc nền sạch → Cloud TTS → mix + mux → verify → (upload GCS).
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass, field

from .steps import ffmpeg_ops, gcsio
from .steps import subtitle_ocr
from .steps.download import download_douyin
from .steps.gemini_script import (
    DialogueLine,
    align_ocr_cues_to_speech,
    extract_visible_subtitle_cues,
    find_highlight,
    translate_ocr_cues_strict,
    transcribe_segment,
    transcribe_segment_with_ocr,
)
from .steps import capcut_tts
from .steps.tts import synthesize_once as synthesize_google_once
from .steps.tts import synthesize_to_fit as synthesize_google_to_fit

MIN_FINAL = 35.0
MAX_FINAL = 45.0
DEFAULT_PROJECT_ID = "YOUR_GCP_PROJECT"
DEFAULT_SCRATCH_GS_PREFIX = "gs://YOUR_GCP_PROJECT-shorts/tmp"


def _log(msg: str) -> None:
    print(f"[short] {msg}", flush=True)


@dataclass
class PipelineConfig:
    project_id: str = DEFAULT_PROJECT_ID
    region: str = "us-central1"
    service_account_path: str | None = None
    # Prefix GCS để upload mp4 tạm cho Gemini xem (bắt buộc vì Gemini đọc gs://).
    scratch_gs_prefix: str = DEFAULT_SCRATCH_GS_PREFIX
    target_seconds: int = 40
    speed: float = 1.0
    bgm_gain_db: float = -20.0
    enable_bgm: bool = True
    enable_subtitles: bool = False
    subtitle_margin_v: int = 690
    enable_subtitle_ocr: bool = False
    subtitle_ocr_fps: float = 4.0
    subtitle_sync_mode: str = "gemini"
    strict_subtitle_ocr: bool = False
    subtitle_ocr_provider: str = "tesseract"
    subtitle_time_offset_seconds: float = 0.0
    align_dub_to_speech: bool = False
    dub_max_speed: float = 1.35
    dub_hard_max_speed: float = 1.45
    dub_tail_headroom_seconds: float = 1.0
    dub_end_microfit_max_speed: float = 1.02
    dub_tail_pad_seconds: float = 0.0
    dub_mode: str = "timed"
    tts_provider: str = "capcut"
    tts_fallback_provider: str = "google"
    tts_voice: str = capcut_tts.DEFAULT_VOICE
    speaking_rate: float = 1.0
    google_tts_voice: str = "vi-VN-Wavenet-B"
    capcut_resource_id: str = capcut_tts.DEFAULT_RESOURCE_ID
    capcut_device_json: str | None = None
    capcut_poll_timeout: int = 300
    gemini_model: str = "gemini-2.5-flash"
    cookie: str | None = None


@dataclass
class PipelineResult:
    output_path: str
    duration: float
    width: int
    height: int
    title_vi: str = ""
    description_vi: str = ""
    hashtags: list[str] = field(default_factory=list)
    aweme_id: str = ""
    subtitle_sync_source: str = ""
    subtitle_cues: list[dict] = field(default_factory=list)
    voice_sync: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["youtube_title"] = _youtube_title(self.title_vi)
        d["youtube_description"] = _youtube_description(
            self.description_vi,
            self.hashtags,
        )
        return d


def _youtube_title(value: str, max_chars: int = 60) -> str:
    """Return a clean YouTube title with a hard character limit."""
    title = re.sub(r"(?<!\w)#[^\s#]+", "", str(value or ""))
    title = " ".join(title.split()).strip(" .,:;|-")
    if len(title) <= max_chars:
        return title
    shortened = title[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" .,:;|-")
    return shortened or title[:max_chars].rstrip(" .,:;|-")


def _youtube_hashtag(value: str) -> str:
    raw = str(value or "").strip().lstrip("#")
    raw = raw.replace("Đ", "D").replace("đ", "d")
    ascii_text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    compact = re.sub(r"[^A-Za-z0-9_]", "", ascii_text)
    return f"#{compact}" if compact else ""


def _youtube_description(description: str, hashtags: list[str]) -> str:
    """Put a clean 1–2 sentence summary first and hashtags on the final line."""
    clean = re.sub(r"(?<!\w)#[^\s#]+", "", str(description or ""))
    clean = " ".join(clean.split()).strip()
    tags: list[str] = []
    seen: set[str] = set()
    for item in hashtags:
        tag = _youtube_hashtag(item)
        key = tag.lower()
        if tag and key not in seen:
            tags.append(tag)
            seen.add(key)
        if len(tags) >= 6:
            break
    if clean and tags:
        return clean + "\n\n" + " ".join(tags)
    return clean or " ".join(tags)


def _pick_segment(src_dur: float, raw_start: float, raw_end: float, speed: float) -> tuple[float, float]:
    """Chọn đoạn [t0,t1] (giây, theo video gốc) sao cho độ dài/speed ∈ [40,55]."""
    t0 = max(0.0, raw_start)
    t1 = raw_end
    if not (t1 > t0 + 1.0) or t1 > src_dur + 0.5:
        # Gemini cho giá trị vô lý → lấy từ đầu một đoạn mục tiêu.
        t0 = 0.0
        t1 = min(src_dur, MAX_FINAL * speed)
    t1 = min(t1, src_dur)

    final_len = (t1 - t0) / speed
    if final_len > MAX_FINAL:
        t1 = t0 + MAX_FINAL * speed
    elif final_len < MIN_FINAL:
        t1 = min(src_dur, t0 + MIN_FINAL * speed)
        if (t1 - t0) / speed < MIN_FINAL and t0 > 0:
            t0 = max(0.0, t1 - MIN_FINAL * speed)
    return t0, min(t1, src_dur)


def _line_slot_seconds(
    lines,
    index: int,
    *,
    speed: float,
    video_dur: float,
) -> float:
    line = lines[index]
    start = max(0.0, float(line.start)) / speed
    end = max(float(line.end), float(line.start) + 0.3) / speed
    natural = max(0.5, end - start)

    if index + 1 < len(lines):
        next_start = max(0.0, float(lines[index + 1].start)) / speed
        available = max(0.3, next_start - start - 0.08)
    else:
        available = max(0.3, video_dur - start - 0.08)
    return max(0.3, min(natural, available))


def _repair_dialogue_timeline(
    lines: list[DialogueLine],
    *,
    segment_start: float,
    source_dur: float,
) -> list[DialogueLine]:
    """Normalize Gemini timestamps to clip-relative seconds, or distribute if degenerate."""
    clean = sorted(
        [
            DialogueLine(
                start=max(0.0, float(line.start)),
                end=max(float(line.end), float(line.start) + 0.5),
                text_vi=line.text_vi.strip(),
            )
            for line in lines
            if line.text_vi and line.text_vi.strip()
        ],
        key=lambda line: line.start,
    )
    if not clean:
        return []

    def shifted(offset: float) -> list[DialogueLine]:
        return [
            DialogueLine(
                start=max(0.0, line.start - offset),
                end=min(source_dur, max(0.4, line.end - offset)),
                text_vi=line.text_vi,
            )
            for line in clean
        ]

    def usable(candidate: list[DialogueLine]) -> bool:
        if not candidate:
            return False
        starts = [line.start for line in candidate]
        ends = [line.end for line in candidate]
        if max(starts) >= source_dur + 0.5:
            return False
        span = max(ends) - min(starts)
        return span >= min(6.0, source_dur * 0.35)

    if usable(clean) and max(line.end for line in clean) <= source_dur + 2.0:
        return [
            DialogueLine(
                start=min(source_dur, line.start),
                end=min(source_dur, max(line.start + 0.4, line.end)),
                text_vi=line.text_vi,
            )
            for line in clean
        ]

    source_shifted = shifted(segment_start)
    if usable(source_shifted):
        return source_shifted

    min_shifted = shifted(min(line.start for line in clean))
    if usable(min_shifted):
        return min_shifted

    total_weight = sum(max(8, len(line.text_vi)) for line in clean)
    cursor = 0.0
    pause = 0.18
    available = max(1.0, source_dur - pause * max(0, len(clean) - 1))
    repaired: list[DialogueLine] = []
    for i, line in enumerate(clean):
        weight = max(8, len(line.text_vi))
        dur = max(0.7, available * weight / total_weight)
        if i == len(clean) - 1:
            end = source_dur
        else:
            end = min(source_dur, cursor + dur)
        repaired.append(
            DialogueLine(
                start=cursor,
                end=min(source_dur, max(cursor + 0.4, end)),
                text_vi=line.text_vi,
            )
        )
        cursor = min(source_dur, end + pause)
    return repaired


def _synthesize_timed_voice_clips(
    *,
    script,
    work: str,
    video_dur: float,
    cfg: PipelineConfig,
) -> tuple[list[tuple[str, int]], float, list[dict]]:
    provider = cfg.tts_provider.strip().lower()
    lines = sorted(
        [l for l in script.dialogue if l.text_vi and l.text_vi.strip()],
        key=lambda l: float(l.start),
    )
    if not lines:
        raise ValueError("dialogue rỗng — không thể lồng tiếng theo timestamp.")

    capcut_device = None
    if provider == "capcut":
        capcut_device = capcut_tts.make_device(cfg.capcut_device_json)

    voice_clips: list[tuple[str, int]] = []
    voice_sync: list[dict] = []
    voice_clip_meta: list[dict] = []
    voice_end = 0.0
    for i, line in enumerate(lines):
        planned_start = max(0.0, float(line.start)) / cfg.speed
        if planned_start >= video_dur:
            continue
        delay_s = ffmpeg_ops.scheduled_voice_start(
            planned_start,
            voice_end,
            align_dub_to_speech=cfg.align_dub_to_speech,
        )

        raw_mp3 = os.path.join(work, f"voice_{i:03d}_raw.mp3")
        trim_wav = os.path.join(work, f"voice_{i:03d}_trim.wav")
        fit_wav = os.path.join(work, f"voice_{i:03d}.wav")
        if cfg.align_dub_to_speech:
            if i + 1 < len(lines):
                next_start = max(0.0, float(lines[i + 1].start)) / cfg.speed
                target = max(0.3, next_start - planned_start - 0.05)
            else:
                target = max(0.3, video_dur - planned_start - 0.05)
        elif cfg.strict_subtitle_ocr:
            target = max(
                0.05,
                (float(line.end) - float(line.start)) / cfg.speed,
            )
        else:
            target = _line_slot_seconds(
                lines,
                i,
                speed=cfg.speed,
                video_dur=video_dur,
            )
        target = min(target, max(0.3, video_dur - delay_s - 0.05))

        text = line.text_vi.strip()
        if provider == "capcut":
            try:
                capcut_tts.synthesize_once(
                    text,
                    raw_mp3,
                    voice=cfg.tts_voice,
                    resource_id=cfg.capcut_resource_id,
                    device=capcut_device,
                    rate=cfg.speaking_rate,
                    poll_timeout=cfg.capcut_poll_timeout,
                )
            except Exception as e:  # noqa: BLE001
                fallback = cfg.tts_fallback_provider.strip().lower()
                if fallback not in ("google", "gcp", "cloud"):
                    raise
                _log(f"CapCut TTS dòng {i + 1} lỗi, fallback Google: {e}")
                synthesize_google_once(
                    text,
                    raw_mp3,
                    voice=cfg.google_tts_voice,
                    service_account_path=cfg.service_account_path,
                    rate=1.25,
                )
        elif provider in ("google", "gcp", "cloud"):
            synthesize_google_once(
                text,
                raw_mp3,
                voice=cfg.google_tts_voice or cfg.tts_voice,
                service_account_path=cfg.service_account_path,
                rate=cfg.speaking_rate,
            )
        else:
            raise ValueError(f"TTS_PROVIDER không hỗ trợ: {cfg.tts_provider!r}")

        raw_dur = ffmpeg_ops.duration_seconds(raw_mp3)
        trimmed_dur = ffmpeg_ops.trim_tts_silence(raw_mp3, trim_wav)
        silence_removed = max(0.0, raw_dur - trimmed_dur)
        fit_target = (
            max(0.1, target - 0.08)
            if cfg.strict_subtitle_ocr and not cfg.align_dub_to_speech
            else target
        )
        fitted_dur = ffmpeg_ops.fit_audio_duration(
            trim_wav,
            fit_wav,
            target_seconds=fit_target,
            max_speed=(
                max(
                    1.0,
                    cfg.dub_hard_max_speed
                    if i == len(lines) - 1
                    else cfg.dub_max_speed,
                )
                if cfg.align_dub_to_speech
                else (2.0 if cfg.strict_subtitle_ocr else 1.35)
            ),
        )
        overflow = max(0.0, fitted_dur - target)
        next_planned_start = (
            max(0.0, float(lines[i + 1].start)) / cfg.speed
            if i + 1 < len(lines) else video_dur
        )
        voice_sync.append(
            {
                "index": i,
                "speech_start": round(planned_start, 3),
                "speech_end": round(max(planned_start, float(line.end) / cfg.speed), 3),
                "actual_voice_start": round(delay_s, 3),
                "start_delay": round(max(0.0, delay_s - planned_start), 3),
                "cue_start": round(delay_s, 3),
                "cue_end": round(delay_s + target, 3),
                "cue_duration": round(target, 3),
                "fit_target": round(fit_target, 3),
                "raw_duration": round(raw_dur, 3),
                "trimmed_duration": round(trimmed_dur, 3),
                "tts_silence_removed": round(silence_removed, 3),
                "fitted_duration": round(fitted_dur, 3),
                "speed_ratio": round(
                    trimmed_dur / fitted_dur if fitted_dur > 0 else 0.0,
                    3,
                ),
                "overflow": round(overflow, 3),
                "spill_past_next_speech": round(
                    max(0.0, delay_s + fitted_dur - next_planned_start),
                    3,
                ),
            }
        )
        if cfg.strict_subtitle_ocr and not cfg.align_dub_to_speech and overflow > 0.01:
            raise RuntimeError(
                f"TTS cue {i} exceeds subtitle window by "
                f"{overflow:.3f}s after safety-margin fitting"
        )
        delay_ms = int(round(delay_s * 1000))
        voice_clips.append((fit_wav, delay_ms))
        voice_clip_meta.append(
            {
                "clip_index": len(voice_clips) - 1,
                "sync_index": len(voice_sync) - 1,
                "path": fit_wav,
                "delay_ms": delay_ms,
                "delay_s": delay_s,
                "duration": fitted_dur,
            }
        )
        voice_end = max(voice_end, delay_s + fitted_dur)

    if not voice_clips:
        raise ValueError("không tạo được voice clip nào theo timestamp.")
    if cfg.align_dub_to_speech and voice_end > video_dur + 0.05:
        overflow = voice_end - video_dur
        tail_pad_limit = max(0.0, float(cfg.dub_tail_pad_seconds))
        latest = max(
            voice_clip_meta,
            key=lambda item: float(item["delay_s"]) + float(item["duration"]),
        )
        try:
            target = ffmpeg_ops.voice_clip_fit_target(
                delay_ms=int(latest["delay_ms"]),
                duration=float(latest["duration"]),
                final_dur=video_dur,
                max_microfit_speed=max(1.0, cfg.dub_end_microfit_max_speed),
            )
        except Exception as exc:  # noqa: BLE001
            if overflow <= tail_pad_limit:
                _log(
                    "final voice overflow left for video tail pad: "
                    f"overflow={overflow:.3f}s, limit={tail_pad_limit:.3f}s; {exc}"
                )
                return voice_clips, voice_end, voice_sync
            raise RuntimeError(
                f"Vietnamese dub ends at {voice_end:.3f}s, beyond video "
                f"duration {video_dur:.3f}s; refusing unsafe full-translation fit: {exc}"
            ) from exc
        if target is None:
            if overflow <= tail_pad_limit:
                _log(
                    "final voice overflow left for video tail pad: "
                    f"overflow={overflow:.3f}s, limit={tail_pad_limit:.3f}s"
                )
                return voice_clips, voice_end, voice_sync
            raise RuntimeError(
                f"Vietnamese dub ends at {voice_end:.3f}s, beyond video "
                f"duration {video_dur:.3f}s; no safe fit target found"
            )
        src_path = str(latest["path"])
        base, _ = os.path.splitext(src_path)
        final_fit = f"{base}_endfit.wav"
        fitted_dur = ffmpeg_ops.fit_audio_duration(
            src_path,
            final_fit,
            target_seconds=target,
            max_speed=max(1.0, cfg.dub_end_microfit_max_speed),
        )
        clip_index = int(latest["clip_index"])
        voice_clips[clip_index] = (final_fit, int(latest["delay_ms"]))
        latest["path"] = final_fit
        latest["duration"] = fitted_dur
        sync = voice_sync[int(latest["sync_index"])]
        sync["fitted_duration"] = round(fitted_dur, 3)
        sync["overflow"] = round(max(0.0, fitted_dur - target), 3)
        sync["end_microfit_applied"] = True
        voice_end = max(
            float(item["delay_s"]) + float(item["duration"])
            for item in voice_clip_meta
        )
        _log(
            "micro-fit final voice overflow: "
            f"target={target:.3f}s, fitted={fitted_dur:.3f}s, "
            f"voice_end={voice_end:.3f}s, video_dur={video_dur:.3f}s"
        )
        if voice_end > video_dur + 0.05:
            overflow = voice_end - video_dur
            if overflow <= tail_pad_limit:
                _log(
                    "final voice still over after micro-fit; leaving for video "
                    f"tail pad: overflow={overflow:.3f}s, limit={tail_pad_limit:.3f}s"
                )
                return voice_clips, voice_end, voice_sync
            raise RuntimeError(
                f"Vietnamese dub ends at {voice_end:.3f}s after micro-fit, "
                f"beyond video duration {video_dur:.3f}s"
            )
    return voice_clips, voice_end, voice_sync


def run(url_or_text: str, output_path: str, cfg: PipelineConfig) -> PipelineResult:
    if not cfg.scratch_gs_prefix:
        raise ValueError(
            "scratch_gs_prefix bắt buộc (Gemini đọc video qua gs:// URI)."
        )

    work = tempfile.mkdtemp(prefix="short_")
    run_id = uuid.uuid4().hex[:10]
    raw = os.path.join(work, "raw.mp4")
    seg_src = os.path.join(work, "segment_src.mp4")
    video_noaudio = os.path.join(work, "video_noaudio.mp4")
    video_subbed = os.path.join(work, "video_subbed.mp4")
    subtitles_ass = os.path.join(work, "subtitles.ass")
    seg_wav = os.path.join(work, "segment.wav")
    voice_mp3 = os.path.join(work, "voice.mp3")
    base = f"{cfg.scratch_gs_prefix.rstrip('/')}/{run_id}"
    scratch_uri = f"{base}/raw.mp4"
    clip_uri = f"{base}/segment.mp4"

    try:
        # B1 — download
        _log(f"download Douyin: {url_or_text[:80]}")
        info = download_douyin(url_or_text, raw, cookie=cfg.cookie)
        src_dur = ffmpeg_ops.duration_seconds(raw)
        _log(f"raw tải xong: aweme_id={info.aweme_id} dur={src_dur:.1f}s")

        # B2 — upload full lên GCS cho Gemini tìm highlight
        _log(f"upload tạm → {scratch_uri}")
        gcsio.upload(raw, scratch_uri, content_type="video/mp4")

        # B3a — Gemini TÌM đoạn cao trào (chỉ trả start/end)
        _log("Gemini tìm đoạn cao trào…")
        hl = find_highlight(
            video_gs_uri=scratch_uri,
            project_id=cfg.project_id,
            region=cfg.region,
            service_account_path=cfg.service_account_path,
            model=cfg.gemini_model,
            target_seconds=cfg.target_seconds,
            speed=cfg.speed,
        )
        t0, t1 = _pick_segment(src_dur, hl.start, hl.end, cfg.speed)
        if cfg.align_dub_to_speech and cfg.dub_tail_headroom_seconds > 0:
            current_dur = (t1 - t0) / cfg.speed
            extra = min(
                max(0.0, cfg.dub_tail_headroom_seconds),
                max(0.0, MAX_FINAL - current_dur),
            )
            t1 = min(src_dur, t1 + extra * cfg.speed)
        _log(
            f"highlight=[{hl.start:.1f},{hl.end:.1f}] → segment [{t0:.2f},{t1:.2f}] "
            f"(final≈{(t1 - t0) / cfg.speed:.1f}s)"
        )

        # B3b — CẮT clip gốc rồi đưa clip ngắn cho Gemini bóc + dịch lời thoại.
        _log("cắt clip + Gemini bóc lời thoại + dịch…")
        ffmpeg_ops.cut_segment(raw, seg_src, start=t0, end=t1)
        gcsio.upload(seg_src, clip_uri, content_type="video/mp4")
        ocr_cues = []
        sync_mode = cfg.subtitle_sync_mode.strip().lower()
        use_ocr = cfg.enable_subtitle_ocr or sync_mode == "ocr"
        if use_ocr:
            try:
                ocr_provider = cfg.subtitle_ocr_provider.strip().lower()
                if ocr_provider in ("gemini", "vision", "gemini-vision"):
                    _log("Gemini Vision OCR phụ đề Trung trực tiếp từ clip…")
                    ocr_cues = extract_visible_subtitle_cues(
                        clip_gs_uri=clip_uri,
                        project_id=cfg.project_id,
                        region=cfg.region,
                        service_account_path=cfg.service_account_path,
                        model=cfg.gemini_model,
                        attempts=3,
                    )
                elif ocr_provider == "tesseract":
                    _log(
                        f"Tesseract OCR sub Trung theo frame "
                        f"({cfg.subtitle_ocr_fps:.1f} fps)…"
                    )
                    ocr_cues = subtitle_ocr.detect_subtitle_cues(
                        seg_src,
                        work,
                        fps=cfg.subtitle_ocr_fps,
                    )
                else:
                    raise ValueError(
                        "SUBTITLE_OCR_PROVIDER must be gemini or tesseract"
                    )
                _log(f"OCR tìm thấy {len(ocr_cues)} cụm sub Trung")
            except Exception as e:  # noqa: BLE001
                if cfg.strict_subtitle_ocr:
                    raise RuntimeError(
                        f"OCR subtitle extraction failed: {e}"
                    ) from e
                _log(f"OCR sub Trung lỗi, fallback Gemini timestamp: {e}")
        if cfg.strict_subtitle_ocr and not ocr_cues:
            raise RuntimeError("OCR subtitle extraction returned zero cues")
        script = None
        ocr_committed = False
        if use_ocr and ocr_cues:
            if cfg.strict_subtitle_ocr:
                script = translate_ocr_cues_strict(
                    ocr_cues=ocr_cues,
                    project_id=cfg.project_id,
                    region=cfg.region,
                    service_account_path=cfg.service_account_path,
                    model=cfg.gemini_model,
                    attempts=3,
                )
                if cfg.align_dub_to_speech:
                    _log("Gemini nghe audio ZH để căn speech_start/speech_end từng cue…")
                    try:
                        speech_timings = align_ocr_cues_to_speech(
                            clip_gs_uri=clip_uri,
                            ocr_cues=ocr_cues,
                            project_id=cfg.project_id,
                            region=cfg.region,
                            service_account_path=cfg.service_account_path,
                            model=cfg.gemini_model,
                            attempts=3,
                        )
                    except Exception as e:  # noqa: BLE001
                        _log(
                            "speech alignment lỗi, fallback OCR visual timing: "
                            f"{e}"
                        )
                    else:
                        speech_timings = ffmpeg_ops.stabilize_speech_timings(
                            ocr_cues,
                            speech_timings,
                        )
                        for index, (line, (speech_start, speech_end)) in enumerate(
                            zip(script.dialogue, speech_timings)
                        ):
                            line.start = speech_start
                            next_start = (
                                speech_timings[index + 1][0]
                                if index + 1 < len(speech_timings)
                                else speech_end
                            )
                            line.end = max(
                                speech_start + 0.05,
                                min(speech_end, next_start),
                            )
                        _log(
                            f"speech alignment commit: {len(speech_timings)} cue "
                            f"(max TTS speed={cfg.dub_max_speed:.2f}x)"
                        )
                offset = float(cfg.subtitle_time_offset_seconds)
                if offset:
                    ffmpeg_ops.shift_dialogue_timeline(
                        script.dialogue,
                        offset_seconds=offset,
                        speed=cfg.speed,
                    )
                    _log(
                        "OCR timeline offset applied after speech alignment: "
                        f"{offset:+.3f}s for subtitle and TTS"
                    )
                ocr_committed = True
            else:
                script = transcribe_segment_with_ocr(
                    clip_gs_uri=clip_uri,
                    ocr_cues=ocr_cues,
                    project_id=cfg.project_id,
                    region=cfg.region,
                    service_account_path=cfg.service_account_path,
                    model=cfg.gemini_model,
                    attempts=3,
                )
                ocr_committed = script is not None
            if ocr_committed:
                _log(f"OCR sync commit: {len(ocr_cues)} cue khớp 1:1")
            else:
                _log("OCR sync không khớp số câu sau 3 lần, rollback Gemini timeline")
        if script is None:
            script = transcribe_segment(
                clip_gs_uri=clip_uri,
                project_id=cfg.project_id,
                region=cfg.region,
                service_account_path=cfg.service_account_path,
                model=cfg.gemini_model,
            )
        _log(f"dialogue lines={len(script.dialogue)} title={script.title_vi[:50]!r}")

        # B4 — video dọc (flip + speed + blur-pad, không audio)
        _log("ffmpeg dựng khung 9:16 (hflip+speed+blur-pad)…")
        ffmpeg_ops.make_vertical_video(
            raw, video_noaudio, start=t0, end=t1, speed=cfg.speed
        )
        video_dur = ffmpeg_ops.duration_seconds(video_noaudio)
        if not ocr_committed:
            script.dialogue = _repair_dialogue_timeline(
                script.dialogue,
                segment_start=t0,
                source_dur=video_dur * cfg.speed,
            )
        if script.dialogue:
            _log(
                "dialogue timeline "
                f"{script.dialogue[0].start:.1f}-{script.dialogue[-1].end:.1f}s "
                f"({len(script.dialogue)} dòng)"
            )
        video_for_mux = video_noaudio

        # B5 — giữ audio gốc đoạn cắt làm nền (nhạc + giọng nền), âm lượng nhỏ.
        bgm_wav = None
        if cfg.enable_bgm:
            _log("trích audio gốc đoạn cắt làm nền…")
            ffmpeg_ops.extract_audio(raw, seg_wav, start=t0, end=t1)
            bgm_wav = seg_wav

        # B6 — lồng tiếng. "timed" giữ timestamp từng câu để giữ khoảng nghỉ tự nhiên.
        provider = cfg.tts_provider.strip().lower()
        dub_mode = cfg.dub_mode.strip().lower()
        if dub_mode == "timed":
            _log(
                f"{provider} TTS {len(script.dialogue)} câu thoại theo timestamp "
                f"(giọng {cfg.tts_voice})…"
            )
            voice_clips, voice_dur, voice_sync = _synthesize_timed_voice_clips(
                script=script,
                work=work,
                video_dur=video_dur,
                cfg=cfg,
            )
        elif dub_mode == "continuous":
            full_script = " ".join(l.text_vi for l in script.dialogue).strip()
            _log(
                f"{provider} TTS {len(script.dialogue)} câu thoại, đọc liền mạch "
                f"(giọng {cfg.tts_voice})…"
            )
            if provider == "capcut":
                try:
                    voice_dur = capcut_tts.synthesize_to_fit(
                        full_script,
                        voice_mp3,
                        target_seconds=video_dur,
                        voice=cfg.tts_voice,
                        resource_id=cfg.capcut_resource_id,
                        device_json_path=cfg.capcut_device_json,
                        base_rate=cfg.speaking_rate,
                        poll_timeout=cfg.capcut_poll_timeout,
                    )
                except Exception as e:  # noqa: BLE001
                    fallback = cfg.tts_fallback_provider.strip().lower()
                    if fallback not in ("google", "gcp", "cloud"):
                        raise
                    _log(f"CapCut TTS lỗi, fallback sang Google TTS: {e}")
                    voice_dur = synthesize_google_to_fit(
                        full_script,
                        voice_mp3,
                        target_seconds=video_dur,
                        voice=cfg.google_tts_voice,
                        service_account_path=cfg.service_account_path,
                        base_rate=1.25,
                    )
            elif provider in ("google", "gcp", "cloud"):
                voice_dur = synthesize_google_to_fit(
                    full_script,
                    voice_mp3,
                    target_seconds=video_dur,
                    voice=cfg.google_tts_voice or cfg.tts_voice,
                    service_account_path=cfg.service_account_path,
                    base_rate=cfg.speaking_rate,
                )
            else:
                raise ValueError(f"TTS_PROVIDER không hỗ trợ: {cfg.tts_provider!r}")
            voice_clips = [(voice_mp3, 0)]
            voice_sync = []
        else:
            raise ValueError(f"DUB_MODE không hỗ trợ: {cfg.dub_mode!r}")
        _log(f"voice timeline end={voice_dur:.1f}s, video dur={video_dur:.1f}s")

        # B6b — burn subtitle after TTS so each cue follows the actual
        # Vietnamese voice clip, not the shorter Chinese speech window.
        if cfg.enable_subtitles:
            subtitle_lines = ffmpeg_ops.subtitle_lines_for_voice(
                script.dialogue,
                voice_sync,
                speed=cfg.speed,
                video_dur=video_dur,
            )
            count = ffmpeg_ops.write_ass_subtitles(
                subtitle_lines,
                subtitles_ass,
                speed=1.0,
                video_dur=video_dur,
                margin_v=cfg.subtitle_margin_v,
                exact_timing=True,
                sequential_punctuation=True,
            )
            if count:
                _log(
                    f"burn subtitle tiếng Việt theo voice thực tế "
                    f"({count} dòng)…"
                )
                ffmpeg_ops.burn_subtitles(
                    video_noaudio,
                    subtitles_ass,
                    video_subbed,
                )
                video_for_mux = video_subbed
            else:
                _log("không có dòng subtitle hợp lệ, bỏ qua burn sub.")

        # B7+B8 — voice (chủ đạo) + nền gốc giữ nguyên (-20dB).
        # Giữ output đúng dải Short mong muốn 35-45s. Nếu voice ngắn hơn clip,
        # không cắt cụt video; phần còn lại vẫn có audio gốc đã hạ âm lượng.
        final_dur = min(video_dur, MAX_FINAL)
        _log(f"mux voice + nền → {output_path} (final_dur={final_dur:.1f}s)")
        ffmpeg_ops.mux_synced(
            video_for_mux, voice_clips, bgm_wav, output_path,
            speed=cfg.speed, bgm_gain_db=cfg.bgm_gain_db, final_dur=final_dur,
        )

        # Verify
        w, h = ffmpeg_ops.video_dimensions(output_path)
        out_dur = ffmpeg_ops.duration_seconds(output_path)
        if out_dur >= 60.0:
            raise RuntimeError(f"Video ra {out_dur:.1f}s ≥ 60s — vi phạm giới hạn Short.")
        _log(f"DONE {w}x{h} {out_dur:.1f}s")

        return PipelineResult(
            output_path=output_path,
            duration=out_dur,
            width=w,
            height=h,
            title_vi=script.title_vi,
            description_vi=script.description_vi,
            hashtags=script.hashtags,
            aweme_id=info.aweme_id,
            subtitle_sync_source=(
                "ocr_speech_aligned"
                if ocr_committed and cfg.align_dub_to_speech
                else ("ocr_strict" if ocr_committed else "gemini")
            ),
            subtitle_cues=[
                {
                    "index": index,
                    "start": round(float(cue.start) / cfg.speed, 3),
                    "end": round(float(cue.end) / cfg.speed, 3),
                    "synced_start": round(float(line.start) / cfg.speed, 3),
                    "synced_end": round(float(line.end) / cfg.speed, 3),
                    "text_zh": cue.text_zh,
                    "text_vi": line.text_vi,
                }
                for index, (cue, line) in enumerate(
                    zip(ocr_cues, script.dialogue)
                )
            ]
            if ocr_committed
            else [],
            voice_sync=voice_sync,
        )
    finally:
        gcsio.delete(scratch_uri)  # dọn mp4 tạm trên GCS
        gcsio.delete(clip_uri)


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Douyin → YouTube Short VN")
    ap.add_argument("url", help="Link/đoạn text chia sẻ Douyin")
    ap.add_argument("output", help="Đường dẫn file mp4 ra (local)")
    ap.add_argument(
        "--scratch",
        default=os.environ.get("SCRATCH_GS_PREFIX", DEFAULT_SCRATCH_GS_PREFIX),
        help="gs:// prefix tạm cho Gemini",
    )
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", DEFAULT_PROJECT_ID))
    ap.add_argument("--region", default=os.environ.get("VERTEX_REGION", "us-central1"))
    ap.add_argument("--sa", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    ap.add_argument("--speed", type=float, default=1.04)
    ap.add_argument("--target", type=int, default=48)
    ap.add_argument("--dub-mode", default=os.environ.get("DUB_MODE", "timed"))
    ap.add_argument("--enable-subtitles", default=os.environ.get("ENABLE_SUBTITLES", "false"))
    ap.add_argument(
        "--subtitle-margin-v",
        type=int,
        default=int(os.environ.get("SUBTITLE_MARGIN_V", "690")),
    )
    ap.add_argument("--enable-subtitle-ocr", default=os.environ.get("ENABLE_SUBTITLE_OCR", "false"))
    ap.add_argument("--subtitle-sync-mode", default=os.environ.get("SUBTITLE_SYNC_MODE", "gemini"))
    ap.add_argument(
        "--subtitle-ocr-fps",
        type=float,
        default=float(os.environ.get("SUBTITLE_OCR_FPS", "4.0")),
    )
    ap.add_argument("--tts-provider", default=os.environ.get("TTS_PROVIDER", "capcut"))
    ap.add_argument("--tts-fallback-provider", default=os.environ.get("TTS_FALLBACK_PROVIDER", "google"))
    ap.add_argument("--tts-voice", default=os.environ.get("TTS_VOICE"))
    ap.add_argument("--google-tts-voice", default=os.environ.get("GOOGLE_TTS_VOICE", "vi-VN-Wavenet-B"))
    ap.add_argument(
        "--capcut-resource-id",
        default=os.environ.get("CAPCUT_RESOURCE_ID", capcut_tts.DEFAULT_RESOURCE_ID),
    )
    ap.add_argument("--capcut-device-json", default=os.environ.get("CAPCUT_DEVICE_JSON"))
    ap.add_argument(
        "--capcut-poll-timeout",
        type=int,
        default=int(os.environ.get("CAPCUT_POLL_TIMEOUT", "300")),
    )
    args = ap.parse_args()

    tts_provider = args.tts_provider.strip().lower() or "capcut"
    default_voice = capcut_tts.DEFAULT_VOICE if tts_provider == "capcut" else "vi-VN-Wavenet-B"

    cfg = PipelineConfig(
        project_id=args.project,
        region=args.region,
        service_account_path=args.sa,
        scratch_gs_prefix=args.scratch,
        target_seconds=args.target,
        speed=args.speed,
        enable_subtitles=args.enable_subtitles.strip().lower() in ("1", "true", "yes", "on"),
        subtitle_margin_v=args.subtitle_margin_v,
        enable_subtitle_ocr=args.enable_subtitle_ocr.strip().lower() in ("1", "true", "yes", "on"),
        subtitle_ocr_fps=args.subtitle_ocr_fps,
        subtitle_sync_mode=args.subtitle_sync_mode,
        dub_mode=args.dub_mode,
        tts_provider=tts_provider,
        tts_fallback_provider=args.tts_fallback_provider,
        tts_voice=(args.tts_voice or default_voice).strip(),
        google_tts_voice=args.google_tts_voice,
        capcut_resource_id=args.capcut_resource_id,
        capcut_device_json=args.capcut_device_json,
        capcut_poll_timeout=args.capcut_poll_timeout,
    )
    res = run(args.url, args.output, cfg)
    import json

    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
