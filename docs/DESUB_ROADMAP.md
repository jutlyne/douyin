# DESUB — Dịch vụ xoá sub cứng trên video Douyin

Tài liệu phân tích nghiệp vụ + lộ trình phát triển. Cập nhật lần cuối: 2026-07-06.

> **Nguyên tắc số 1: DESUB là flow ĐỘC LẬP HOÀN TOÀN với long/short.**
> Job riêng, service riêng, image riêng, bucket prefix riêng. Không sửa bất kỳ
> dòng code nào của `container_long/`, `container_short/`, `job_runner/`,
> `long_job_runner/`. Chỉ tái dùng (import read-only) các helper sẵn có:
> `douyin_api/douyin_downloader.py`, `container_short/steps/gcsio.py` và
> pattern HMAC download.

## 1. Bối cảnh & vấn đề

- Video nguồn Douyin (cả dạng manhua lẫn phim tư liệu người thật, ví dụ
  `https://v.douyin.com/p4u4LgNB3XE/` — 老祖宗的智慧, dạng 非遗/văn hoá) có
  **sub tiếng Trung burn cứng** ở band đáy khung hình.
- Nhu cầu: người dùng gửi **1 URL duy nhất** → hệ thống trả về **link tải
  video đã xoá sạch sub cứng**. Video sạch này sau đó có thể được đưa vào
  long flow để lồng tiếng + sub Việt **như quy trình hiện tại, không đổi gì**.
- Video mục tiêu dài ~5–7 phút, 720p–1080p.

## 2. Phạm vi

### In-scope (v1)
- API nhận 1 `douyin_url`, chạy bất đồng bộ, trả `download_url` + `status_uri`.
- Xoá text trong **band đáy** (sub thoại) + watermark tĩnh (mask cố định,
  bật/tắt bằng env). Chế độ `full` (xoá mọi text phát hiện được) để sau cờ env,
  mặc định tắt.
- Inpainting bằng GPU, chất lượng ưu tiên không nhấp nháy (temporal consistency)
  vì có nội dung nền thật chuyển động.
- Cache kết quả theo URL — cùng URL không tốn GPU lần 2.
- Callback n8n/Telegram (tuỳ chọn, cùng pattern long).

### Out-of-scope (v1) — để phase sau
- Tích hợp tự động vào long flow (worker render từ video sạch).
- Dùng `mask.json` (timing sub chính xác từng frame) để sync sub Việt.
- Xoá title/hiệu ứng chữ nghệ thuật giữa khung hình.
- Xử lý batch nhiều URL trong 1 request.

## 3. Luồng nghiệp vụ (user story)

> Là người vận hành kênh, tôi gửi 1 link Douyin cho hệ thống; sau khi xử lý
> xong tôi nhận được link tải video đã xoá sub cứng, để tôi tiếp tục quy trình
> lồng tiếng/sub Việt như hiện tại.

```text
User (n8n / Telegram / curl)
  │ POST /run { douyin_url }
  ▼
desub-job-runner   (Cloud Run service, CPU)
  │ 202 { desub_id, status_uri, download_url }     ← link HMAC + TTL, dùng được khi job xong
  │ jobs.run ↓
desub              (Cloud Run Job, GPU NVIDIA L4)
  1. Resolve + download video (tái dùng douyin_api)
  2. Cache-hit check theo url_hash → có thì copy, bỏ qua GPU
  3. Text detection (detection-only, sample ~10 fps, vùng theo DESUB_REGION)
  4. Gom box theo thời gian (IoU tracking) → mask ổn định từng span sub, dilate ~10px
  5. Inpaint GPU trên dải crop quanh mask (STTN | LaMa, env DESUB_MODEL —
     ProPainter bị LOẠI vì license, xem mục 8)
  6. Overlay dải đã vá lên video gốc, encode 1 pass (CRF 18)
  7. Upload clean.mp4 + mask.json + status.json → callback nếu được bật
```

## 4. Yêu cầu chức năng (FR)

