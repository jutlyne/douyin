# container_short — Douyin → YouTube Short (tiếng Việt)

Pipeline biến 1 video Douyin thành **YouTube Short tiếng Việt**, chạy dưới dạng **Cloud Run Job**
(mirror `container_batch/` của Whisper). n8n trigger job bằng env vars → job ghi `final.mp4` + metadata
về GCS → n8n tải về và upload YouTube.

## Pipeline (xem `pipeline.py`)
1. **Download** video Douyin (reuse `douyin_api/douyin_downloader.py`).
2. **Upload tạm** mp4 lên GCS (`SCRATCH_GS_PREFIX`) để Gemini đọc qua `gs://`.
3. **Gemini** (Vertex) xem video → JSON `{highlight[start,end], script_vi, title_vi, description_vi, hashtags}`.
4. **ffmpeg**: cắt highlight + `hflip` + tăng tốc `1.04x` + **blur-pad 1080×1920 H.264** (không audio).
5. **Demucs** (`--two-stems=vocals`) trên audio đoạn cắt → `no_vocals.wav` (giữ nhạc nền, bỏ tiếng Trung).
6. **Cloud TTS** giọng VN → `voice.mp3`, khít thời lượng clip.
7. **Mix + mux**: voice (chủ đạo) + BGM (~ `-16 dB`, ≈12%) → AAC stereo 44100, clamp **40–55s**, verify `<60s`.
8. Upload `final.mp4` + `final.json` (title/desc/hashtags) lên `OUTPUT_URI`.

Đầu ra đảm bảo spec: 9:16 1080×1920, H.264/.mp4, AAC stereo 44100Hz, 40–55s, voice VN, không còn tiếng Trung.

## Env vars (Job)
| Env | Bắt buộc | Mặc định | Mô tả |
|-----|:--:|--|--|
| `DOUYIN_URL` | ✅ | | n8n truyền lúc execute |
| `OUTPUT_URI` | ✅ | | `gs://.../final.mp4` |
| `SCRATCH_GS_PREFIX` | | `gs://YOUR_GCP_PROJECT-shorts/tmp` | mp4 tạm cho Gemini |
| `GCP_PROJECT_ID` | | `YOUR_GCP_PROJECT` | project Vertex/Gemini |
| `VERTEX_REGION` | | `us-central1` | |
| `TARGET_SECONDS` | | `48` | clamp 40–55 |
| `SPEED` | | `1.04` | 1.03–1.05 |
| `BGM_GAIN_DB` | | `-16` | ~12% |
| `ENABLE_BGM` | | `true` | tắt → voice-only |
| `ENABLE_SUBTITLES` | | `true` | burn phụ đề Việt theo speech timing |
| `SUBTITLE_MARGIN_V` | | `690` | vị trí sub tính từ đáy khung 1920px |
| `ENABLE_SUBTITLE_OCR` | | `true` | OCR phụ đề Trung |
| `SUBTITLE_OCR_FPS` | | `4.0` | số frame OCR mỗi giây |
| `SUBTITLE_SYNC_MODE` | | `ocr` | production dùng OCR 1–1 |
| `STRICT_SUBTITLE_OCR` | | `true` | dừng nếu OCR/dịch/timing không đạt |
| `SUBTITLE_OCR_PROVIDER` | | `gemini` | Gemini Vision OCR |
| `ALIGN_DUB_TO_SPEECH` | | `true` | căn sub và dub theo audio Trung |
| `DUB_MAX_SPEED` | | `1.35` | giới hạn tăng tốc TTS |
| `DUB_HARD_MAX_SPEED` | | `1.45` | cứu hộ riêng câu cuối khi hết timeline |
| `DUB_TAIL_HEADROOM_SECONDS` | | `1.0` | chừa hình cuối cho câu dịch cuối |
| `TTS_PROVIDER` | | `capcut` | provider lồng tiếng |
| `TTS_VOICE` | | `BV074_streaming` | giọng CapCut mặc định |
| `SPEAKING_RATE` | | `1.0` | tốc độ giọng |
| `GEMINI_MODEL` | | `gemini-2.5-flash` | |
| `DOUYIN_COOKIE` | | `` | nếu nội dung bị giới hạn |

