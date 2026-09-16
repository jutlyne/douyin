from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass

from container_long.gemini_long import (
    analyze_chunk,
    analyze_gap_chunk,
    analyze_visual_gap_chunk,
    select_cliffhanger_boundary,
)
from container_long.models import ChunkResult, LongDialogueLine, SourceResult
from container_long.subtitles import write_vietnamese_srt
from container_long.text_cleanup import (
    normalize_long_dialogue_lines,
    normalize_long_text_value,
    normalize_long_vi_text,
)
from container_short.pipeline import PipelineConfig, _synthesize_timed_voice_clips
from container_short.steps import ffmpeg_ops, gcsio
from container_short.steps.download import download_douyin
from container_short.steps.gemini_script import DialogueLine, ScriptResult
from container_long.utils import (
    chunk_ranges,
    dialogue_gap_ranges,
    merge_dialogue_lines,
    non_overlapping_dialogue_lines,
    prefer_visual_dialogue_lines,
    readable_short_subtitle_lines,
    replace_tail_dialogue_lines,
    select_chunk_lines,
    suppress_tail_repeated_dialogue_lines,
    voice_synced_dialogue_lines,
)


@dataclass
class LongPipelineConfig:
    project_id: str = "YOUR_GCP_PROJECT"
    region: str = "global"
    scratch_prefix: str = "gs://YOUR_GCP_PROJECT-scratch-sg/long"
    model: str = "gemini-2.5-pro"
    # Chunk ngắn (~4 phút) để Gemini bóc HẾT thoại; chunk >5 phút bị cắt cụt đuôi.
    chunk_seconds: int = 240
    min_chunk_seconds: int = 180
    max_chunk_seconds: int = 300
    analysis_padding_seconds: float = 3.0
    tts_provider: str = "capcut"
    tts_fallback_provider: str = "google"
    tts_voice: str = "BV074_streaming"
    google_tts_voice: str = "vi-VN-Wavenet-B"
    speaking_rate: float = 1.0
    bgm_gain_db: float = -20.0
    dub_max_speed: float = 1.35
    dub_hard_max_speed: float = 1.45
    dub_end_microfit_max_speed: float = 1.08
    dub_tail_pad_seconds: float = 1.0
    visual_refresh: str = "auto"
    video_crf: int = 18
    video_width: int = 1920
    video_height: int = 1080
    capcut_resource_id: str = "7102355709945188865"
    capcut_device_json: str | None = None
    capcut_poll_timeout: int = 300
    cookie: str | None = None
    source_start_seconds: float = 0.0
    source_max_seconds: float = 0.0
    cliffhanger_enabled: bool = False
    cliffhanger_min_seconds: float = 600.0
    cliffhanger_max_seconds: float = 900.0
    raw_source_uri: str = ""


def _raw_metadata_uri(raw_source_uri: str) -> str:
    return raw_source_uri.rsplit(".", 1)[0] + ".json"


def _load_or_download_source(
    *,
    douyin_url: str,
    raw_path: str,
    cfg: LongPipelineConfig,
) -> str:
    """Return aweme id while reusing a URL-level raw GCS cache when present."""
    raw_uri = str(cfg.raw_source_uri or "").strip()
    if raw_uri and gcsio.exists(raw_uri):
        print(f"[long] raw source cache hit: {raw_uri}", flush=True)
        gcsio.download(raw_uri, raw_path)
        aweme_id = ""
        meta_uri = _raw_metadata_uri(raw_uri)
        if gcsio.exists(meta_uri):
            meta_path = raw_path + ".json"
            gcsio.download(meta_uri, meta_path)
            try:
                with open(meta_path, encoding="utf-8-sig") as handle:
                    aweme_id = str(json.load(handle).get("aweme_id") or "")
            except (OSError, ValueError, TypeError):
                aweme_id = ""
        return aweme_id

    info = download_douyin(douyin_url, raw_path, cookie=cfg.cookie)
    if raw_uri:
        print(f"[long] writing raw source cache: {raw_uri}", flush=True)
        gcsio.upload(raw_path, raw_uri, content_type="video/mp4")
        meta_path = raw_path + ".json"
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "aweme_id": info.aweme_id,
                    "douyin_url": douyin_url,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        gcsio.upload(
            meta_path,
            _raw_metadata_uri(raw_uri),
            content_type="application/json",
        )
    return info.aweme_id


def _bounded_source_range(
    duration: float,
    start_seconds: float,
    max_seconds: float,
) -> tuple[float, float]:
    duration = float(duration)
    start = max(0.0, float(start_seconds))
    limit = float(max_seconds)
    if duration <= 0:
        raise ValueError("duration must be > 0")
    if start >= duration:
        raise ValueError("source_start_seconds must be before source duration")
    if limit <= 0:
        return start, duration
    if limit < 60:
        raise ValueError("source_max_seconds must be at least 60 seconds")
    return start, min(duration, start + limit)