| # | Yêu cầu |
|---|---------|
| FR1 | `POST /run` nhận `douyin_url` (bắt buộc), `id`, `callback_enabled`, `callback_url`, `chat_id` (tuỳ chọn); auth bằng `X-API-Key`. |
| FR2 | Trả 202 ngay với `desub_id`, `status_uri`, `output_uri`, `download_url` (HMAC, TTL), `operation`. |
| FR3 | Job ghi `status.json` các trạng thái: `queued → downloading → detecting → inpainting → encoding → completed | failed` (kèm `error`). |
| FR4 | `GET /download` stream file từ GCS, verify chữ ký + hạn như long-job-runner. |
| FR5 | Cache: `_cache/v1/{url_hash}/clean.mp4`; cùng URL (đã normalize) trả kết quả cache, có `force_refresh` để chạy lại. |
| FR6 | Callback POST JSON khi hoàn tất/lỗi: `event=desub.completed|desub.failed`, kèm `download_url`, `duration`, `processing_seconds`. |
| FR7 | Mọi tham số chất lượng gate bằng env: `DESUB_MODEL`, `DESUB_REGION`, `DESUB_DETECT_FPS`, `DESUB_MASK_DILATE_PX`, `DESUB_BAND_TOP_RATIO`, `DESUB_WATERMARK_MASKS`. |
| FR8 | Xuất kèm `mask.json`: danh sách span `{t_start, t_end, box}` từng sub đã xoá (phục vụ phase tích hợp sau). |

## 5. Yêu cầu phi chức năng (NFR)

| # | Yêu cầu | Ngưỡng |
|---|---------|--------|
| NFR1 | Thời gian xử lý video 7 phút 1080p | ≤ 45 phút trên L4 (mục tiêu 15–30) |
| NFR2 | Chi phí GPU / video | ≤ $0.5 (ước tính L4 ~$0.6–0.7/giờ) |
| NFR3 | Chất lượng | Không còn text đọc được trong vùng xử lý; không nhấp nháy thấy rõ ở 100% speed; nghiệm thu bằng mắt trên bộ video test |
| NFR4 | Bảo mật | API key + HMAC download như long; **mọi dependency pin bản mới nhất đã vá CVE** (torch, paddlepaddle/paddleocr, opencv-headless) |
| NFR5 | Cách ly | Deploy/xoá desub không ảnh hưởng long/short; không ghi vào prefix GCS của long/short |
| NFR6 | Vận hành | Log tiến độ theo bước + % frame; lỗi rõ ràng trong `status.json`; retry tải Douyin 3 lần như hiện tại |

## 6. Thành phần & vị trí code

```text
douyin_shorts/
├── desub_job/                  # Cloud Run Job GPU
│   ├── job.py                  # entrypoint: download → detect → mask → inpaint → encode → upload
│   ├── detect.py               # PaddleOCR det + IoU tracking + span builder
│   ├── inpaint.py              # adapter ProPainter / STTN (vendor lõi, model bake vào image)
│   ├── Dockerfile              # CUDA base image, bake sẵn model weights
│   └── cloudbuild.yaml
├── desub_job_runner/           # Cloud Run service CPU (clone pattern long_job_runner)
│   ├── app.py                  # /run, /download, /health
│   ├── Dockerfile
│   └── cloudbuild.yaml
├── tests/test_desub_masks.py   # unit test: span builder, cache key, API payload
└── docs/DESUB_ROADMAP.md       # tài liệu này
```

GCS layout (media bucket, prefix riêng):

```text
gs://YOUR_GCP_PROJECT-media-sg/desub/{desub_id}/clean.mp4
gs://YOUR_GCP_PROJECT-media-sg/desub/{desub_id}/mask.json
gs://YOUR_GCP_PROJECT-media-sg/desub/{desub_id}/status.json
gs://YOUR_GCP_PROJECT-media-sg/desub/_cache/v1/{url_hash}/clean.mp4
```

Cloud Run resources (mới hoàn toàn):

```text
Service: desub-job-runner   (CPU, asia-southeast1)
Job:     desub              (GPU L4, --gpu=1 --gpu-type=nvidia-l4, gen2, ≥4 CPU/16GiB)
Images:  us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/desub:prod
         us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/desub-runner:prod
```

## 7. Lộ trình phát triển

### Phase 0 — Tiền đề & khảo sát ✅ HOÀN THÀNH 2026-07-06
- [x] Verify quota/region: **Cloud Run jobs hỗ trợ GPU L4 tại `asia-southeast1`**;
      project lần đầu deploy GPU được cấp tự động 3 GPU quota (zonal redundancy
      off), yêu cầu ≥4 CPU + 16GiB. → Chốt chạy Cloud Run job. Fallback nếu
      thiếu capacity: GCE Spot `g2-standard-8` (quota GCE hiện có 1× L4 +
      1× L4 preemptible tại asia-southeast1).