## Build & deploy
```bash
# build (từ repo ROOT):
gcloud builds submit . --config=container_short/cloudbuild.job.yaml

# tạo job lần đầu:
gcloud run jobs create short-maker \
  --image us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/short:job \
  --region asia-southeast1 --memory 8Gi --cpu 4 --max-retries 1 --task-timeout 3600 \
  --set-env-vars GCP_PROJECT_ID=YOUR_GCP_PROJECT,VERTEX_REGION=us-central1,SCRATCH_GS_PREFIX=gs://YOUR_GCP_PROJECT-shorts/tmp

# cập nhật image về sau (giữ env):
gcloud run jobs update short-maker --region asia-southeast1 \
  --image us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/short:job
```

## Trigger từ n8n
Node HTTP Request gọi Cloud Run Admin API `jobs.run` (hoặc node gcloud), override env:
```
DOUYIN_URL = {{ $json.douyin_url }}
OUTPUT_URI = gs://YOUR_GCP_PROJECT-shorts/out/{{ $json.id }}/final.mp4
```
Đây là hai env duy nhất cần truyền theo mỗi execution. Các cấu hình CapCut, OCR, subtitle,
Gemini, project và scratch bucket đã có mặc định trong image; chỉ override khi cần thử nghiệm.
Sau khi job xong: tải `final.mp4` (node GCS) → node YouTube Upload. `final.json`
giữ các field cũ `title_vi`, `description_vi`, `hashtags` và có thêm hai field
sẵn để map trong n8n:

- `youtube_title`: tiêu đề sạch, không hashtag, tối đa 60 ký tự.
- `youtube_description`: tóm tắt 1–2 câu, sau đó là 3–6 hashtag ở dòng cuối.

## Test local (không qua cloud, vẫn cần service account + 1 bucket scratch)
```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/sa.json
python -m container_short.pipeline "https://v.douyin.com/xxx/" out.mp4 \
  --scratch gs://your-bucket/tmp --project your-proj --region us-central1
ffprobe out.mp4   # kiểm tra 1080x1920, h264, aac 44100 stereo, 40–55s
```
Yêu cầu local: `ffmpeg`/`ffprobe` trên PATH, `pip install -r container_short/requirements.txt`
(+ torch CPU), và `pip install -r douyin_api/requirements.txt`.

## Lưu ý
- Demucs + torch làm image nặng → Job nên cấp ≥4 CPU / 8Gi RAM. Không ảnh hưởng app trans (image tách biệt).
- Flip + speed lách Content ID **hạn chế**; nếu BGM gốc bị claim nhiều, đặt `ENABLE_BGM=false` (voice-only)
  hoặc bổ sung nhánh thay BGM riêng.
- Douyin đổi cấu trúc share-page → bảo trì `douyin_api/douyin_downloader.py`.
## CapCut TTS

CapCut là provider mặc định. Chỉ cần override các env sau khi muốn đổi giọng/cấu hình:

```bash
TTS_PROVIDER=capcut
TTS_VOICE=BV074_streaming
CAPCUT_RESOURCE_ID=7102355709945188865
SPEAKING_RATE=1.0
```

Optional:

```bash
CAPCUT_DEVICE_JSON=/path/to/device.json
CAPCUT_POLL_TIMEOUT=300
```

## Job OCR/subtitle tách biệt khỏi production

Phần OCR và burn subtitle được thử nghiệm bằng Cloud Run Job riêng:

- Production: `short-maker`, image `short:job`
- Development: `short-maker-subtitle-dev`, image `short:subtitle-dev`
- Output development bắt buộc nằm dưới
  `gs://YOUR_GCP_PROJECT-shorts/out/subtitle-dev/`

Build image development:

```bash
gcloud builds submit . \
  --config=container_short/cloudbuild.subtitle-job.yaml
```