def _select_cliffhanger_chunks(
    chunks: list[ChunkResult],
    *,
    cfg: LongPipelineConfig,
) -> tuple[list[ChunkResult], dict]:
    """Keep chunks through the strongest eligible story-hook boundary."""
    if not cfg.cliffhanger_enabled or len(chunks) < 2:
        return chunks, {}
    minimum = max(60.0, float(cfg.cliffhanger_min_seconds))
    maximum = max(minimum, float(cfg.cliffhanger_max_seconds))
    candidates = [
        {
            "boundary_index": chunk.index,
            "source_end": chunk.source_end,
            "output_end": chunk.output_start + chunk.duration,
            "summary_vi": chunk.summary_vi,
        }
        for chunk in chunks
        if minimum
        <= chunk.output_start + chunk.duration
        <= maximum + 0.01
    ]
    if not candidates:
        return chunks, {
            "enabled": True,
            "selected": False,
            "reason_vi": "Nguồn ngắn hơn thời lượng cliffhanger tối thiểu.",
        }
    fallback = candidates[-1]
    selection: dict = {}
    try:
        selection = select_cliffhanger_boundary(
            candidates=candidates,
            project_id=cfg.project_id,
            region=cfg.region,
            model=cfg.model,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[long] cliffhanger selection fallback: {exc}", flush=True)
    allowed = {int(item["boundary_index"]): item for item in candidates}
    requested_index = int(selection.get("boundary_index", -1))
    selected = allowed.get(requested_index, fallback)
    selected_index = int(selected["boundary_index"])
    kept = [chunk for chunk in chunks if chunk.index <= selected_index]
    metadata = {
        "enabled": True,
        "selected": True,
        "boundary_index": selected_index,
        "source_end": float(selected["source_end"]),
        "output_end": float(selected["output_end"]),
        "window_min_seconds": minimum,
        "window_max_seconds": maximum,
        "title_vi": str(selection.get("title_vi") or "").strip(),
        "reason_vi": str(selection.get("reason_vi") or "").strip(),
        "confidence": float(selection.get("confidence") or 0.0),
        "fallback_used": requested_index not in allowed,
    }
    print(
        "[long] cliffhanger boundary selected: "
        f"chunk={selected_index} source_end={metadata['source_end']:.1f}s "
        f"confidence={metadata['confidence']:.2f}",
        flush=True,
    )
    return kept, metadata


def _short_cfg(cfg: LongPipelineConfig) -> PipelineConfig:
    return PipelineConfig(
        project_id=cfg.project_id,
        region=cfg.region,
        scratch_gs_prefix=cfg.scratch_prefix,
        speed=1.0,
        enable_bgm=True,
        enable_subtitles=True,
        strict_subtitle_ocr=False,
        align_dub_to_speech=True,
        dub_max_speed=cfg.dub_max_speed,
        dub_hard_max_speed=cfg.dub_hard_max_speed,
        dub_end_microfit_max_speed=cfg.dub_end_microfit_max_speed,
        dub_tail_pad_seconds=cfg.dub_tail_pad_seconds,
        tts_provider=cfg.tts_provider,
        tts_fallback_provider=cfg.tts_fallback_provider,
        tts_voice=cfg.tts_voice,
        google_tts_voice=cfg.google_tts_voice,
        speaking_rate=cfg.speaking_rate,
        capcut_resource_id=cfg.capcut_resource_id,
        capcut_device_json=cfg.capcut_device_json,
        capcut_poll_timeout=cfg.capcut_poll_timeout,
        gemini_model=cfg.model,
        cookie=cfg.cookie,
    )


def _dialogue_script(lines: list[LongDialogueLine]) -> ScriptResult:
    return ScriptResult(
        dialogue=[
            DialogueLine(start=line.start, end=line.end, text_vi=line.text_vi)
            for line in lines
        ]
    )


# Đuôi chunk thiếu thoại quá ngưỡng này (giây) → phân tích lại đoạn còn thiếu.
def _assert_no_large_output_gaps(
    lines: list[LongDialogueLine],
    *,
    index: int,
    source_lines: list[LongDialogueLine] | None = None,
) -> None:
    gaps = dialogue_gap_ranges(lines, min_gap=_OUTPUT_GAP_FAIL_SECONDS)
    if not gaps:
        return
    source = merge_dialogue_lines(source_lines or [])
    if source and not _output_gaps_have_source_dialogue(gaps, source):
        gap_text = ", ".join(f"{start:.1f}-{end:.1f}s" for start, end in gaps[:5])
        print(
            "[long] output gap warning "
            f"chunk {index}: {gap_text}; no source dialogue line covers gap",
            flush=True,
        )
        return
    gap_text = ", ".join(f"{start:.1f}-{end:.1f}s" for start, end in gaps[:5])
    raise RuntimeError(
        f"chunk {index} still has uncovered subtitle/dub gap(s): {gap_text}"
    )


def _output_gaps_have_source_dialogue(
    gaps: list[tuple[float, float]],
    source_lines: list[LongDialogueLine],
) -> bool:
    """Return True when a post-TTS gap likely lost a real analyzed dialogue line."""
    for gap_start, gap_end in gaps:
        for line in source_lines:
            overlap = min(gap_end, float(line.end)) - max(gap_start, float(line.start))
            midpoint = (float(line.start) + float(line.end)) / 2.0
            if overlap >= 1.0 or (gap_start < midpoint < gap_end):
                return True
    return False


_COVERAGE_RETRY_MIN_GAP = 25.0
_INTERNAL_GAP_RETRY_MIN_GAP = 12.0
_VISUAL_GAP_RETRY_MIN_GAP = 18.0
_GAP_RETRY_WINDOW_SECONDS = 8.0
_GAP_RETRY_OVERLAP_SECONDS = 1.5
_GAP_RETRY_MAX_LINE_SECONDS = 12.0
_GAP_RETRY_MAX_WINDOW_RATIO = 0.85
_LONG_LINE_REFINE_MIN_SECONDS = 14.0
_OUTPUT_GAP_FAIL_SECONDS = 25.0
_FINAL_TAIL_REFRESH_SECONDS = 14.0
_FINAL_TAIL_REFRESH_MIN_SECONDS = 6.0
_FINAL_TAIL_REPLACE_LEAD_SECONDS = 0.2
_VISUAL_REFRESH_WINDOW_SECONDS = 8.0
_VISUAL_REFRESH_OVERLAP_SECONDS = 0.75


def _line_signature(lines: list[LongDialogueLine]) -> tuple[tuple[float, float, str, str], ...]:
    return tuple(
        (
            round(float(line.start), 2),
            round(float(line.end), 2),
            line.text_zh.strip(),
            line.text_vi.strip(),
        )
        for line in merge_dialogue_lines(lines)
    )


def _visual_refresh_enabled(cfg: LongPipelineConfig, *, suspect: bool) -> bool:
    mode = cfg.visual_refresh.strip().lower()
    if mode in ("1", "true", "yes", "on", "always"):
        return True
    if mode in ("0", "false", "no", "off", "never"):
        return False
    return suspect


def _analyze_tail_lines(
    raw: str,
    *,
    chunk_start: float,
    chunk_end: float,
    covered_end: float,
    cfg: LongPipelineConfig,
    chunk_dir: str,
    run_key: str,
    index: int,
) -> list[LongDialogueLine]:
    """Phân tích lại phần đuôi chunk Gemini bỏ sót. Trả lines theo hệ chunk-relative."""
    overlap = 2.0
    tail_src_start = max(chunk_start, chunk_start + covered_end - overlap)
    if chunk_end - tail_src_start < 5.0:
        return []
    tail_clip = os.path.join(chunk_dir, "tail.mp4")
    ffmpeg_ops.cut_segment(raw, tail_clip, start=tail_src_start, end=chunk_end)
    clip_uri = (
        f"{cfg.scratch_prefix.rstrip('/')}/{run_key}/chunk-{index:03d}-tail.mp4"
    )
    gcsio.upload(tail_clip, clip_uri, content_type="video/mp4")
    try:
        tail_lines, _ = analyze_chunk(
            clip_uri=clip_uri,
            project_id=cfg.project_id,
            region=cfg.region,
            model=cfg.model,
        )
    finally:
        gcsio.delete(clip_uri)
    base = tail_src_start - chunk_start  # offset clip-tail -> chunk
    chunk_duration = chunk_end - chunk_start
    extra: list[LongDialogueLine] = []
    for line in tail_lines:
        start = base + float(line.start)
        end = base + float(line.end)
        if end <= covered_end + 0.2:  # bỏ phần đã có
            continue
        start = max(covered_end, min(start, chunk_duration))
        end = min(chunk_duration, end)
        if end > start:
            extra.append(
                LongDialogueLine(
                    start=start, end=end, text_zh=line.text_zh, text_vi=line.text_vi
                )
            )
    return extra


def _analyze_gap_window_lines(
    raw: str,
    *,
    chunk_start: float,
    gap_start: float,
    gap_end: float,
    retry_start: float,
    retry_end: float,
    accept_start: float,
    accept_end: float,
    cfg: LongPipelineConfig,
    chunk_dir: str,
    run_key: str,
    index: int,
    gap_index: int,
    window_index: int,
    visual: bool = False,
) -> list[LongDialogueLine]:
    """Analyze one small internal-gap window and return chunk-relative lines."""
    if retry_end - retry_start < 5.0:
        return []

    gap_clip = os.path.join(chunk_dir, f"gap_{gap_index:02d}_{window_index:02d}.mp4")
    ffmpeg_ops.cut_segment(
        raw,
        gap_clip,
        start=chunk_start + retry_start,
        end=chunk_start + retry_end,
    )
    clip_uri = (
        f"{cfg.scratch_prefix.rstrip('/')}/{run_key}/"
        f"chunk-{index:03d}-gap-{gap_index:02d}-{window_index:02d}.mp4"
    )
    gcsio.upload(gap_clip, clip_uri, content_type="video/mp4")
    try:
        analyzer = analyze_visual_gap_chunk if visual else analyze_gap_chunk
        gap_lines, _ = analyzer(
            clip_uri=clip_uri,
            project_id=cfg.project_id,
            region=cfg.region,
            model=cfg.model,
        )
    finally:
        gcsio.delete(clip_uri)

    extra: list[LongDialogueLine] = []
    window_duration = retry_end - retry_start
    for line in gap_lines:
        start = retry_start + float(line.start)
        end = retry_start + float(line.end)
        midpoint = (start + end) / 2.0
        if midpoint <= accept_start + 0.15 or midpoint >= accept_end - 0.15:
            continue
        start = max(gap_start, min(start, gap_end))
        end = min(gap_end, max(end, gap_start))
        duration = end - start
        if duration > _GAP_RETRY_MAX_LINE_SECONDS:
            print(
                "[long] gap retry rejected long line "
                f"chunk {index}: duration={duration:.1f}s text={line.text_vi[:50]!r}",
                flush=True,
            )
            continue
        if (
            window_duration >= 8.0
            and duration / window_duration > _GAP_RETRY_MAX_WINDOW_RATIO
        ):
            print(
                "[long] gap retry rejected window-sized line "
                f"chunk {index}: duration={duration:.1f}s "
                f"window={window_duration:.1f}s text={line.text_vi[:50]!r}",
                flush=True,
            )
            continue
        if end > start:
            extra.append(
                LongDialogueLine(
                    start=start,
                    end=end,
                    text_zh=line.text_zh,
                    text_vi=line.text_vi,
                )
            )
    return extra


def _recover_internal_gap_lines(
    raw: str,
    *,
    chunk_start: float,
    chunk_end: float,
    lines: list[LongDialogueLine],
    cfg: LongPipelineConfig,
    chunk_dir: str,
    run_key: str,
    index: int,
) -> list[LongDialogueLine]:
    merged = merge_dialogue_lines(lines)
    gaps = dialogue_gap_ranges(merged, min_gap=_INTERNAL_GAP_RETRY_MIN_GAP)
    if gaps:
        merged = merge_dialogue_lines(
            merged
            + _retry_gap_windows(
                raw,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                gaps=gaps,
                cfg=cfg,
                chunk_dir=chunk_dir,
                run_key=run_key,
                index=index,
                visual=False,
            )
        )

    remaining = dialogue_gap_ranges(merged, min_gap=_VISUAL_GAP_RETRY_MIN_GAP)
    if remaining:
        merged = merge_dialogue_lines(
            merged
            + _retry_gap_windows(
                raw,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                gaps=remaining,
                cfg=cfg,
                chunk_dir=chunk_dir,
                run_key=run_key,
                index=index,
                visual=True,
            )
        )
    return merged


def _retry_gap_windows(
    raw: str,
    *,
    chunk_start: float,
    chunk_end: float,
    gaps: list[tuple[float, float]],
    cfg: LongPipelineConfig,
    chunk_dir: str,
    run_key: str,
    index: int,
    visual: bool,
) -> list[LongDialogueLine]:
    recovered: list[LongDialogueLine] = []
    mode = "visual" if visual else "audio"
    chunk_duration = chunk_end - chunk_start
    for gap_index, (gap_start, gap_end) in enumerate(gaps):
        print(
            "[long] gap retry "
            f"{mode} chunk {index}: {gap_start:.1f}-{gap_end:.1f}s "
            f"gap={gap_end - gap_start:.1f}s",
            flush=True,
        )
        gap_lines: list[LongDialogueLine] = []
        cursor = gap_start
        window_index = 0
        while cursor < gap_end - 0.5:
            accept_start = cursor
            accept_end = min(gap_end, cursor + _GAP_RETRY_WINDOW_SECONDS)
            retry_start = max(0.0, accept_start - _GAP_RETRY_OVERLAP_SECONDS)
            retry_end = min(chunk_duration, accept_end + _GAP_RETRY_OVERLAP_SECONDS)
            try:
                gap_lines.extend(
                    _analyze_gap_window_lines(
                        raw,
                        chunk_start=chunk_start,
                        gap_start=gap_start,
                        gap_end=gap_end,
                        retry_start=retry_start,
                        retry_end=retry_end,
                        accept_start=accept_start,
                        accept_end=accept_end,
                        cfg=cfg,
                        chunk_dir=chunk_dir,
                        run_key=run_key,
                        index=index,
                        gap_index=gap_index,
                        window_index=window_index,
                        visual=visual,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - supplemental analysis must not fail chunk
                print(
                    "[long] gap retry failed "
                    f"{mode} chunk {index} window {window_index}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
            cursor = accept_end
            window_index += 1
        if gap_lines:
            print(
                "[long] gap retry recovered "
                f"{mode} chunk {index}: {len(gap_lines)} line(s)",
                flush=True,
            )
            recovered.extend(gap_lines)
    return recovered


def _refresh_visual_subtitle_lines(
    raw: str,
    *,
    chunk_start: float,
    chunk_end: float,
    lines: list[LongDialogueLine],
    cfg: LongPipelineConfig,
    chunk_dir: str,
    run_key: str,
    index: int,
) -> list[LongDialogueLine]:
    """Sweep visible captions in short windows and prefer them over same-time guesses."""
    chunk_duration = chunk_end - chunk_start
    visual_lines: list[LongDialogueLine] = []
    cursor = 0.0
    window_index = 0
    while cursor < chunk_duration - 0.5:
        accept_start = cursor
        accept_end = min(chunk_duration, cursor + _VISUAL_REFRESH_WINDOW_SECONDS)
        retry_start = max(0.0, accept_start - _VISUAL_REFRESH_OVERLAP_SECONDS)
        retry_end = min(chunk_duration, accept_end + _VISUAL_REFRESH_OVERLAP_SECONDS)
        try:
            visual_lines.extend(
                _analyze_gap_window_lines(
                    raw,
                    chunk_start=chunk_start,
                    gap_start=accept_start,
                    gap_end=accept_end,
                    retry_start=retry_start,
                    retry_end=retry_end,
                    accept_start=accept_start,
                    accept_end=accept_end,
                    cfg=cfg,
                    chunk_dir=chunk_dir,
                    run_key=run_key,
                    index=index,
                    gap_index=99,
                    window_index=window_index,
                    visual=True,
                )
            )
        except Exception as exc:  # noqa: BLE001 - supplemental visual refresh must not fail chunk
            print(
                "[long] visual refresh failed "
                f"chunk {index} window {window_index}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
        cursor = accept_end
        window_index += 1

    visual_lines = merge_dialogue_lines(visual_lines)
    if not visual_lines:
        return merge_dialogue_lines(lines)

    before = _line_signature(lines)
    refreshed = prefer_visual_dialogue_lines(lines, visual_lines)
    after = _line_signature(refreshed)
    if after != before:
        print(
            "[long] visual refresh chunk "
            f"{index}: replaced with {len(visual_lines)} visual line(s)",
            flush=True,
        )
    return refreshed


def _refine_long_lines(
    raw: str,
    *,
    chunk_start: float,
    chunk_end: float,
    lines: list[LongDialogueLine],
    cfg: LongPipelineConfig,
    chunk_dir: str,
    run_key: str,
    index: int,
) -> list[LongDialogueLine]:
    refined: list[LongDialogueLine] = []
    for line in merge_dialogue_lines(lines):
        duration = line.end - line.start
        if duration <= _LONG_LINE_REFINE_MIN_SECONDS:
            refined.append(line)
            continue

        span = [(line.start, line.end)]
        print(
            "[long] refine long line "
            f"chunk {index}: {line.start:.1f}-{line.end:.1f}s "
            f"duration={duration:.1f}s text={line.text_vi[:50]!r}",
            flush=True,
        )
        candidates = _retry_gap_windows(
            raw,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            gaps=span,
            cfg=cfg,
            chunk_dir=chunk_dir,
            run_key=run_key,
            index=index,
            visual=False,
        )
        candidates = merge_dialogue_lines(
            candidates
            + _retry_gap_windows(
                raw,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                gaps=span,
                cfg=cfg,
                chunk_dir=chunk_dir,
                run_key=run_key,
                index=index,
                visual=True,
            )
        )
        if candidates:
            print(
                "[long] refined long line "
                f"chunk {index}: {len(candidates)} replacement line(s)",
                flush=True,
            )
            refined.extend(candidates)
        else:
            refined.append(line)
    return merge_dialogue_lines(refined)


def _refresh_final_tail_lines(
    raw: str,
    *,
    chunk_start: float,
    chunk_end: float,
    lines: list[LongDialogueLine],
    cfg: LongPipelineConfig,
    chunk_dir: str,
    run_key: str,
    index: int,
) -> list[LongDialogueLine]:
    """Re-analyze the final seconds so the last spoken lines are not guessed."""
    chunk_duration = chunk_end - chunk_start
    tail_seconds = min(_FINAL_TAIL_REFRESH_SECONDS, chunk_duration)
    if tail_seconds < _FINAL_TAIL_REFRESH_MIN_SECONDS:
        return merge_dialogue_lines(lines)

    tail_start = max(0.0, chunk_duration - tail_seconds)
    tail_clip = os.path.join(chunk_dir, "final_tail.mp4")
    ffmpeg_ops.cut_segment(
        raw,
        tail_clip,
        start=chunk_start + tail_start,
        end=chunk_end,
    )
    clip_uri = (
        f"{cfg.scratch_prefix.rstrip('/')}/{run_key}/"
        f"chunk-{index:03d}-final-tail.mp4"
    )
    gcsio.upload(tail_clip, clip_uri, content_type="video/mp4")

    candidates: list[LongDialogueLine] = []
    try:
        analyzers = (
            ("audio", analyze_gap_chunk),
            ("visual", analyze_visual_gap_chunk),
        )
        for mode, analyzer in analyzers:
            try:
                tail_lines, _ = analyzer(
                    clip_uri=clip_uri,
                    project_id=cfg.project_id,
                    region=cfg.region,
                    model=cfg.model,
                )
            except Exception as exc:  # noqa: BLE001 - tail refresh is supplemental
                print(
                    "[long] final tail refresh failed "
                    f"{mode} chunk {index}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue

            accepted = 0
            for line in tail_lines:
                start = tail_start + float(line.start)
                end = tail_start + float(line.end)
                midpoint = (start + end) / 2.0
                if midpoint < tail_start + _FINAL_TAIL_REPLACE_LEAD_SECONDS:
                    continue
                start = max(tail_start, min(start, chunk_duration))
                end = min(chunk_duration, max(end, tail_start))
                if end <= start:
                    continue
                if end - start > _GAP_RETRY_MAX_LINE_SECONDS:
                    print(
                        "[long] final tail refresh rejected long line "
                        f"chunk {index}: duration={end - start:.1f}s "
                        f"text={line.text_vi[:50]!r}",
                        flush=True,
                    )
                    continue
                candidates.append(
                    LongDialogueLine(
                        start=start,
                        end=end,
                        text_zh=line.text_zh,
                        text_vi=line.text_vi,
                    )
                )
                accepted += 1
            if accepted:
                print(
                    "[long] final tail refresh recovered "
                    f"{mode} chunk {index}: {accepted} line(s)",
                    flush=True,
                )
    finally:
        gcsio.delete(clip_uri)

    refreshed = replace_tail_dialogue_lines(
        lines,
        candidates,
        tail_start=tail_start,
        lead_seconds=_FINAL_TAIL_REPLACE_LEAD_SECONDS,
    )
    if refreshed != merge_dialogue_lines(lines):
        old_tail_count = sum(
            1
            for line in merge_dialogue_lines(lines)
            if (line.start + line.end) / 2.0
            >= tail_start + _FINAL_TAIL_REPLACE_LEAD_SECONDS
        )
        new_tail_count = sum(
            1
            for line in refreshed
            if (line.start + line.end) / 2.0
            >= tail_start + _FINAL_TAIL_REPLACE_LEAD_SECONDS
        )
        print(
            "[long] final tail refresh chunk "
            f"{index}: replaced {old_tail_count} tail line(s) with "
            f"{new_tail_count} line(s)",
            flush=True,
        )
    return refreshed


def process_source(
    *,
    douyin_url: str,
    source_index: int,
    output_uri: str,
    cfg: LongPipelineConfig,
) -> SourceResult:
    work = tempfile.mkdtemp(prefix="long_source_")
    run_key = uuid.uuid4().hex[:12]
    raw = os.path.join(work, "raw.mp4")
    aweme_id = _load_or_download_source(
        douyin_url=douyin_url,
        raw_path=raw,
        cfg=cfg,
    )
    source_duration = ffmpeg_ops.duration_seconds(raw)
    source_start, processing_end = _bounded_source_range(
        source_duration,
        cfg.source_start_seconds,
        cfg.source_max_seconds,
    )
    processing_duration = processing_end - source_start
    if source_start > 0.01 or processing_end < source_duration - 0.01:
        print(
            "[long] source processing range: "
            f"original={source_duration:.1f}s "
            f"start={source_start:.1f}s end={processing_end:.1f}s",
            flush=True,
        )
    processed_chunks: list[str] = []
    chunk_results: list[ChunkResult] = []
    summaries: list[str] = []
    output_cursor = 0.0

    relative_ranges = chunk_ranges(
        processing_duration,
        cfg.chunk_seconds,
        min_chunk_seconds=cfg.min_chunk_seconds,
        max_chunk_seconds=cfg.max_chunk_seconds,
    )
    ranges = [
        (source_start + start, source_start + end)
        for start, end in relative_ranges
    ]
    for index, (start, end) in enumerate(ranges):
        chunk_dir = os.path.join(work, f"chunk_{index:03d}")
        os.makedirs(chunk_dir, exist_ok=True)
        analysis_chunk = os.path.join(chunk_dir, "analysis.mp4")
        rendered = os.path.join(chunk_dir, "rendered.mp4")
        bgm = os.path.join(chunk_dir, "background.wav")
        output = os.path.join(chunk_dir, "final.mp4")
        ass = os.path.join(chunk_dir, "subtitles.ass")

        analysis_start = max(0.0, start - cfg.analysis_padding_seconds)
        analysis_end = min(processing_end, end + cfg.analysis_padding_seconds)
        ffmpeg_ops.cut_segment(
            raw,
            analysis_chunk,
            start=analysis_start,
            end=analysis_end,
        )
        clip_uri = (
            f"{cfg.scratch_prefix.rstrip('/')}/{run_key}/"
            f"chunk-{index:03d}.mp4"
        )
        gcsio.upload(analysis_chunk, clip_uri, content_type="video/mp4")
        try:
            padded_lines, summary = analyze_chunk(
                clip_uri=clip_uri,
                project_id=cfg.project_id,
                region=cfg.region,
                model=cfg.model,
            )
        finally:
            gcsio.delete(clip_uri)

        chunk_duration = end - start
        summary = normalize_long_vi_text(summary)
        lines = merge_dialogue_lines(
            select_chunk_lines(
                padded_lines,
                analysis_start=analysis_start,
                chunk_start=start,
                chunk_end=end,
            )
        )
        # Coverage guard: nếu Gemini bỏ đuôi chunk → phân tích lại phần thiếu.
        if lines:
            pre_repair_signature = _line_signature(lines)
            lines = _refine_long_lines(
                raw,
                chunk_start=start,
                chunk_end=end,
                lines=lines,
                cfg=cfg,
                chunk_dir=chunk_dir,
                run_key=run_key,
                index=index,
            )
            lines = _recover_internal_gap_lines(
                raw,
                chunk_start=start,
                chunk_end=end,
                lines=lines,
                cfg=cfg,
                chunk_dir=chunk_dir,
                run_key=run_key,
                index=index,
            )
            repair_changed = _line_signature(lines) != pre_repair_signature
            if _visual_refresh_enabled(cfg, suspect=repair_changed):
                lines = _refresh_visual_subtitle_lines(
                    raw,
                    chunk_start=start,
                    chunk_end=end,
                    lines=lines,
                    cfg=cfg,
                    chunk_dir=chunk_dir,
                    run_key=run_key,
                    index=index,
                )
            covered_end = max(line.end for line in lines)
            if (chunk_duration - covered_end) > _COVERAGE_RETRY_MIN_GAP:
                try:
                    lines = merge_dialogue_lines(
                        lines
                        + _analyze_tail_lines(
                            raw,
                            chunk_start=start,
                            chunk_end=end,
                            covered_end=covered_end,
                            cfg=cfg,
                            chunk_dir=chunk_dir,
                            run_key=run_key,
                            index=index,
                        )
                    )
                except Exception:  # noqa: BLE001 - retry lỗi không làm fail chunk
                    pass
            if end >= processing_end - 0.01:
                lines = _refresh_final_tail_lines(
                    raw,
                    chunk_start=start,
                    chunk_end=end,
                    lines=lines,
                    cfg=cfg,
                    chunk_dir=chunk_dir,
                    run_key=run_key,
                    index=index,
                )
        deduped_lines = suppress_tail_repeated_dialogue_lines(
            lines,
            chunk_duration=chunk_duration,
        )
        if len(deduped_lines) != len(merge_dialogue_lines(lines)):
            print(
                "[long] suppressed repeated tail line "
                f"chunk {index}: {len(merge_dialogue_lines(lines))} -> "
                f"{len(deduped_lines)} line(s)",
                flush=True,
            )
        lines = normalize_long_dialogue_lines(deduped_lines)
        ffmpeg_ops.extract_audio(raw, bgm, start=start, end=end)
        subtitle_ass: str | None = None
        voices: list[tuple[str, int]] = []
        voice_timeline_end = 0.0
        output_lines = lines
        subtitle_after_pad = False
        if lines:
            script = _dialogue_script(lines)
            # Lồng tiếng TRƯỚC để biết vị trí giọng thật, rồi dóng phụ đề theo nó
            # (giống short) — tránh sub lệch giọng + tách câu theo dấu câu.
            voices, voice_timeline_end, voice_sync = _synthesize_timed_voice_clips(
                script=script,
                work=chunk_dir,
                video_dur=chunk_duration,
                cfg=_short_cfg(cfg),
            )
            subtitle_after_pad = voice_timeline_end > chunk_duration + 0.01
            subtitle_dur = max(chunk_duration, voice_timeline_end)
            output_lines = voice_synced_dialogue_lines(
                lines,
                voice_sync,
                video_dur=subtitle_dur,
            )
            # Sau khi voice đã synth, mọi dòng đều có tiếng trong audio —
            # không dedup/drop nữa, chỉ được co/giãn thời gian hiển thị.
            output_lines = non_overlapping_dialogue_lines(
                output_lines,
                dedup=False,
                drop_short=False,
            )
            output_lines = readable_short_subtitle_lines(
                output_lines,
                video_dur=subtitle_dur,
                dedup=False,
                drop_short=False,
            )
            if not subtitle_after_pad:
                _assert_no_large_output_gaps(
                    output_lines,
                    index=index,
                    source_lines=lines,
                )
                ffmpeg_ops.write_ass_subtitles(
                    _dialogue_script(output_lines).dialogue,
                    ass,
                    speed=1.0,
                    video_dur=chunk_duration,
                    font_size=54,
                    margin_v=60,
                    exact_timing=True,
                    sequential_punctuation=True,
                    play_res_x=cfg.video_width,
                    play_res_y=cfg.video_height,
                )
                subtitle_ass = ass
        ffmpeg_ops.render_landscape_segment(
            raw,
            rendered,
            start=start,
            end=end,
            ass_path=subtitle_ass,
            width=cfg.video_width,
            height=cfg.video_height,
            crf=cfg.video_crf,
        )
        video_for_mux = rendered
        final_dur = ffmpeg_ops.duration_seconds(rendered)
        if voice_timeline_end > final_dur + 0.01:
            overflow = voice_timeline_end - final_dur
            max_pad = max(0.0, cfg.dub_tail_pad_seconds)
            pad_seconds = min(max_pad, overflow + 0.08)
            if overflow - pad_seconds > 0.05:
                raise RuntimeError(
                    f"Vietnamese dub ends at {voice_timeline_end:.3f}s, beyond "
                    f"rendered chunk duration {final_dur:.3f}s; tail pad limit "
                    f"{max_pad:.3f}s is not enough"
                )
            padded = os.path.join(chunk_dir, "tail_padded.mp4")
            ffmpeg_ops.pad_video_tail(
                video_for_mux,
                padded,
                extra_seconds=pad_seconds,
                crf=cfg.video_crf,
            )
            final_dur = ffmpeg_ops.duration_seconds(padded)
            video_for_mux = padded
            print(
                "[long] tail-pad chunk "
                f"{index}: overflow={overflow:.3f}s pad={pad_seconds:.3f}s "
                f"final_dur={final_dur:.3f}s",
                flush=True,
            )
        if lines and subtitle_after_pad:
            output_lines = voice_synced_dialogue_lines(
                lines,
                voice_sync,
                video_dur=final_dur,
            )
            output_lines = non_overlapping_dialogue_lines(
                output_lines,
                dedup=False,
                drop_short=False,
            )
            output_lines = readable_short_subtitle_lines(
                output_lines,
                video_dur=final_dur,
                dedup=False,
                drop_short=False,
            )
            _assert_no_large_output_gaps(
                output_lines,
                index=index,
                source_lines=lines,
            )
            ffmpeg_ops.write_ass_subtitles(
                _dialogue_script(output_lines).dialogue,
                ass,
                speed=1.0,
                video_dur=final_dur,
                font_size=54,
                margin_v=60,
                exact_timing=True,
                sequential_punctuation=True,
                play_res_x=cfg.video_width,
                play_res_y=cfg.video_height,
            )
            subtitled = os.path.join(chunk_dir, "tail_padded_subtitled.mp4")
            ffmpeg_ops.burn_subtitles(
                video_for_mux,
                ass,
                subtitled,
                crf=cfg.video_crf,
            )
            video_for_mux = subtitled
            final_dur = ffmpeg_ops.duration_seconds(subtitled)
        ffmpeg_ops.mux_synced(
            video_for_mux,
            voices,
            bgm,
            output,
            speed=1.0,
            bgm_gain_db=cfg.bgm_gain_db,
            final_dur=final_dur,
            max_microfit_speed=cfg.dub_end_microfit_max_speed,
        )
        output_duration = ffmpeg_ops.duration_seconds(output)
        processed_chunks.append(output)
        summaries.append(summary)
        chunk_results.append(
            ChunkResult(
                index=index,
                source_start=start,
                source_end=end,
                duration=output_duration,
                output_start=output_cursor,
                summary_vi=summary,
                lines=output_lines,
            )
        )
        output_cursor += output_duration

    selected_chunks, cliffhanger = _select_cliffhanger_chunks(
        chunk_results,
        cfg=cfg,
    )
    if len(selected_chunks) != len(chunk_results):
        keep_count = len(selected_chunks)
        processed_chunks = processed_chunks[:keep_count]
        summaries = summaries[:keep_count]
        chunk_results = selected_chunks

    local_final = os.path.join(work, "final.mp4")
    ffmpeg_ops.concat_mp4(processed_chunks, local_final)
    gcsio.upload(local_final, output_uri, content_type="video/mp4")
    metadata_uri = output_uri.rsplit(".", 1)[0] + ".json"
    subtitle_uri = output_uri.rsplit(".", 1)[0] + ".srt"
    result = SourceResult(
        source_index=source_index,
        douyin_url=douyin_url,
        aweme_id=aweme_id,
        duration=ffmpeg_ops.duration_seconds(local_final),
        output_uri=output_uri,
        metadata_uri=metadata_uri,
        subtitle_uri=subtitle_uri,
        title_vi=summaries[0][:80] if summaries else f"Phần {source_index + 1}",
        description_vi=" ".join(item for item in summaries if item),
        chunks=chunk_results,
        source_duration=source_duration,
        source_start=source_start,
        source_processed_end=(
            chunk_results[-1].source_end if chunk_results else source_start
        ),
        cliffhanger=cliffhanger,
    )
    if cliffhanger.get("title_vi"):
        result.title_vi = str(cliffhanger["title_vi"])[:80]
    metadata_path = os.path.join(work, "final.json")
    result_dict = normalize_long_text_value(result.to_dict())
    with open(metadata_path, "w", encoding="utf-8") as handle:
        json.dump(result_dict, handle, ensure_ascii=False, indent=2)
    gcsio.upload(metadata_path, metadata_uri, content_type="application/json")
    subtitle_path = os.path.join(work, "final.srt")
    with open(subtitle_path, "w", encoding="utf-8") as handle:
        write_vietnamese_srt(
            handle,
            (
                (
                    chunk.output_start + line.start,
                    chunk.output_start + line.end,
                    line.text_vi,
                )
                for chunk in chunk_results
                for line in chunk.lines
            ),
        )
    gcsio.upload(subtitle_path, subtitle_uri, content_type="application/x-subrip")
    return result