- [x] **Chốt license model inpainting**:
      - ProPainter = NTU **S-Lab License 1.0, cấm thương mại** → **LOẠI**
        (chỉ dùng lại nếu xin được phép từ tác giả qua email).
      - **STTN = MIT ✓** (mạnh với video người thật, có skip-detection nhanh).
      - **LaMa = Apache-2.0 ✓** (mạnh với ảnh tĩnh/animation).
      - VSR (video-subtitle-remover) = Apache-2.0, còn maintain (v1.4.0,
        04/2026) → dùng làm lõi tham chiếu, **chỉ bật mode STTN/LAMA**.
- [x] Audit CVE dependency:
      - torch: bắt buộc **≥ 2.10.0** (CVE-2026-24747 RCE qua
        `torch.load weights_only=True` vá ở 2.10.0; CVE-2025-32434 vá ở 2.6.0).
      - paddlepaddle: nếu dùng thì bản 3.x mới nhất (các CVE 2023 vá từ 2.5/2.6);
        **KHÔNG cài paddle-serving** (RCE chưa có CVE/chưa vá). Phương án giảm
        bề mặt deps ưu tiên xem xét ở Phase 1: detector torch-based
        (EasyOCR/CRAFT, Apache-2.0) để cả stack chỉ cần 1 framework torch.
      - VSR: KHÔNG pip install nguyên `requirements.txt` của repo — audit
        từng bản pin, nếu pin bản cũ dính CVE thì vendor code + tự pin bản vá.
      - Model weights: bake vào image từ nguồn chính thức, load bằng
        torch ≥2.10 `weights_only=True`.
- **Nghiệm thu**: đạt — hạ tầng Cloud Run L4 khả dụng, model hợp pháp là
  STTN/LaMa, quy tắc pin deps đã rõ.

### Phase 1 — Prototype chất lượng (2–3 ngày)
- [ ] Script standalone `experiments/desub_prototype.py` chạy trên 1 máy GPU
      (VM dev hoặc job đơn lẻ — local KHÔNG tải được Douyin do CDN chặn IP,
      test trên cloud).
- [ ] Bộ video test: 1 video tư liệu (mẫu của user) + 1 manhua
      (`rWN_en8Y76E`) + 1 video có watermark động nếu tìm được.
- [ ] So sánh **STTN vs LaMa** (ProPainter đã loại vì license): xuất frame
      trước/sau + clip 30s cho user duyệt bằng mắt. Thử cả detector
      Paddle-det vs EasyOCR/CRAFT để chọn stack ít dependency hơn.
- [ ] Benchmark: phút GPU / phút video, VRAM, chi phí ước tính.
- **Nghiệm thu**: user chốt model mặc định + mức chất lượng chấp nhận.
- **Gate**: không đạt chất lượng → dừng, không tốn công dựng service.

### Phase 2 — Job + Runner skeleton, chạy e2e (2–3 ngày)
- [ ] `desub_job`: entrypoint env-driven, status.json từng bước, upload GCS.
- [ ] `desub_job_runner`: `/run`, `/download`, `/health`, API key, HMAC.
- [ ] Dockerfile GPU (bake model weights vào image — tránh cold download).
- [ ] Deploy + smoke test e2e bằng đúng video mẫu: gửi URL → nhận link → tải
      được video sạch sub.
- **Nghiệm thu**: 1 lệnh curl duy nhất ra được video sạch sub qua download_url.

### Phase 3 — Production hoá (1–2 ngày)
- [ ] Cache `_cache/v1/{url_hash}` + `force_refresh`.
- [ ] Callback n8n/Telegram (`desub.completed` / `desub.failed`).
- [ ] Retry/timeout/quota guard; log chuẩn `[desub]`.
- [ ] Unit test span-builder + cache key; cập nhật `docs/DEPLOY.md` (mục riêng).
- [ ] (n8n) workflow mẫu: command Telegram `/desub <url>` → nhận link khi xong.
- **Nghiệm thu**: checklist FR1–FR8 pass; chạy 3 video 5–7 phút liên tiếp ổn định.