Tạo job development lần đầu:

```bash
gcloud run jobs create short-maker-subtitle-dev \
  --image us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/short:subtitle-dev \
  --region asia-southeast1 \
  --memory 8Gi --cpu 4 --max-retries 1 --task-timeout 3600 \
  --set-env-vars GCP_PROJECT_ID=YOUR_GCP_PROJECT,VERTEX_REGION=us-central1,SCRATCH_GS_PREFIX=gs://YOUR_GCP_PROJECT-shorts/tmp/subtitle-dev,SUBTITLE_OUTPUT_PREFIX=gs://YOUR_GCP_PROJECT-shorts/out/subtitle-dev/
```

Chạy thử mà không đụng output production:

```bash
gcloud run jobs execute short-maker-subtitle-dev \
  --region asia-southeast1 \
  --update-env-vars DOUYIN_URL='https://v.douyin.com/xxx/',OUTPUT_URI=gs://YOUR_GCP_PROJECT-shorts/out/subtitle-dev/test-001/final.mp4
```

Entrypoint `container_short.subtitle_job` luôn ép bật:

```text
ENABLE_SUBTITLES=true
ENABLE_SUBTITLE_OCR=true
SUBTITLE_SYNC_MODE=ocr
STRICT_SUBTITLE_OCR=true
SUBTITLE_OCR_PROVIDER=gemini
SUBTITLE_TIME_OFFSET_SECONDS=0
ALIGN_DUB_TO_SPEECH=true
DUB_MAX_SPEED=1.35
DUB_TAIL_HEADROOM_SECONDS=1.0
```

Nếu `OUTPUT_URI` nằm ngoài `SUBTITLE_OUTPUT_PREFIX`, job dừng trước khi chạy
pipeline. Với n8n, dùng một runner/service development riêng và đặt
`JOB_NAME=short-maker-subtitle-dev`; không đổi biến `JOB_NAME` trên runner
production.

Ở chế độ strict development, Gemini Vision chỉ OCR nguyên văn phụ đề Trung
nhìn thấy trên video. Một request Gemini riêng sau đó chỉ dịch mảng OCR theo
index 1–1. Nếu OCR rỗng, thiếu câu hoặc kết quả dịch sai index, execution thất
bại thay vì fallback sang timeline Gemini.

Khi burn subtitle, job giữ nguyên ánh xạ 1–1 với cue Trung nhưng dùng mốc
`speech_start/speech_end` đã nghe từ audio. Text Việt chỉ được ngắt hiển thị
thành tối đa hai dòng và vẫn thuộc cùng một cue.

Job development dùng Gemini nghe audio Trung để căn `speech_start/speech_end`
cho đúng từng cue OCR. Subtitle Việt bắt đầu theo lời nói thay vì thời điểm chữ
Trung xuất hiện. Bản dịch phải giữ đầy đủ nội dung, không rút gọn để ép thời
lượng. TTS chỉ tăng tốc tối đa `DUB_MAX_SPEED=1.35`; câu dài được phép dùng
khoảng nghỉ sau câu, và câu kế tiếp sẽ lùi vừa đủ để hai giọng Việt không chồng
lên nhau. Trước khi fit, job bỏ silence ở cả đầu/cuối TTS và dùng WAV trung gian để tránh
MP3 encoder delay; đồng thời chừa tối đa 1 giây hình cuối để không cắt mất phần
dịch đầy đủ của câu cuối. `voice_sync` lưu cả `speech_start`, `actual_voice_start`,
`start_delay`, `spill_past_next_speech` và các duration để kiểm tra.
Ngoài `final.mp4` và `final.json`, job còn upload:

- `final.subtitles.json`: từng cue gồm timestamp, `text_zh` OCR và `text_vi`.
- `final.srt`: phụ đề Việt kèm dòng Trung để đối chiếu.

`TTS_VOICE` is the CapCut `voice_type`; `CAPCUT_RESOURCE_ID` is the matching `resource_id`
from `sample/voice.json`.