### Phase 4 — (Tương lai, ngoài phạm vi v1)
- Long flow render từ `clean.mp4` (worker chỉ đổi input render, phân tích
  Gemini vẫn dùng video gốc vì prompt dựa vào sub Trung để verify transcript).
- Dùng `mask.json` làm nguồn timing sub Trung chính xác cho sync sub Việt.
- Chế độ `DESUB_REGION=full` xoá mọi text.

**Tổng ước tính v1: ~6–9 ngày công**, điểm dừng sau mỗi phase.

## 8. Rủi ro & giảm thiểu

| Rủi ro | Mức | Trạng thái / Giảm thiểu |
|--------|-----|------------|
| **License ProPainter (S-Lab, hạn chế commercial)** | ~~Cao~~ | **ĐÃ CLEAR 2026-07-06**: loại ProPainter; dùng STTN (MIT) + LaMa (Apache-2.0); lõi VSR Apache-2.0 |
| GPU L4 chưa mở cho Cloud Run jobs ở asia-southeast1 / thiếu quota | ~~Trung~~ | **ĐÃ CLEAR 2026-07-06**: asia-southeast1 được hỗ trợ, auto-grant 3 GPU lần deploy đầu; fallback GCE Spot g2 (quota 1× L4 sẵn) |
| CVE dependency (torch/paddle) | ~~Trung~~ | **ĐÃ CLEAR 2026-07-06**: torch ≥2.10.0 (vá CVE-2026-24747), paddle 3.x hoặc thay bằng EasyOCR; không dùng paddle-serving; không cài requirements VSR nguyên bản |
| Chất lượng inpaint kém trên nền phức tạp (text đè cảnh động nhanh) | Trung | Prototype gate ở Phase 1 trước khi dựng hạ tầng; band-crop giảm vùng vá; STTN vốn mạnh live-action |
| Chi phí GPU vượt dự kiến với video dài | Trung | Benchmark Phase 1; STTN skip-detection chỉ inpaint frame có mask |
| Douyin đổi cấu trúc/chặn tải | Thấp | Đã có retry + candidates trong downloader hiện tại; lỗi rõ trong status.json |
| Detect sót text (font lạ, chữ nghiêng) | Trung | Dilate mask + ngưỡng det thấp; QA bằng mắt bộ test; env chỉnh `DESUB_DETECT_FPS`/`DILATE_PX` |
| Ảnh hưởng nhầm sang long/short | Thấp | Cách ly tuyệt đối theo mục 6; review diff chỉ được thêm file mới |

## 9. Quyết định đã chốt & câu hỏi mở

Đã chốt (theo trao đổi 2026-07-06):
- Flow tách riêng hoàn toàn; input 1 URL; output link tải video sạch sub.
- Async + callback; long/short giữ nguyên.
- GPU: **Cloud Run job GPU L4 tại asia-southeast1** (đã verify hỗ trợ);
  fallback GCE Spot g2-standard-8.
- Model inpaint: **STTN (mặc định cho video người thật) + LaMa (tuỳ chọn cho
  manhua)** — ProPainter loại vì license S-Lab cấm thương mại.
- **Audio gốc giữ nguyên vẹn** (copy stream, chỉ re-encode video).
- **Watermark tĩnh: xoá MẶC ĐỊNH BẬT** (tắt được từng request qua payload/env).
- **TTL link download: 24h** (như short).
- **v1 gọi bằng curl/HTTP**; lệnh Telegram `/desub` qua n8n làm sau khi chất
  lượng đã chốt (Phase 3+).

Câu hỏi mở còn lại: (không còn — sẽ phát sinh mới ở gate Phase 1 nếu chất
lượng STTN/LaMa không đạt trên bộ video test).

## 10. Tiêu chí nghiệm thu tổng (Definition of Done v1)

1. Gửi 1 URL Douyin bất kỳ (5–7 phút) qua `POST /run` → trong ≤ 45 phút nhận
   callback + tải được `clean.mp4` qua `download_url`.
2. Video sạch: không còn sub Trung đọc được ở band đáy; không nhấp nháy thấy
   rõ; audio gốc nguyên vẹn; độ phân giải/duration không đổi.
3. Gửi lại cùng URL → trả kết quả cache trong < 1 phút, không tốn GPU.
4. Long/short flow chạy bình thường, không có thay đổi hành vi nào.
5. Docs DEPLOY có mục desub: build, deploy, smoke test, rollback.
