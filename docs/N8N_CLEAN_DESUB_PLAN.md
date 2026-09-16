# Kế hoạch n8n → Clean DESUB + lồng tiếng Việt → link tải

Cập nhật: 2026-07-27
Trạng thái: **Phase A đã được user cho phép và bắt đầu ngày 2026-07-23. Contract/schema baseline đã được materialize và kiểm thử cục bộ; Gate A vẫn BLOCKED/fail-closed vì runtime, audio/video verifier, KMS, media assets, fixture và calibration chưa đủ. Chưa xử lý video production, chưa deploy và chưa bắt đầu Phase B.**

## 1. Product contract đã được user duyệt

User đã xác nhận `/desub` là **một flow tích hợp**: xóa sạch dialogue subtitle Trung và tái tạo nền trước, sau đó dịch 1:1, lồng một giọng Việt cố định và render Vietsub. Bot chỉ trả `dubbed.mp4`; `clean.mp4` là artifact nội bộ bất biến để QA và recovery. Cover Visub hiện tại tiếp tục là flow riêng, không bị thay thế hoặc sửa behavior.

Khi user duy nhất trong allowlist gửi `/desub <url>` qua private chat Telegram:

1. n8n xác thực URL và gửi job bất đồng bộ.
2. Clean stage tải video, trích immutable source-cue audio/crop/text evidence trước inpaint, xóa sub Trung burn-in, giữ nguyên nội dung ngoài mask và tạo `clean.mp4` với audio gốc.
3. Clean machine QA, đúng ba verifier độc lập và final Sol pass cùng `clean_sha256`, source-cue-manifest SHA và policy; controller ký clean authorization.
4. Dub stage dùng `clean.mp4` làm video base và signed source-cue collection làm nguồn timing/text, rồi khóa cue ledger: đúng `N` câu Trung → `N` câu Việt → `N` TTS clip.
5. CapCut `BV075_streaming` đọc mọi câu bằng exact request `prosody rate="1.5000"`; AAC source-identical nằm trong approved `clean.mp4` là input duy nhất cho bed ở pre-duck gain `-20.000 dB`, Vietsub trắng viền tối tối đa hai dòng, không box/blur.
6. Dub machine QA, các verifier độc lập và final Sol pass cùng `dubbed_sha256`, cue-ledger SHA và policy.
7. Verifier pass chưa tự tạo quyền phát hành: controller phải revalidate exact attempt, active clean approval/index và toàn bộ ledger/evidence, rồi tạo KMS-signed `release_approval`, CAS active `release_index`. Chỉ khi release authorization còn hiệu lực và khớp exact `dubbed.mp4`, n8n mới gửi HTTPS download URL cho người dùng.

Trong tài liệu này, **clean artifact** có nghĩa:

- không blur để che sub;
- không thêm sub Việt;
- không thêm TTS hoặc thay đổi nội dung audio;
- nền phía sau sub được tái tạo bằng inpainting;
- giữ resolution, SAR, fps, frame count và timeline nguồn trong ngưỡng nghiệm thu.

**Dubbed artifact** là derivative chỉ được tạo từ clean artifact đã pass: có Vietsub + giọng Việt, AAC source-identical của approved `clean.mp4` làm nền và approval riêng. Không được dùng dub success để thay thế hoặc gắn nhãn clean pass.

Flow Cover Visub hiện tại (blur che sub Trung + sub Việt + TTS Việt) là sản phẩm khác và phải giữ endpoint/job/name/prefix riêng.

### 1.1 Scope v1 đã duyệt

- Input: đúng một URL Douyin video, container MP4, duration `1 giây–10 phút`, tối đa `1 GiB`.
- Video: SDR 8-bit, tối đa `1920×1080` hoặc `1080×1920`, `15–60 fps`, CFR, PTS tăng đơn điệu.
- V1 reject VFR, HDR, non-zero rotation metadata, resolution/fps vượt cap bằng error code rõ ràng; không âm thầm normalize.
- Audio: bắt buộc đúng một AAC stream tương thích MP4 vì timing phải lấy từ giọng Trung thực tế. Zero-audio, multi-audio hoặc codec nguồn cần transcode bị reject bằng stable error code.
- Vùng xử lý: subtitle dialogue band `y=0,55–0,90` của frame và chỉ các track/cụm được phân loại là caption.
- Không xóa watermark, logo, title, product text hoặc CJK hợp lệ ngoài target mask; full-frame text removal là out-of-scope v1.
- Cam kết “clean” của v1 chỉ áp dụng cho **dialogue caption trong supported band**. Preflight dùng detector độc lập quét toàn frame trên mọi frame; nếu phát hiện track có khả năng là dialogue caption nằm ngoài band thì reject `UNSUPPORTED_SUBTITLE_LAYOUT`, không âm thầm trả `completed`.
- Internal clean output: MP4/H.264 `yuv420p`, CRF 18, audio AAC stream-copy, CFR/fps/frame count khớp source; color/SAR metadata được bảo toàn trong giới hạn encoder.
- User output `dubbed.mp4`: MP4/H.264 `yuv420p`, CRF 18, Vietsub burn-in trắng viền tối và mixed AAC 44.1 kHz stereo; geometry/timeline video khớp clean artifact. Chấp nhận file lớn hơn source để ưu tiên chất lượng.

Các cap/behavior này đã được user duyệt cho v1. Gate A không được tự nới phạm vi; thay đổi phải có decision record mới và fixture tương ứng.

### 1.2 Quality policy v1 đề xuất

Gate A phải version hóa thành `clean_qa_policy.json`, `dub_qa_policy.json` và các referenced style/mix schemas trước khi executor làm engine:

- `video_timeline`: frame count bằng tuyệt đối; chuẩn hóa từng PTS/duration rational về source time base rồi so toàn bộ sequence với sai số tối đa một source tick/frame; DTS phải đơn điệu, không duplicate/gap so với source. Source/output time base và mọi normalization phải được ghi vào report.
- `container_duration`: lệch không quá một frame, thay cho ngưỡng `0,15 giây` cũ.
- `video_metadata`: width/height, SAR, fps, field order, color primaries, transfer, matrix, range, chroma location và rotation phải khớp source theo policy; metadata source không khai báo phải được giữ ở trạng thái unspecified, không tự đoán.
- `clean_audio`: packet count, thứ tự packet, packet-payload SHA và decoded PCM SHA của clean artifact bằng source; so toàn bộ PTS/duration sequence với sai số tối đa một audio tick (`1024/sample_rate`). Audio nguồn vốn kết thúc trước video vẫn pass ở clean gate nếu output bảo toàn chính xác sequence đó.
- `residual_ocr`: detector/OCR verifier phải khác detector của engine và được pin model/version/digest qua CVE gate. Nó quét **toàn supported band trên mọi decoded frame**, không chỉ vùng engine đã mask; đồng thời bắt buộc quét active-mask frame, union ROI và ±2 frame quanh mọi transition. Track được nối khi bbox IoU `≥0,30` ở frame kề nhau. Dialogue-CJK confidence `≥0,50` ở ít nhất hai frame liên tiếp là fail; single-frame/không chắc chắn chuyển agent review. CJK hợp lệ phải có source-side classification/provenance, không được mặc nhiên bỏ qua vì nằm ngoài mask.
- `mask_precision`: mỗi connected component phải truy được về source caption track hoặc phép dilation/padding đã version hóa. Component chạm stable logo/product/title text, không có caption evidence, vượt supported geometry hoặc xóa chữ hợp lệ là fail; QA packet phải có cặp source/candidate cho **mọi** component và mọi CJK đáng ngờ.
- `metric_decode`: candidate/control cùng được decode bởi một ffmpeg image digest đã pin sang luma 8-bit full-range bằng source color matrix/range; filter graph, pixel format và digest được ghi trong report. Mọi aggregate bỏ qua frame/pixel thiếu và phải báo denominator, không được coi missing data là pass.
- `outside_mask_damage`: tạo control re-encode cùng encoder/settings nhưng không inpaint. Trên từng frame, so candidate với control ngoài `(mask + 32 px codec halo)`; proposed gates là **max per-frame** luma MAE `≤1,5`, global p99 `≤8`, và **max per-frame** tỷ lệ pixel có absolute diff `>16` không quá `0,1%`.
- `temporal_flicker`: stable mask core là giao của mask ở `t-1,t` sau erosion 3 px; core rỗng được báo riêng, không auto-pass. Scene cut được xác định từ control bằng algorithm/threshold đã freeze và loại khỏi scalar gate trong cửa sổ ±2 frame, nhưng vẫn vào agent packet. Với frame hợp lệ, tính `F_t = p95(abs((candidate_t-candidate_t-1) - (control_t-control_t-1)))`; video p95 của `F_t` proposed `≤12`, không có ba frame liên tiếp `F_t >24`. Union ROI `(mask_t-1 ∪ mask_t) + 32 px` luôn được render cho transition review, kể cả một phía mask inactive.
- `cue_provenance`: Phase B phải trích evidence **từ source trước inpaint**, ghi create-only `source_cue_manifest` cùng từng immutable `cue_id`, rồi bind collection SHA/generations vào `clean_manifest` + signed `clean_approval`. Mỗi cue bind `source_sha256`, index, source audio sample range + decoded PCM-slice SHA, visual frame/sample range, crop URI + object generation + SHA, raw/Unicode-normalized `text_zh`, extractor/aligner provider + exact model/version/digest, prompt/schema digest, response ID và confidence. `speech_start/end` lấy từ audio Trung; `text_zh` lấy từ subtitle Trung nhìn thấy trên source frame. Thiếu evidence, mismatch hoặc alignment không monotonic đều fail-closed; hai câu lặp giống chữ vẫn là hai cue khác nhau khi time/evidence khác nhau. Phase C dùng clean làm video base và chỉ được đọc exact signed evidence collection; không được đọc/list raw source video.
- `cue_ledger`: final immutable ledger bắt buộc đúng `N source cues = N selected translations = N selected TTS clips = N Vietsub events`; index liên tục, mỗi cue có đúng một bản selected và không empty/merge/split/drop/reorder. Translation/TTS retry artifacts nằm trong create-only attempt log riêng, không được làm tăng cardinality final. Mỗi selected entry phải bind exact signed `translation_preflight_approval` đã authorize exact selected TTS attempt; release approval phải liệt kê đầy đủ các preflight approval được TTS history/ledger tham chiếu, không thiếu, trùng hoặc orphan.
- `translation`: giữ đúng tên, số, đơn vị, phủ định, quan hệ nghĩa và cơ chế gây cười/punchline; được Việt hóa tự nhiên để kéo view nhưng không bịa. `max_compact_retries=2` nghĩa là tối đa ba candidate cho một cue: một initial + hai compact. `translation_attempt` là pure create-only generator candidate nên tuyệt đối không chứa verdict/approval sinh sau. Với mỗi candidate, semantic + style verifier độc lập phải pass exact candidate/source-cue/attempt/policy bindings; controller mới ký create-only `translation_preflight_approval` decision `pass`, và exact approval còn hiệu lực phải được bind vào TTS attempt **trước synthesize**. Generator không tự verify. Hết budget hoặc vẫn không fit thì `DUB_CUE_UNFIT`.
- `tts_rate`: normative implementation là gửi đúng CapCut SSML/request `prosody rate="1.5000"` cho **mọi** cue với voice `BV075_streaming`; synthesize trực tiếp ở rate đó, không `atempo` hậu kỳ kể cả global, không per-cue fit và không provider/voice fallback. Lưu normalized request-payload hash + provider response ID cho từng selected clip; Dub verifier C đối chiếu actual pace/voice consistency với pinned Gate-A calibration fixtures để phát hiện payload metadata giả hoặc provider trả sai voice/rate.
- `tts_timing`: source cue PCM evidence và selected TTS clip đều dùng absolute `44.1 kHz` float timeline sau exact decode/resample filter đã pin; `speech_anchor` bắt buộc bằng `source_cue_record.source_audio.speech_start_sample`, không dùng native AAC clock hoặc phép đổi ngầm. Detector TTS dùng cửa sổ `20 ms`/hop `10 ms`, active khi max-channel RMS `>-45 dBFS` trong hai cửa sổ liên tiếp; onset là sample đầu và offset là sample cuối của run active. `actual_onset = placement_sample + detected_onset`; `lag = actual_onset - speech_anchor`; gap dùng actual previous offset → next onset. Scheduler nhắm actual onset sớm nhất là `max(speech_anchor, previous_actual_offset + 2205)`, có bù leading silence nhưng không cho active speech nói sớm. Gap `≥2205 samples (0,05s)`, `0 ≤ lag ≤26460 samples (0,60s)`, overlap tolerance đúng `0 sample`, final active voice offset không vượt video. Mọi detector parameter/digest phải được freeze/calibrate tại Gate A.
- `dub_mix_graph`: input bed duy nhất là exact AAC của approved `clean.mp4`, đã được clean-audio gate chứng minh packet/PCM/PTS bằng source; Phase C không đọc raw source. Decode approved clean AAC → resample `44.1 kHz` stereo bằng matrix pin → `apad/atrim` tới video duration → pre-duck gain đúng `-20.000 dB`; voice bus dùng selected clips ở gain `0.000 dB`; `sidechaincompress threshold=0.020000, ratio=10.000, attack=8 ms, release=250 ms`; `amix normalize=0`; two-pass BS.1770 loudness normalize mục tiêu `I=-16 LUFS, LRA=11, TP=-1 dBTP`; true-peak limiter pin ở `-1 dB`; cuối cùng pad/trim đúng video duration rồi encode một AAC `44.1 kHz` stereo stream. Không source separation; contract chấp nhận giọng Trung nhỏ. Filter order, exact options, ffmpeg image digest, clean PCM input SHA và PCM SHA của bed/ducked-bed/voice/premaster phải nằm trong report.
- `dub_audio_metrics`: đo bằng pinned FFmpeg implementation của ITU-R BS.1770-4/EBU R128; true peak oversample tối thiểu `4×`; decoded-float sample có `abs(sample) ≥1.0` được tính clipped. Gate: `-16 ±1 LUFS`, true peak `≤-1 dBTP`, zero clipped samples, không dropout/truncation. Trên các cửa sổ `100 ms` nằm trong active-voice mask, p05 của `voice_bus − ducked_bed` phải `≥8 dB`. No-TTS windows phải khớp deterministic bed-only control trong tolerance đã freeze, không channel swap/phase inversion/dropout. Effective playable audio end sau khi xử lý AAC priming/padding phải cách video end không quá đúng một AAC frame: `1024/44100 ≈23,22 ms`; source audio ngắn hơn video bắt buộc được pad bằng silence tới hết video.
- `dubbed_video_integrity`: tạo same-settings no-Vietsub control render từ exact clean frames. Ngoài `(Vietsub alpha mask + 32 px codec halo)`, so dubbed render với control bằng pinned metric decode: max per-frame luma MAE `≤1,5`, global p99 `≤8`, max per-frame fraction có absolute diff `>16` không quá `0,1%`; Gate A calibrate boundary fixtures nhưng không được bỏ gate. Không coi mọi global loss do re-encode là intended subtitle change.
- `vietsub`: đúng một event trên mỗi selected cue, text khớp ledger và timing theo actual Vietnamese voice schedule. `vietsub_style_policy.json` phải pin font file/license/SHA, Unicode NFC + full Vietnamese glyph coverage, font-size formula/min/max, outline width, line spacing, safe margins, max width và baseline; style trắng viền tối, tối đa hai dòng, không blur/box. Text không fit/thiếu glyph phải fail `DUB_SUBTITLE_UNFIT`; cấm truncate, tạo tofu, dòng thứ ba hoặc shrink dưới minimum.
- Mọi threshold phải được calibrate trên ba fixture ở Gate A; thay policy tạo version mới và reverify artifact/layer bị ảnh hưởng, không mặc định rerun GPU clean stage.

### 1.3 Operational decision record đã duyệt

| Hạng mục | Quyết định v1 |
|---|---|
| Executor | Sol lập kế hoạch/audit; Luna thực thi; Terra là fallback |
| Reasoning | Sol architect/auditor `xhigh`; Luna `high`; Terra `high`, tăng `xhigh` khi xử lý blocker khó; `max` chỉ khi lỗi dai dẳng hoặc quyết định release cuối |
| Kênh user | Chỉ private Telegram `/desub <url>`; public HTTP intake/result URL ngoài scope v1 |
| Access | Một Telegram user/chat ID duy nhất trong allowlist; không xây quota/phân quyền multi-user ở v1 |
| User output | Bot chỉ trả `dubbed.mp4`; `clean.mp4` và mọi evidence giữ nội bộ |
| Trạng thái bot | Edit một status message: queued → download → inpaint → clean QA → translate → TTS/Vietsub → dub QA → done; failure dùng cùng message |
| Lỗi bot | Hiển thị mô tả an toàn, stable error code, stage và nút “Thử lại”; redact URL/credential/OCR/log nhạy cảm |
| Subtitle target | Chỉ dialogue caption trong `y=0,55–0,90`; giữ watermark/logo/title/product text/chữ hợp lệ; caption dialogue ngoài band bị reject |
| Dubbing | Cue Trung→Việt/TTS/Vietsub đúng N→N; mọi cue dùng `BV075_streaming` + request `rate=1.5000`, max lag `0,60s`, không post-`atempo`/per-cue speed/fallback; không phân loại/match giọng Trung — 1:1 là cue/timing, không speaker identity |
| Translation | Tự động; tối đa hai compact retries; giữ đủ nghĩa + humor/punchline; không human script approval mỗi job |
| Audio bed | Không loại giọng Trung; bed chỉ lấy từ source-identical AAC của approved `clean.mp4`, exact pre-duck `-20.000 dB`, duck dưới voice Việt và pad tới video end; Phase C cấm raw-source read |
| QA | Fail-closed: clean machine + 3 verifier + Sol; mỗi translation candidate qua 2 preflight verifier; final dub machine + 3 fresh-context verifier + Sol trên exact SHA/ledger. Không majority/degraded pass |
| QA data | Chỉ gửi QA packet tối thiểu; QA provider phải no-training, retention ngắn và chứng minh xử lý/lưu tạm trong jurisdiction Singapore bằng endpoint riêng của provider. User chấp nhận ngoại lệ riêng cho private CapCut TTS endpoint: không có bảo đảm API/data-region/no-training |
| Retention | Source/clean/dubbed/source-cue evidence/attempts/report/verdict 7 ngày; raw QA packet 2 ngày; download link 24 giờ và refresh được |
| Capacity | Tối đa 2 job chạy đồng thời, queue 10; vượt queue báo hệ thống bận; vì chỉ có một user nên không có per-user quota riêng |
| SLO | Integrated video 7 phút/1080p ≤60 phút từ lúc execution bắt đầu; queue time hiển thị riêng; hard timeout 90 phút |
| Cost | Không đặt per-video cost cap; ưu tiên chất lượng; vẫn đo/alert chi phí và không tự giảm agent/reasoning/quality |
| Quality repair | Chỉ clean visual-quality fail được tạo tối đa một automatic repair attempt với clean SHA mới và invalidate toàn bộ dub derivative; fail lần hai thì dừng. Translation compact tối đa hai lần trước terminal; không automatic full-job dub repair |
| Isolation | Integrated Desub dùng service/job/storage prefix riêng trong project/region hiện tại; không đổi Cover Visub |
| Canary | Video hiện tại + một manhua + một high-motion/complex-background video |
| Release | Ba canary và mọi gate pass, sau đó user kiểm video cuối và phê duyệt trước production rollout |
| Authorization | User đã cho phép bắt đầu Phase A ngày 2026-07-23; quyền này chỉ bao gồm contract/policy candidate/ADR/fixture inventory/test cục bộ. Phase B, production video, Cloud Run Execution/deploy, KMS provisioning, n8n mutation và download link vẫn cần đúng gate/quyền riêng |

## 2. Kết quả rà soát hiện trạng

### 2.1 Artifact production gần nhất

Mẫu đã đối soát:

```text
tmp/cover-url-verification/output.mp4
GCS: gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub/v9/douyin-967a16485ac8/output.mp4
SHA-256: cc1748725ed4da518ed8b90d421bf8c7fd57143ec2722d4d72206bdc2388e567
```

| Kiểm tra | Kết quả |
|---|---|
| Full decode video/audio | Pass |
| Video | H.264, 720×1280, 30 fps, 5.729 frame, 190,967 giây |
| Report coverage | 29/29 cluster, 865/865 detection, 77/77 event |
| Report geometry/layout/timing | Pass |
| Visual sampling | Sub Trung được che và sub Việt nằm đúng band |
| Clean inpainting | **Fail: output dùng rounded blur** |
| Audio tail | **Fail: audio kết thúc sớm khoảng 0,699 giây** |
| Post-render residual OCR toàn timeline | Chưa có bằng chứng đầy đủ |

Kết luận: artifact này có thể là Cover Visub đạt hình ảnh ở các mốc đã xem, nhưng **không được dùng làm bằng chứng clean DESUB** và chưa được báo người dùng final-pass.

### 2.2 Prototype LaMa 10 giây

Artifact cũ:

```text
tmp/desub-flow-8015-first10/source_first10.mp4
tmp/desub-flow-8015-first10/clip_lama_10s_base.mp4
```

Visual sampling cho thấy LaMa đã xóa được caption trên clip manhua 10 giây. Tuy nhiên prototype chưa đạt media parity:

- source: 576×1024, 24 fps, 238 frame, 10,000 giây, 3.354.192 byte;
- output: 576×1024, 24 fps, 239 frame, 9,959 giây, 33.938.264 byte;
- chưa có exhaustive residual-OCR và temporal-flicker gate.

Prototype này là điểm khởi đầu cho engine, không phải artifact production-ready.

### 2.3 Backend và n8n hiện tại

Có thể tái sử dụng có chọn lọc:

- runner đã có `/run`, `/status`, `/download`, API key, HMAC link 24 giờ;
- URL normalization chỉ nhận Douyin;
- prefix/cache hiện được tạo ổn định theo URL;
- workflow n8n hiện tại đã có pattern Telegram → HTTP Request → callback cho Short/Long;
- kiểm tra ngày 2026-07-21 bằng `python -m unittest tests.test_desub_job_runner tests.test_cover_visub`: `Ran 77 tests; OK (skipped=6)`.

Khoảng trống bắt buộc xử lý:

- runner đang gọi `cover-visub-lab`, không phải clean-desub job;
- chưa có callback `desub.completed|desub.failed` và chưa có nhánh `/desub` trong n8n;
- check-status → write-queued → launch không atomic, có race khi request đồng thời;
- `force_refresh` có thể khiến nhiều attempt ghi chung output/prefix;
- link tải được mint trước khi artifact tồn tại/QA pass;
- API key còn có thể đi qua query string;
- downloader chưa kiểm tra đầy đủ redirect/DNS/IP để chống SSRF;
- code production đang có phần được nạp từ GCS mutable thay vì image digest bất biến;
- production chưa áp dụng retention policy đã được user duyệt;
- chưa có create-only source-cue evidence collection được trích trước inpaint và bind vào signed clean authorization;
- khi Phase A được cho phép, executor vẫn phải inventory `git status/diff` và bảo toàn mọi thay đổi hiện hữu trước khi sửa.

## 3. Kiến trúc đích

```text
Telegram private chat (1 allowlisted user)
          |
          v
n8n intake workflow
  validate URL + request_id
  POST /v1/desub/jobs
  trả acknowledgement ngay
          |
          v
integrated-desub-job-runner
  idempotency + atomic lease + attempt fencing
          |
          v
Cloud Run integrated desub job — clean stage
  download → detect/align + create source cue evidence → mask → inpaint → encode/remux
          |
          v
create-only source cue collection + clean manifest + clean.mp4
          |
          v
qa-controller — clean gate
  clean machine QA → 3 clean verifier agents → Sol clean approval
  create-only clean_approval + fenced clean_gate_index
          |
          v
dub stage (clean video base + signed source-cue evidence only)
  verify clean_approval → read exact signed source cue collection
  clean.mp4 là video base; không đọc/list raw source video
  strict N→N translation → independent semantic/style preflight
  CapCut BV075 request rate=1.5000 → decoded-PCM schedule
  Vietsub text-only + approved-clean-audio bed mix
          |
          v
create-only cue ledger + dub manifest + dubbed.mp4
          |
          v
qa-controller — dub gate
  dub machine QA → 3 dub verifier agents → Sol final approval
  signed release_approval + fenced release_index + terminal transition
          |
          | signed terminal callback
          v
n8n callback workflow
  verify signature + dedupe event_id
  GET terminal status để mint link mới
  chỉ gửi download URL của dubbed.mp4 cho user

Schedule reconciler: poll các job pending nếu callback thất lạc.
```

Không giữ một webhook request mở trong thời gian xử lý video. Intake trả `202`/job ID ngay; link cuối được gửi qua callback hoặc lấy qua status API.

State machine:

```text
queued
  → downloading
  → detecting
  → inpainting
  → encoding_clean
  → verifying_clean_machine
  → verifying_clean_agents
      ↳ nếu visual QA fail và clean_repair_attempts_used = 0:
        repairing_clean → detecting → inpainting → encoding_clean → chạy lại toàn bộ clean gate
  → clean_approved
  → aligning_speech
  → translating
  → verifying_translation
  → synthesizing_tts
  → scheduling_tts
      ↳ nếu unfit và compact_retry < 2:
        compacting_translation → verifying_translation → synthesizing_tts → scheduling_tts
  → rendering_vietsub
  → mixing_audio
  → verifying_dub_machine
  → verifying_dub_agents
  → signing_release
  → completed

Terminal khác: clean_qa_failed | translation_failed | tts_failed | scheduling_failed | subtitle_failed | dub_qa_failed | failed | cancelled
```

`clean_approved` chỉ có nghĩa clean gate pass và dub stage được phép đọc exact clean SHA + signed source-cue collection đã bind; nó không phải terminal/public success. `completed` bị cấm trước khi cả clean gate và dub gate pass. Nếu dub fail, clean artifact/approval vẫn bất biến nhưng bot không phát link final.

`job_state_record` luôn lưu `clean_repair_attempts_used` trong `0..1` và
`compact_retries_used` trong `0..2`. Chỉ transition vào `repairing_clean` khi counter
đang `0`; controller tăng counter bằng CAS trước khi chạy lại từ `detecting`. Clean
SHA/report/verdict cũ không được promote, và lần visual repair thứ hai chuyển
`clean_qa_failed`. Error code/stage/retryability/HTTP trực tiếp/terminal mapping được
freeze machine-readable trong `policies/v1/error_semantics_policy.json`; cancel dùng
riêng `REQUEST_CANCELLED`, không giả làm `STALE_FENCE`.

Launch lifecycle là field trực giao với public job state, không thêm `launch_unknown` vào state machine phía user:

- `launch_state = reserving | launching | unknown | launched`; khi chưa launch, public `state` vẫn là `queued`.
- Trước API call, launcher persist random `launch_token` 128-bit, `launch_started_at` và winning fence. `jobs.run` bắt buộc dùng `containerOverrides.env` để truyền immutable `DESUB_LAUNCH_TOKEN`, `DESUB_JOB_ID`, `DESUB_ATTEMPT_ID` và `DESUB_ATTEMPT_SEQ`; response `Operation.name`/`Execution.name` được persist ngay khi có.
- Cloud Run execution labels là output-only và không được dùng để correlation. Gate A phải dry-run chứng minh env override xuất hiện nguyên vẹn trong `Execution.template`; nếu không chứng minh được thì deployment bị block. API contract tham chiếu [Cloud Run `jobs.run`](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run) và [Execution resource](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions).
- Launcher heartbeat mỗi 30 giây. Timeout/network/5xx không rõ kết quả đặt `launch_state=unknown`, không tạo attempt mới và không gọi launch lần hai.
- Reconciler ưu tiên GET persisted operation/execution. Nếu chưa có tên, nó list/paginate execution mới từ safety window trước `launch_started_at`, đọc `Execution.template` và match **exact launch token** theo backoff `30s → 60s → 2m`; một match chuyển `launched`, nhiều match fail-closed và alert. Kết quả list bằng 0 chỉ là evidence âm chưa chắc chắn vì API không hứa strong-consistent negative, nên không được dùng để relaunch.
- Chỉ documented pre-accept validation/auth failure có no-operation mới được kết thúc ngay bằng `failed`; mọi ambiguous launch phải chờ. Sau 15 phút không phân giải được, revoke winning write fence rồi chuyển public state sang `failed` với `LAUNCH_STATE_UNRESOLVED`; execution xuất hiện muộn không được publish artifact.
- n8n coi `state=queued, launch_state=unknown` là pending, tiếp tục poll theo `retry_after_seconds` và alert khi quá unresolved deadline.

### 3.1 QA-controller ownership

`qa-controller` là component duy nhất có IAM quyền ghi `completed`; engine/runner chỉ được ghi trạng thái stage và create-only manifest. Sol không trực tiếp sửa status và không giữ signing key: Sol trả authenticated verdict, controller xác thực provider response rồi tự ký approval bundle bằng service account/KMS và thực hiện transition có fencing.

Controller chạy clean authorization, bounded translation preflight và final dub gate theo thứ tự:

1. Clean gate xác thực `clean_manifest`, source/clean SHA, create-only `source_cue_manifest`/per-cue object generations+hashes và clean machine report; packet gồm source crops/audio slices, head/mid/tail mọi caption span, transition ±2 frame, high-motion clips, outside-mask samples và media/audio parity.
2. Dispatch ba clean verifier độc lập: residual text/coverage, temporal flicker và visual damage; clean machine report, từng verifier và clean Sol verdict phải self-bind exact source SHA + clean SHA + `source_cue_manifest_sha` + collection root + policy. Sau ba pass, Sol tạo clean final verdict trên cùng bindings.
3. Controller/KMS tạo create-only `clean_approval.json`, rồi CAS `clean_gate_index` bind exact attempt fence + clean SHA + source-cue-manifest SHA/policy + approval ID. Dub work item chỉ chứa exact read-only clean URI và signed source-cue collection object URIs; dub service chỉ đọc sau khi verify signature, generations, active index và four-verdict set, không có raw-source list/read permission. Clean approval không mint user link.
4. Dub stage validate/consume immutable cue-provenance records đã được signed clean approval bind; nó không tái dựng text từ `clean.mp4`. Translation generator ghi pure create-only candidate `0`, không backfill verdict ref; semantic + style preflight verifiers ghi hai immutable verdict objects cho exact candidate/source-cue/attempt/policy bindings. Controller chỉ sau hai verdict `pass` mới ký create-only `translation_preflight_approval`; TTS attempt phải bind exact candidate ObjectRef/SHA và exact approval ObjectRef/SHA còn hiệu lực trước synthesize. Nếu decoded TTS schedule không fit, generator được ghi tối đa candidate `1` và `2`; mỗi compact candidate lại đi qua cùng chuỗi candidate → hai verdict → controller approval → TTS. Initial + hai compact là ba candidate tối đa, nhưng final ledger chọn đúng một và bind lại chính approval đã authorize selected TTS.
5. Mỗi selected clip phải bind CapCut request `BV075_streaming/rate=1.5000`, request hash/response ID, decoded PCM hash và actual onset/offset. Scheduler dùng decoded-sample metrics; không fit sau candidate `2` thì `DUB_CUE_UNFIT`.
6. Dub machine gate xác thực exact equality `N source cues = N selected translations = N selected TTS clips = N Vietsub events`, cue provenance, global voice/rate, decoded schedule, dubbed-video control diff, style, mix/stem integrity, audio metrics và dubbed SHA.
7. Dispatch ba final dub verifier độc lập trên exact final ledger/video: A kiểm nghĩa Trung→Việt/tên/số/phủ định; B kiểm tiếng Việt tự nhiên, humor/punchline và compact equivalence; C kiểm phát âm, actual N→N timing, Vietsub, rate/voice consistency, bed và audio tail. A/B phải reverify selected candidate trong final artifact; preflight verdict không thay thế final verdict. Sau ba pass, Sol tạo dub final verdict cùng dubbed SHA + cue-ledger SHA/policy.
8. TTS output được ASR/back-check để phát hiện truncation và critical-token mismatch; ASR không được dùng làm bằng chứng duy nhất cho semantic pass.
9. Cấp read-only signed URL TTL 1 giờ cho packet/artifact; không dùng user download URL. Mỗi verifier timeout 15 phút, retry tối đa hai provider attempts; verdict theo JSON schema và bind exact model/version, policy, relevant artifact/candidate SHA, cue-ledger SHA, timestamps và findings.
10. Bất kỳ fail/SHA mismatch/disagreement/hết retry đều fail-closed và không phát link. Clean repair vẫn tối đa một automatic attempt; compact translation có budget riêng như trên. Không cắt tiếng, merge/split/drop cue, đổi rate riêng cue hoặc publish partial output.
11. Controller chỉ tạo integrated release approval và `completed` khi active clean approval cùng authenticated final dub Sol verdict pass đúng manifests/SHA/policies/attempt fence; release payload phải liệt kê transitive set của signed translation preflight approvals mà TTS attempts và selected ledger tham chiếu.

Nếu adapter model/agent không khả dụng, state giữ gate tương ứng rồi timeout thành `clean_qa_failed` (clean roles), `translation_failed` (candidate preflight) hoặc `dub_qa_failed` (final dub roles); không fallback thành pass hoặc báo user kiểm.

### 3.2 Runtime cho verifier agents là Gate A cứng

Agent tương tác trong Codex task hiện tại không mặc nhiên là runtime callable từ Cloud Run. Trước Phase B, Gate A phải hoàn tất `docs/DESUB_QA_RUNTIME_DECISION.md` và kiểm chứng bằng một dry-run thật:

- mode production là provider API/endpoint callable theo job; pin provider, exact model IDs/versions cho clean/dub verifier sets và Sol verdicts, endpoint, auth secret reference, request/response schema, context/file size limit, timeout, quota/rate limit và provenance/response ID;
- chứng minh provider xử lý/lưu tạm QA packet trong jurisdiction Singapore bằng endpoint định danh riêng của provider, no-training và retention ngắn; không dùng tên region của Google làm contract chung cho provider khác, và không coi regional storage là bằng chứng regional processing; chỉ gửi packet tối thiểu, không mặc định gửi full video; không ghi API key vào image, workflow JSON hoặc model payload;
- chứng minh endpoint của từng role hỗ trợ đúng input modality cần thiết. Tài liệu API hiện tại của Sol/Luna/Terra chỉ nêu text/image input, nên không được tuyên bố các model này đã trực tiếp nghe audio hoặc xem video. Final Dub verifier C phải có runtime audio/video-capable độc lập, hoặc contract phải được user phê duyệt sửa sang bộ derived evidence được pin chính xác cùng giới hạn perceptual được ghi rõ;
- controller xác thực provider identity/response ID và schema; chỉ controller/KMS ký approval bundle. Model/provider không được nhận signing secret;
- nếu không chứng minh được QA runtime callable, quota và data policy, **Gate A dừng**; không implement/deploy một flow giả vờ có multi-agent QA;
- ngoại lệ user đã chấp nhận: private CapCut TTS endpoint/`BV075_streaming` không có bảo đảm chính thức về API stability, region hoặc no-training. Ngoại lệ này chỉ áp dụng payload text TTS; không nới policy của QA agents. CapCut/voice lỗi làm cả job fail, không mixed-provider fallback;
- v1 không có degraded hoặc manual pass. Agent unavailable/timeout/disagreement đều fail-closed. Chỉ clean visual-quality fail được phép đúng một automatic repair attempt theo policy; dub translation compact tối đa hai lần xảy ra trước terminal, còn dub terminal failure không tự full-job repair. User vẫn có thể bấm retry để tạo request/attempt mới; mọi artifact/SHA mới phải chạy lại đúng các gate phụ thuộc.

SLO execution phải tính clean gate, translate/TTS/render và dub gate: video 7 phút/1080p ≤60 phút từ lúc execution bắt đầu, hard timeout 90 phút; queue time báo riêng. User không đặt per-video cost cap và ưu tiên chất lượng. Hệ thống vẫn phải đo/alert GPU/model/API cost, nhưng không tự giảm agent, reasoning hoặc quality để tiết kiệm.

### 3.3 Artifact, verification và cache records

Không trộn engine artifact với approval:

- `clean/source_cues/{cue_id}.json` + crop/PCM objects create-only trước inpaint: source/audio/visual provenance fields tại §1.2; `clean/source_cue_manifest.json` liệt kê exact cue IDs, URI, generation và SHA của mọi evidence object.
- `clean/clean_manifest.json` create-only: source/clean SHA, exact source-cue-manifest URI/generation/SHA, media metadata, engine image/model/parameter digests, mask/report URIs.
- `dub/translation_attempts/{cue_id}/{candidate_index}.json` create-only: candidate `0..2`, normalized `text_vi`, reason `initial|compact_unfit`, generator provenance, candidate hash và semantic/style preflight verdict URI/generation/SHA. Không mutate attempt để đánh dấu selected.
- `dub/tts_attempts/{cue_id}/{candidate_index}.json` create-only: CapCut normalized request hash/response ID, selected voice/rate, returned media hash, decoded PCM hash, silence-detector result và schedule finding. Candidate bị reject vẫn giữ evidence tới hết retention.
- `dub/cue_ledger.json` create-only: ordered signed source-cue-evidence hashes và đúng một selected translation/TTS record/Vietsub event cho mỗi cue, cùng actual decoded-sample schedule; ledger SHA là selection authority.
- `dub/dub_manifest.json` create-only: active clean approval/manifest/SHA binding, cue-ledger SHA, dubbed SHA, same-settings no-Vietsub control SHA, Vietsub alpha-mask SHA, video/audio/subtitle metadata, stem PCM hashes và mix/render/style policy digests.
- `verification/clean/{clean_sha}/{clean_policy}/`: machine report, exact clean role set, clean Sol verdict và create-only `clean_approval.json`; every report/verdict self-binds source/clean/source-cue-manifest SHA + collection root + policy. Generation-conditional `clean_gate_index` bind attempt fence + same bindings tới active clean approval ID.
- `verification/dub/{dubbed_sha}/{dub_policy}/`: translation preflight verdicts, dub machine report, exact final dub role set, dub Sol verdict và create-only `release_approval.json`.
- Generation-conditional `release_index` bind `(clean_approval_id, clean_sha, source_cue_manifest_sha, dubbed_sha, cue_ledger_sha, clean_policy_digest, dub_policy_digest)` tới exact integrated `release_approval_id`; controller ghi approval trước rồi CAS/transaction chuyển `completed`. Re-verification tạo approval ID mới, không overwrite approval cũ.
- Clean authorization cache key: canonical source ID + clean engine/model/quality + source-cue extractor/aligner versions. Dub cache key: clean SHA + source-cue-manifest SHA + cue-ledger SHA + translation/provider/voice/global-rate/render/mix policy versions.
- Verification cache tách theo artifact SHA + policy version. Policy đổi thì reverify đúng layer; không mặc định rerun GPU clean stage.
- Cache hit production yêu cầu active signed clean approval, mọi manifest/selected attempt/cue ledger, active release index và exact `release_approval` hợp lệ.

Retention đã duyệt: source, clean/dubbed outputs, source cue collection, translation/TTS attempts, cue ledger, mask, controls/stems, reports, manifests và approval/verdict bundles 7 ngày; raw QA packets 2 ngày; download link 24 giờ và refresh được. Không xóa source/clean/evidence trước khi hết thời hạn cache cần reverify.

### 3.4 Clean authorization, release approval và KMS verification

Hai envelope create-only có mục đích khác nhau và không được thay thế lẫn nhau.

`clean_approval.json` là authorization nội bộ để dub stage đọc **một exact clean artifact**, không phải release cho user. `payload` tối thiểu bind:

- `schema_version`, random `clean_approval_id`, `issued_at`, `expires_at`, `job_id`, `attempt_id`, `attempt_seq` và winning fence token/digest;
- source object URI + GCS generation + SHA-256;
- clean object/manifest URI + GCS generation + size + SHA-256 và clean-contract digest;
- source-cue-manifest URI + generation + SHA-256, extractor/aligner policy digest và collection root/hash list của every crop/PCM/cue record;
- exact `clean_qa_policy` version/SHA, clean machine report URI/generation/SHA; report payload phải self-bind source SHA, clean SHA, source-cue-manifest SHA, collection root và policy SHA;
- đúng four-verdict clean set (`clean_a`, `clean_b`, `clean_c`, `clean_final_sol`), mỗi verdict có URI, generation, SHA, provider/service identity, exact model ID/version, response ID và self-bound source/clean/source-cue-manifest SHA + collection root + policy SHA;
- controller signer principal và clean-authorization schema version.

`release_approval.json` là integrated approval duy nhất được phép dẫn tới `completed`/download link. `payload` tối thiểu bind:

- các common identity/time/attempt/fence fields và exact `clean_approval` URI + generation + SHA + ID;
- source + clean object/manifest identities được lặp lại để phát hiện cross-attempt substitution;
- exact signed source-cue-manifest/collection inherited từ clean approval, selected translation/preflight verdicts, TTS attempt records và cue-ledger URI + generation + SHA;
- dubbed object/manifest, no-Vietsub control, Vietsub alpha mask, mix/stem report URI + generation + size/SHA và dub-contract/render/style/mix digests;
- exact `clean_qa_policy` + `dub_qa_policy` versions/SHA và clean/dub machine report identities;
- final dub verdict set (`translation_semantics`, `translation_style`, `dub_audio_video`, `dub_final_sol`) với URI/generation/SHA/provider/model/response identity. Preflight translation verdicts không thay thế final A/B verdicts;
- controller signer principal và integrated-release schema version. V1 không chấp nhận manual/degraded verdict thay thế bất kỳ role nào.

Quy tắc ký normative:

```text
canonical_bytes = UTF8(RFC8785_JCS(envelope.payload))
signed_digest = SHA256(canonical_bytes)
signature_bytes = CloudKMS.AsymmetricSign(
  name=full_crypto_key_version,
  digest.sha256=signed_digest
)
```

CryptoKeyVersion được pin phải có purpose `ASYMMETRIC_SIGN`, algorithm `EC_SIGN_P256_SHA256` và state `ENABLED`; algorithm là thuộc tính của key version, không phải override tùy request. Request/response phải kiểm CRC32C theo client contract trước khi chấp nhận signature. `envelope.signature` chứa exact algorithm enum, full KMS crypto-key-version resource, SHA-256 của pinned public key, lowercase hex `signed_digest` và base64 của exact signature bytes KMS trả về. Field `signature` không nằm trong canonical payload. Gate A phải freeze canonicalization fixtures cross-language; dependency JCS nếu dùng phải qua license/SBOM/CVE gate. API contract tham chiếu [Cloud KMS `asymmetricSign`](https://docs.cloud.google.com/kms/docs/reference/rest/v1/projects.locations.keyRings.cryptoKeys.cryptoKeyVersions/asymmetricSign).

Trước khi dub đọc clean artifact, verifier clean-authorization phải fail-closed theo thứ tự:

1. Validate `clean_approval` schema, exact four-role set, timestamps/expiry và accepted clean-policy digest.
2. Refetch source/clean/source-cue collection/manifest/report/verdict metadata; generation và recomputed byte SHA của collection manifest cùng every referenced crop/PCM/cue object phải khớp payload.
3. Provider/model/response IDs phải khớp `docs/DESUB_QA_RUNTIME_DECISION.md`; clean machine report và cả four-verdict set phải cross-match exact source/clean/source-cue-manifest SHA + collection root + policy. Sau đó recompute JCS digest, verify KMS signature/key state và exact attempt fence.
4. Generation-conditional `clean_gate_index` phải bind clean SHA + source-cue-manifest SHA/policy/attempt tới exact clean approval ID. Chỉ sau bước này controller cấp dub work item chứa exact read-only clean URI + evidence object URIs; dub service không được list/chọn “latest” hoặc đọc raw source video.

Trước transition `completed`, mỗi cache promotion/hit và mỗi lần mint/refresh download URL, release verifier phải fail-closed:

1. Validate release schema, exact final role set, timestamps/expiry, active clean-approval identity và accepted two-policy digests.
2. Refetch object metadata; generation và recomputed byte SHA của source/clean/dub manifests, cue evidence/attempts/ledger, artifacts, controls, reports và mọi verdict phải khớp payload.
3. Provider/model/response IDs phải khớp runtime decision; source/clean/dubbed/cue-ledger/candidate SHA trong mọi evidence phải đồng nhất theo gate và selection.
4. Full key version/public-key hash phải nằm trong allowlist và KMS key version phải đang `ENABLED`; verify cả clean và release envelope signatures.
5. Immutable attempt record phải bind đúng clean approval + clean/source-cue-manifest/dubbed/cue-ledger SHA; `release_index` phải bind cả policies/fence tới exact release approval ID. Mismatch, absent, malformed, expired, tampered, wrong policy/artifact/key, disabled/destroyed/revoked key hoặc KMS/key-status lookup lỗi đều không được `completed`, cache-hit hoặc phát link.

Chỉ `qa-controller` service account có `cloudkms.cryptoKeyVersions.useToSign`; runner/engine/n8n chỉ có quyền verify/read cần thiết. Bật Cloud Audit Logs cho mọi `asymmetricSign`. Rotation tạo key version mới; version cũ phải còn `ENABLED`/allowlisted ít nhất đến hết approval expiry. Nếu version cũ bị revoke/disable sớm, affected clean/release result phải chạy lại đúng verification gate, tạo approval ID mới dưới current key rồi CAS active index trước khi authorize dub hoặc phát link; không chỉ re-sign mù evidence cũ.

## 4. API contract đề xuất

### 4.1 Tạo job

```http
POST /v1/desub/jobs
X-API-Key: <n8n credential>
Idempotency-Key: <request-id>
Content-Type: application/json
```

```json
{
  "schema_version": "1",
  "douyin_url": "https://v.douyin.com/.../",
  "force_refresh": false,
  "client_context": {
    "request_id": "telegram-..."
  }
}
```

`client_context` chỉ chứa opaque request ID tối đa 128 ký tự. n8n giữ `chat_id/user_id` allowlisted trong registry nội bộ; backend không echo hoặc tin routing context do callback cung cấp.

Job mới/running trả `202`; cache hit đã được verify trả `200`:

```json
{
  "schema_version": "1",
  "job_id": "desub-...",
  "attempt_id": "att-...",
  "attempt_seq": 1,
  "state": "queued",
  "launch_state": "reserving",
  "cached": false,
  "callback_expected": true,
  "status_url": "https://.../v1/desub/jobs/desub-.../attempts/1",
  "retry_after_seconds": 30
}
```

Không trả download URL khi file chưa ready.

Verified cache hit trả `200`, không launch execution mới và không phát callback:

```json
{
  "schema_version": "1",
  "job_id": "desub-...",
  "attempt_id": "att-...",
  "attempt_seq": 1,
  "state": "completed",
  "launch_state": "launched",
  "cached": true,
  "callback_expected": false,
  "status_url": "https://.../v1/desub/jobs/desub-.../attempts/1",
  "clean_approval_id": "cap-...",
  "release_approval_id": "rap-...",
  "clean_sha256": "...",
  "source_cue_manifest_sha256": "...",
  "cue_ledger_sha256": "...",
  "artifact": {
    "kind": "dubbed",
    "ready": true,
    "download_url": "https://...",
    "expires_at": "...",
    "size_bytes": 123,
    "sha256": "..."
  }
}
```

n8n gửi link ngay ở nhánh cache hit và không chờ callback.

### 4.2 Status/result

```http
# Authoritative endpoint cho subscription/callback
GET /v1/desub/jobs/{job_id}/attempts/{attempt_seq}
X-API-Key: <n8n credential>

# Convenience alias; chỉ trả attempt mới nhất
GET /v1/desub/jobs/{job_id}
X-API-Key: <n8n credential>
```

n8n phải lưu `attempt_seq` từ start response và luôn dùng endpoint attempt-specific cho polling, callback và notification. Generic job endpoint chỉ là alias tới latest attempt, không được dùng để resolve terminal event. Callback handler GET đúng `{job_id, attempt_seq}`, rồi yêu cầu response `attempt_id`, `attempt_seq`, state/version khớp event; riêng `completed` còn bắt buộc `artifact.sha256` khớp trước khi fan-out. Mismatch fail-closed + alert.

Attempt record và terminal result là immutable ngoại trừ mint fresh signed URL. Một completed attempt bị supersede bởi `force_refresh` vẫn có thể mint đúng artifact của nó trong retention 7 ngày nếu approval hiện hành còn hợp lệ; attempt mới không rewrite result cũ. Hết retention trả `410 ATTEMPT_RESULT_EXPIRED`. Approval hết hạn/revoked trả `409 APPROVAL_REVERIFICATION_REQUIRED`, không có link và không tự lấy artifact của attempt khác.

Mọi response status đều có các field chuẩn:

```json
{
  "schema_version": "1",
  "job_id": "desub-...",
  "attempt_id": "att-...",
  "attempt_seq": 1,
  "state": "synthesizing_tts",
  "launch_state": "launched",
  "state_version": 4,
  "cached": false,
  "callback_expected": true,
  "status_url": "https://.../v1/desub/jobs/desub-.../attempts/1",
  "updated_at": "...",
  "retry_after_seconds": 60
}
```

Failure dùng error code ổn định, không bắt n8n parse message:

```json
{
  "schema_version": "1",
  "job_id": "desub-...",
  "attempt_id": "att-...",
  "attempt_seq": 1,
  "state": "failed",
  "state_version": 5,
  "launch_state": "launched",
  "cached": false,
  "callback_expected": true,
  "updated_at": "...",
  "error": {
    "code": "SOURCE_DOWNLOAD_TIMEOUT",
    "stage": "download",
    "message": "...",
    "retryable": true,
    "context": {"retry_after_seconds": 60}
  }
}
```

Chỉ khi terminal pass response mới có artifact:

```json
{
  "schema_version": "1",
  "job_id": "desub-...",
  "attempt_id": "att-...",
  "attempt_seq": 1,
  "state": "completed",
  "state_version": 9,
  "launch_state": "launched",
  "cached": false,
  "callback_expected": true,
  "updated_at": "...",
  "clean_approval_id": "cap-...",
  "release_approval_id": "rap-...",
  "clean_sha256": "...",
  "source_cue_manifest_sha256": "...",
  "cue_ledger_sha256": "...",
  "artifact": {
    "kind": "dubbed",
    "ready": true,
    "download_url": "https://...",
    "expires_at": "...",
    "size_bytes": 123,
    "sha256": "..."
  }
}
```

Mỗi lần đọc terminal attempt status có thể mint link mới sau khi revalidate approval theo §3.4; TTL link v1 là 24 giờ.

Status codes: `200` cho job/attempt tồn tại và integrated release hợp lệ; `401` auth sai; `404` unknown job/attempt; `409` approval cần reverify; `410` result hết retention; `429` quá tải; `5xx` lỗi tạm thời. Mọi clean/dub failure terminal và `cancelled` đều không có download URL final.

### 4.3 Callback tới n8n

Callback URL phải cấu hình server-side/allowlist, không nhận URL tùy ý từ payload người dùng.

```text
X-Desub-Event-Id: evt-...
X-Desub-Timestamp: <unix-seconds>
X-Desub-Key-Id: callback-v2
X-Desub-Signature: sha256=<HMAC>
```

```json
{
  "schema_version": "1",
  "event": "desub.completed",
  "event_id": "evt-...",
  "job_id": "desub-...",
  "attempt_id": "att-...",
  "attempt_seq": 1,
  "state": "completed",
  "state_version": 9,
  "verification_passed": true,
  "clean_approval_id": "cap-...",
  "release_approval_id": "rap-...",
  "source_sha256": "...",
  "clean_sha256": "...",
  "source_cue_manifest_sha256": "...",
  "cue_ledger_sha256": "...",
  "dubbed_sha256": "...",
  "occurred_at": "..."
}
```

n8n xác thực callback rồi gọi exact attempt status `/jobs/{job_id}/attempts/{attempt_seq}` để lấy link mới; `clean_approval_id`, clean/source-cue-manifest/cue-ledger/dubbed SHA và `release_approval_id` phải khớp, trong đó callback `dubbed_sha256` bằng status `artifact.sha256`. Callback delivery là at-least-once, retry exponential; `event_id` phải được dedupe bền vững.

Quy tắc chữ ký normative:

```text
signed_bytes = ASCII(unix_timestamp) + b"." + exact_http_raw_body_bytes
signature = lowercase_hex(HMAC-SHA256(secret_for_key_id, signed_bytes))
replay_window = 5 phút
```

So sánh signature constant-time; không thêm newline, đổi encoding hoặc parse/reserialize JSON trước khi verify.

Mỗi **HTTP delivery attempt** giữ nguyên exact raw body bytes, `event_id` và body `occurred_at`, nhưng dispatcher tạo `X-Desub-Timestamp` mới tại thời điểm gửi và ký lại bằng callback key đang active/key ID tương ứng. Không reuse timestamp/signature của lần gửi trước. Vì vậy retry sau replay window vẫn hợp lệ, còn replay nguyên header/signature cũ quá 5 phút bị `401`.

- Signature/replay invalid trả `401`.
- `event_id` record có state `received | fanout_pending | done`. Duplicate khi `fanout_pending` phải resume chỉ các subscription chưa durable-ACK; chỉ event `done` mới trả `200` mà không enqueue notification lần hai. Không đánh dấu `done` ngay khi mới nhìn thấy event.
- Terminal state, active `release_index`/approval binding và outbox event phải được ghi trong cùng một generation-conditional state document hoặc cùng một DB transaction.
- Job không tự POST callback. Dispatcher/Cloud Task claim outbox event, chỉ ghi `delivered_at` sau HTTP `2xx`, retry có giới hạn và chuyển DLQ khi cạn retry.
- Retry schedule đề xuất: `10s, 30s, 1m, 5m, 15m, 1h, 3h, 6h`; retry network/408/429/5xx, còn non-auth 4xx chuyển DLQ và alert.
- Backend outbox reconciler quét event pending nếu enqueue/dispatcher gặp sự cố; n8n reconciler là lớp recovery phía nhận, không thay thế backend outbox.
- Callback secret rotation chấp nhận current + previous key ID trong tối đa 24 giờ; unknown key ID bị reject.
- `desub.failed`, `desub.clean_qa_failed`, `desub.translation_failed`, `desub.tts_failed`, `desub.scheduling_failed`, `desub.subtitle_failed`, `desub.dub_qa_failed` và `desub.cancelled` dùng cùng envelope/state version, có structured `error`, không có artifact/download URL.
- Mỗi subscription bind immutable `(job_id, attempt_seq, attempt_id)`. Callback chỉ update các row bind đúng attempt; với row đó chỉ nhận `state_version` lớn hơn. Attempt cũ hơn latest job **không bị bỏ toàn cục** nếu nó còn subscription chưa notify; handler đọc exact attempt result. Event cho attempt khác subscription hoặc state version cũ vẫn ACK `200` nhưng không mutate row đó.

### 4.4 Idempotency, work dedupe và attempt semantics

Ba khái niệm không được trộn:

1. `Idempotency-Key` dedupe request trong 7 ngày. Cùng key + cùng canonical request body trả cùng `job_id/attempt_id/attempt_seq` và trạng thái hiện tại; signed link có thể được mint lại. Cùng key + body khác trả `409`.
2. Parent `content_fingerprint = SHA-256(canonical_aweme_id + clean contract + dub contract)` dedupe integrated work. Clean và dub layer còn có cache key riêng theo §3.3; hai short URL trỏ cùng aweme phải có cùng canonical ID trước reservation.
3. `attempt_id` là opaque ID của một lần xử lý; `attempt_seq` là số nguyên tăng đơn điệu trong job/content fingerprint. `force_refresh=true` chỉ tạo attempt mới với seq lớn hơn sau khi attempt hiện hành đã terminal; nếu còn attempt active trả `409 ATTEMPT_IN_PROGRESS`. Mọi output/status write được fenced theo winning `(attempt_seq, attempt_id)` và terminal result cũ vẫn immutable/readable qua exact attempt endpoint.

Atomic reservation theo content fingerprint phải trả existing running job cho request key khác, thay vì launch execution thứ hai. Artifact có thể tái sử dụng theo fingerprint; cache chỉ được trả cho user khi approval bundle của QA-policy hiện hành cũng hợp lệ.

Mỗi intake `request_id` là một **subscription** riêng tới shared `job_id/attempt_seq`. V1 chỉ có một private Telegram chat trong allowlist; nhiều subscription chỉ phục vụ idempotency, retry và work dedupe cho các request của chính chat đó, không phải thiết kế quota/phân quyền multi-user. Callback job-level không mang `request_id`; registry là source of truth. Notification idempotency key là `request_id + job_id + attempt_seq + terminal_fingerprint`, trong đó completed fingerprint là `dubbed_sha256`, còn failure fingerprint là `state + state_version + error.code`. Handler phải claim/update từng subscription transactionally để crash giữa chừng chỉ retry row chưa notify.

Runner không gọi `jobs.run` trực tiếp trong request path. Start API persist queued record trước; launcher riêng claim record và persist `launch_state`/operation. Nếu lệnh launch timeout không rõ execution đã tạo hay chưa, `launch_state` chuyển `unknown` trong khi public state vẫn `queued`; reconciler xử lý theo §3, không retry mù.

### 4.5 Subscription và cancel contract

```http
POST /v1/desub/requests/{request_id}/cancel
X-API-Key: <n8n credential>
```

Request cancel là idempotent và chỉ detach/cancel request tương ứng; shared work vẫn chạy nếu cùng chat còn request khác dùng chung job. Global job cancel dùng endpoint admin-only `POST /v1/desub/jobs/{job_id}/cancel`, fence winning attempt trước rồi mới yêu cầu Cloud Run stop và đánh dấu toàn bộ subscription terminal. Callback cũ không được phép đổi state mới hơn.

### 4.6 Credential boundaries và Telegram recipient

Ba credential phải độc lập và có version/rotation:

- n8n → runner: Cloud Run OIDC hoặc runner API key dùng cho start/status/cancel;
- backend → n8n: callback HMAC secret;
- download: signing secret/service account riêng.

Workflow JSON chỉ chứa credential reference/name, không chứa value. Download URL là bearer URL có TTL; không dùng runner credential trong URL.

Bot chỉ nhận `/desub` từ private chat có cả `user_id` và `chat_id` khớp allowlist. Telegram user không bao giờ nhận runner credential; bot chỉ gửi fresh download URL có TTL sau khi release gate pass. Public HTTP intake, public `result_url` và arbitrary caller callback đều ngoài scope v1.

## 5. Phân vai model và handoff

Codex host hiện tại expose `gpt-5.6-sol`, `gpt-5.6-luna` và `gpt-5.6-terra`; Luna có thể được gọi qua một Codex task chuyên trách. Danh sách model override hẹp hơn của một bề mặt orchestration nội bộ không có nghĩa Luna vắng mặt ở host. Tham chiếu official: [Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra).

Handoff và reasoning đã được user duyệt như sau. Phase A đang được Sol triển khai/audit; Luna vẫn là executor chính cho các phase implementation sau khi gate tương ứng pass, Terra là fallback đã duyệt:

| Vai trò | Model/agent | Trách nhiệm |
|---|---|---|
| Architect | 5.6 Sol, `xhigh` | Khóa contract, test fixtures, acceptance gates, task graph |
| Executor | 5.6 Luna, `high` | Thực thi từng phase đã được user cho phép, không tự nới gate |
| Executor fallback | 5.6 Terra, `high` | Thay Luna khi Luna không khả dụng hoặc bị blocker; tăng `xhigh` chỉ cho blocker khó |
| Clean verifier A/B/C | Ba agent/model độc lập | Residual text/coverage; temporal flicker; visual damage/valid-text preservation |
| Translation generator | Exact model pin tại Gate A | Sinh initial/compact candidate; không được tự chấm hoặc thấy verdict ngoài findings cần sửa |
| Translation preflight A/B | Hai model độc lập với generator và nhau | Mỗi candidate hash: semantic fidelity/names/numbers/negation và naturalness/humor/compact equivalence trước TTS |
| Final dub verifier A/B | Hai fresh-context verifier độc lập | Reverify exact selected candidate trong final ledger/video; preflight verdict không được copy thành final pass |
| Final dub verifier C | Agent audio/video độc lập | Actual voice/rate, decoded timing, Vietsub/control pixels, mix/bed/channel/phase/loudness/dropout/tail |
| Clean + release auditor | 5.6 Sol, `xhigh` | Clean approval và final release trên exact evidence/SHA; chỉ dùng `max` cho lỗi dai dẳng hoặc quyết định release cuối |

Executor không được tự nghiệm thu output do chính mình tạo.

## 6. TODO theo phase

Execution order là `A → B → C → D → E → F → G`. Gate B phải tạo signed clean authorization trước Phase C; vì vậy không còn dependency ngược từ dub engine sang QA phase phía sau. Không phase nào được bypass gate trước nó. Phase A đã bắt đầu ngày 2026-07-23; checkbox chỉ được đánh dấu hoàn tất khi phần việc tương ứng đã materialize và pass kiểm thử, còn mọi mục phụ thuộc external evidence vẫn để mở và fail-closed.

### Phase A — Contract và baseline freeze (`SOL`)

- [x] Materialize contract/schema đã duyệt: v1 chỉ nhận một URL Douyin; internal `clean.mp4` pure clean, user chỉ nhận `dubbed.mp4`.
- [x] Đưa input caps, subtitle band, stable terminal/error taxonomy và reject policy tại §1.1 vào ADR/schema/tests; tách tên/service/job/prefix khỏi Cover Visub.
- [ ] Freeze `clean_qa_policy.json` và `dub_qa_policy.json`, gồm exact source-cue collection/manifest schema + extractor/aligner identity, metric decode, `max_compact_retries=2`, translation candidate schema, CapCut request `rate=1.5000`, PCM silence detector/sample arithmetic, no-Vietsub control diff, Vietsub style/font SHA, deterministic mix graph/stem hashes, BS.1770 implementation và audio-tail semantics tại §1.2. Candidate policy đã materialize nhưng còn blocker media/font/renderer/calibration.
- [ ] Freeze `clean_approval.schema.json`, `release_approval.schema.json`, `clean_gate_index`/`release_index`, RFC 8785 fixtures, KMS key allowlist, attempt fence, IAM/audit và rotation/revocation runbook theo §3.4. Schema/fixture/runbook đã có; KMS allowlist vẫn rỗng.
- [ ] Hoàn tất và dry-run `docs/DESUB_QA_RUNTIME_DECISION.md`; pin callable clean, translation-preflight và final-dub verifier IDs/auth/quota/schema/provenance/cost; chứng minh QA processing/transient storage ở Singapore, no-training, retention ngắn và input modality phù hợp. Decision record hiện BLOCKED; Sol/Luna/Terra không có direct audio/video input qua API được tài liệu hóa. Ghi riêng accepted CapCut risk exception; không áp dụng ngoại lệ này cho QA agents.
- [ ] Dry-run Cloud Run `jobs.run` env overrides; chứng minh exact launch token trong `Execution.template`; không dùng label/zero-result list làm authoritative negative. Mới có validate-only template; chưa có side-effect-free Execution proof được user cho phép riêng.
- [x] Ánh xạ retention: source/clean/dubbed/cue evidence/attempts/ledger/masks/reports/manifests/verdicts/approvals 7 ngày, raw QA packet 2 ngày, signed link 24 giờ refreshable.
- [x] Inventory `git status/diff`, bảo toàn thay đổi user; trước bất kỳ dependency install nào phải qua license/SBOM/CVE gate và không cài version có CVE đã biết. Không package/tool nào được cài trong Phase A hiện tại.
- [ ] Freeze ba fixture đã duyệt với expected hashes/findings; bao gồm thoại dày, punchline, repeated cue, multi-speaker, source audio ngắn hơn video, last Vietnamese clip kết thúc ≥1 giây trước video, text hai dòng/diacritics và high-motion subtitle render. Hiện thiếu full manhua, ground truth cue và tail/direct-rate fixtures.
- [x] Freeze SLO/capacity: 7 phút 1080p ≤60 phút execution, hard timeout 90 phút, concurrency 2, queue 10; cost telemetry không tự downgrade quality/reasoning.

Gate A: mọi schema/policy/runtime/fixture trên được Sol approve và đủ deterministic để Luna implement; chưa xử lý video production hoặc deploy.

### Phase B — Clean engine, clean QA và signed clean authorization (`LUNA` implement, `SOL` audit)

- [ ] Tách detect/mask/inpaint từ prototype thành package/job riêng; benchmark LaMa/STTN trên cùng fixtures, bake code/weights vào immutable image digest.
- [ ] Probe input và giữ clean stage thuần: không translator/CapCut/TTS/Vietsub trước signed clean approval.
- [ ] Trước inpaint, trích create-only per-cue source crops + PCM slices + raw/NFC ZH text/speech anchors; tạo `source_cue_manifest` liệt kê exact URI/generation/SHA và reject missing/mismatch/non-monotonic provenance.
- [ ] Encode `clean.mp4` H.264 `yuv420p`, CRF 18; bảo toàn geometry/full timeline và AAC packet/PCM/PTS bằng stream-copy/remux; xuất mask/report/create-only clean manifest/SHA có binding tới source-cue-manifest SHA.
- [ ] Sửa frame/timeline parity của prototype; tạo same-settings control re-encode; heartbeat/progress theo frame/chunk.
- [ ] Implement clean machine QA: full decode, metadata/full PTS/DTS/container parity, exact AAC parity, source-cue collection completeness/hash/extractor provenance, outside-band preflight, exhaustive residual OCR, component provenance/mask precision, outside-mask control diff và temporal flicker.
- [ ] Implement clean QA packet + three independent clean verifiers + clean Sol; machine report và mọi verdict self-bind/cross-match exact source/clean/source-cue-manifest SHA + collection root + policy/model/response identity.
- [ ] Implement shared RFC 8785 canonicalization + KMS sign/verify library, CRC32C, pinned public-key/key-state allowlist và generation/fence/index revalidation tối thiểu cho clean authorization; controller tạo `clean_approval.json`, CAS `clean_gate_index` và test tamper/stale fence/wrong generation/key/policy fail-closed. Clean approval không tạo user link.
- [ ] Clean visual-quality fail được tạo tối đa một automatic repair clean SHA; repair pass phải chạy lại toàn bộ clean machine/agents/Sol, repair fail dừng.

Gate B: staging có exact clean SHA + source-cue-manifest SHA pass machine + 3 verifier + Sol, signed clean approval/index hợp lệ; dub service dry-run chỉ đọc exact clean/evidence objects và bị deny raw-source list/read. Chưa tạo dubbed artifact.

### Phase C — Authorized dubbing engine và dub machine QA (`LUNA` implement, `SOL` khóa gate)

- [ ] Dub stage chỉ nhận controller work item bind exact attempt/clean approval; verify signature/generations/index/fence trước khi đọc exact clean URI + signed source-cue evidence URIs, không list/chọn latest và không raw-source access.
- [ ] Validate/consume Phase-B per-cue provenance: source SHA, cue ID/index, audio sample/PCM slice, visual frame/crop hashes, raw/NFC ZH text, aligner/extractor identity/confidence; reject missing/mismatch/non-monotonic evidence. Không OCR lại `clean.mp4` đã mất sub.
- [ ] Generator ghi candidate `0`; independent semantic + style preflight bind candidate hash trước TTS. Schedule unfit mới được tạo candidate `1` rồi `2`; mỗi candidate compact phải reverify. Hết `max_compact_retries=2` trả `DUB_CUE_UNFIT`.
- [ ] Synthesize selected candidate bằng CapCut `BV075_streaming`, exact request `prosody rate="1.5000"`; lưu request/response/media/PCM hashes. Không post-`atempo`, per-cue fit, provider fallback, mixed voice hoặc speaker classification/matching.
- [ ] Detect actual active PCM onset/offset bằng pinned 44.1-kHz detector; schedule actual onset không sớm hơn speech anchor, gap ≥2205 samples, `0 ≤ lag ≤26460` samples, overlap 0 sample và final active voice within video.
- [ ] Render Vietsub theo pinned style/font policy; đúng N selected events/timing. Overflow/glyph fail `DUB_SUBTITLE_UNFIT`, không truncate/tofu/third-line/excessive shrink.
- [ ] Tạo same-settings no-Vietsub control + alpha mask; render video từ exact clean frames và kiểm intended-pixel boundary.
- [ ] Mix chỉ exact decoded PCM từ approved `clean.mp4` theo deterministic graph tại §1.2; pad clean-derived/source-identical bed tới video end, lưu stem hashes, encode một AAC 44.1-kHz stereo stream; không raw-source audio read.
- [ ] Reference exact signed Phase-B source-cue evidence; xuất create-only translation/TTS attempts, final cue ledger, dub manifest/report/control/mask/stem evidence và dubbed SHA.
- [ ] Implement dub machine QA: selected N→N cardinality/order, candidate verdict bindings, actual voice/rate consistency evidence, decoded schedule, ASR critical-token back-check, Vietsub style/sync, outside-subtitle control diff, bed integrity, BS.1770 loudness/true peak/clipping, p05 voice-bed margin, dropout/tail/priming-padding.
- [ ] Cache dub layer chỉ khi clean approval + every selected candidate/clip/ledger/control/report SHA và current policies khớp; chưa mint public link.

Gate C: signed clean authorization đã được tiêu thụ đúng; dubbed artifact và full machine evidence pass exact dub policy. State chỉ được chuyển `verifying_dub_agents`, chưa `completed`.

### Phase D — Runner, lifecycle và security (`LUNA`)

- [ ] Tạo random `job_id`, distinct `attempt_id`, per-attempt prefix và create-only `clean/`, `dub/`, `verification/{clean,dub}/` records.
- [ ] Implement immutable attempt-specific status endpoint + latest alias; idempotency/body conflict, canonical aweme fingerprint, separate versioned clean/dub cache keys và force-refresh semantics.
- [ ] Atomic lease/fencing; launcher tách HTTP path; persist launch token/state/operation trước `jobs.run`; reconcile ambiguous launch bằng positive exact-token match, không relaunch mù.
- [ ] Controller-issued dub work item bắt buộc bind active clean approval/index/fence, clean SHA và source-cue-manifest SHA; least-privilege dub service chỉ đọc exact clean/evidence object URIs, không list prefix, dùng unsigned artifact hoặc đọc raw source video.
- [ ] Retry/reconcile/cancel có giới hạn; concurrency 2, queue 10, stable busy/`429`, không per-user quota v1.
- [ ] Header auth/OIDC; SSRF defense cho mọi redirect/resolved IP; cap redirect/bytes/MIME/time.
- [ ] Signed callback outbox/DLQ/replay handling; atomic terminal + outbox; only integrated release mints refreshable Range-capable dubbed link, never clean link.
- [ ] GCS lifecycle, immutable generations và least-privilege service accounts; redact URL/text/CapCut/device/provider payload/PII from structured logs.
- [ ] CapCut egress allowlist/timeout/retry/circuit breaker; endpoint/voice/empty/corrupt response fail whole job, không fallback.
- [ ] Metrics/alerts cho leases, launch unknown, stage loops/retry budgets, callback, clean/dub QA, latency/cost/capacity.

Gate D: concurrency/idempotency/force-refresh/stale-fence/SSRF/callback/cache/tamper/expired-link tests pass; runner không thể bypass clean authorization hoặc integrated release.

### Phase E — n8n Telegram workflow (`LUNA`)

- [ ] Preflight n8n version/nodes; thêm duy nhất private-chat `/desub <url>` và reject group/channel/user/chat ngoài single allowlist trước runner.
- [ ] Validate một URL, tạo registry row trước request, retry cùng idempotency key; lưu exact job/attempt/state version, clean approval/SHA, source-cue-manifest/cue-ledger/dubbed SHA và notification state.
- [ ] Nhánh `202` tạo một acknowledgement; cache-hit `200/completed` chỉ gửi fresh dubbed link sau integrated approval.
- [ ] Edit một status message qua user stages `queued → download → inpaint → clean QA → translate → TTS/Vietsub → dub QA → done`; internal verify/compact/schedule loops map vào stage hiện hành, state không lùi/spam.
- [ ] Tạo `/desub-event`; verify raw-body HMAC trước parse, durable event/subscription dedupe và exact attempt status lookup. Nếu n8n không bảo đảm raw body, dùng authenticated verifier gateway.
- [ ] Completed phải verify callback/status clean approval ID + clean/source-cue-manifest/cue-ledger/dubbed SHA + release approval ID rồi mới lấy `artifact.kind=dubbed`; không dùng latest alias hoặc clean link.
- [ ] Reconciler có lease/backoff xử lý lost/out-of-order callback, restart, `401/404/409/410/429/5xx`, expiry và exact-attempt refresh.
- [ ] Terminal failure edit cùng message với safe code/stage/retry; request detach và admin global cancel đúng shared-work contract.
- [ ] Export workflow JSON sanitized; không hard-code credential/secret.

Gate E: mocked Telegram intake/status/callback/reconcile tests pass cho mọi internal/public stage và terminal; chưa live E2E hoặc production acceptance.

### Phase F — Final dub multi-agent QA và integrated release (`3 verifier + SOL`)

- [ ] Revalidate active signed clean approval/index and exact Gate-C dub machine report before dispatch.
- [ ] Final Dub verifier A đối chiếu per-cue audio/visual ZH evidence với selected VI: nghĩa, tên/số/đơn vị/phủ định/quan hệ, no invention, exact N→N.
- [ ] Final Dub verifier B kiểm tiếng Việt tự nhiên, humor/mỉa mai/punchline và compact equivalence; reverify selected candidate, không tin preflight pass mù.
- [ ] Final Dub verifier C nghe/kiểm toàn bộ selected clips/video: đúng voice, pace consistency với rate fixture, decoded onset/gap/overlap/tail, Vietsub sync/style, unintended pixels, bed continuity/channel/phase và perceptual voice-over-bed.
- [ ] Mỗi final verdict bind source/clean approval/source-cue-manifest + cue evidence/selected candidates/TTS/cue-ledger/dubbed/control/report SHA, exact model/version/policy/response identity; timeout/retry/disagreement fail-closed.
- [ ] Dub Sol review exact machine report + three final verdicts on same bindings; controller/KMS transition `verifying_dub_agents → signing_release`, tạo `release_approval.json`, CAS release index và chỉ sau đó chuyển `completed`.
- [ ] Reuse shared RFC 8785/KMS verifier đã pass Gate B và mở rộng cho `release_approval`: release-specific schema/role/SHA checks, rotation/revocation, audit logging, active release-index/cache/link revalidation theo §3.4; không trì hoãn clean signature verification tới Phase F, no manual/degraded pass hoặc automatic full-job dub repair.

Gate F: exact clean approval + exact final dub evidence/roles/Sol + release approval đều hợp lệ; only controller can mark completed and expose dubbed link.

### Phase G — Canary và production rollout (`LUNA` deploy, `SOL` audit)

- [ ] Canary đúng ba video: sample hiện tại, manhua, high-motion/complex-background; bộ ba bao phủ thoại dày, repeated cue, multi-speaker, humor/punchline, long Vietsub và early-ending source/voice tail.
- [ ] Chạy tuần tự/song song, cache hit/force refresh; đo latency, GPU/CPU/model/API cost, failure/callback/QA rate.
- [ ] Integrated 7 phút 1080p ≤60 phút; concurrency 2/queue 10/busy behavior pass; hard timeout 90 phút.
- [ ] Smoke + live E2E private Telegram: `/desub` → signed clean gate → dub/preflight/final gate → callback → one status message → fresh dubbed-only link.
- [ ] Verify tail regression: last voice kết thúc ≥1 giây sớm nhưng bed/AAC vẫn tới video end trong 1024 samples; kiểm output download SHA/decode/Range.
- [ ] Rollback immutable image digest; xác nhận Long/Short/Cover Visub không regression; cập nhật deploy/runbook/incident recovery.
- [ ] Sau mọi gate/canary pass, gửi user video cuối để kiểm hình ảnh, bản dịch/humor, Vietsub và lồng tiếng; chờ phê duyệt rõ ràng trước production rollout.

Gate G: chỉ mời user kiểm khi fresh dubbed link, exact clean/source-cue-manifest/cue-ledger/dubbed/release bindings, SLO/concurrency/cost và Gate A–F đều pass. Không production rollout trước user approval.

## 7. Test matrix tối thiểu

| Scenario | Kỳ vọng |
|---|---|
| Private Telegram `/desub <Douyin URL>` từ allowlisted user/chat | Acknowledge bằng một status message, `202`, một integrated execution, callback terminal chỉ chứa link `dubbed.mp4` khi completed |
| Group/channel hoặc user/chat ngoài allowlist | Reject bằng safe stable error, không gọi runner |
| URL cache hit có integrated approval hợp lệ | `200`, không tốn GPU, fresh dubbed-only link; clean artifact không public |
| Cùng idempotency key nhưng body khác | `409` |
| Hai request đồng thời cùng idempotency key | Một job/attempt |
| Hai execution active + 10 job queued + thêm request | Request mới nhận stable busy/`429`; không vượt capacity |
| Integrated execution 7 phút/1080p vượt 60 phút hoặc chạm 90 phút | SLO alert hoặc hard-timeout terminal đúng policy; queue time được báo riêng |
| Hai request khác key, cùng content fingerprint | Một in-flight execution, mỗi subscription của allowlisted chat nhận đúng một kết quả |
| Hai `force_refresh` đồng thời | Conflict/serialize, không ghi đè |
| `force_refresh` khi attempt hiện hành còn active | `409 ATTEMPT_IN_PROGRESS`, không tạo execution thứ hai |
| `jobs.run` timeout sau khi execution có thể đã tạo | Public state vẫn `queued`, `launch_state=unknown`, không launch mù lần hai |
| Dry-run `jobs.run` override | Exact launch token/job/attempt env xuất hiện trong returned `Execution.template`; labels không được dùng |
| `launch_state=unknown` positive-token match thấy 1/2/0 execution | Recover `launched` / fail-closed+alert / giữ `unknown`, tuyệt đối không relaunch; unresolved 15 phút fail fenced |
| Crash sau lease nhưng trước launch | Lease được recover, tối đa một winning attempt |
| Attempt cũ ghi status/artifact sau winner mới | Fencing reject write |
| Callback duplicate/out-of-order | Không gửi Telegram trùng |
| Terminal callback cho shared job có hai request của allowlisted chat | Hoàn tất đúng từng subscription, một notification/subscription |
| Crash sau khi update request đầu tiên | Retry chỉ subscription chưa ACK |
| Callback sai signature/timestamp quá replay window | `401` |
| Callback hợp lệ replay cùng event ID | `200`, không notify lại |
| Dispatcher retry sau replay window | Giữ exact body/event ID, tạo timestamp mới và ký lại thì pass; reuse signature cũ bị `401` |
| Callback ký bằng current/previous/unknown key | Rotation window đúng; unknown reject |
| `clean_qa_failed/translation_failed/tts_failed/scheduling_failed/subtitle_failed/dub_qa_failed/failed/cancelled` callback | Structured error, không có link |
| Progress lặp/out-of-order qua các stage tích hợp | Chỉ edit một status message, state không lùi và không spam; UI gom đúng download → inpaint → clean QA → translate → TTS/Vietsub → dub QA → done |
| Terminal failure | Message đã redact có human summary, stable code, failed stage và retry button; không lộ secret/stack/provider payload |
| Callback bị mất | Reconciler tìm và hoàn tất |
| Hai reconciler chạy đồng thời | Một row lease, một notification |
| n8n restart khi job chạy | Job vẫn được reconcile |
| Status trả `401/404/429/5xx` | Alert/grace/retry đúng policy |
| Link hết hạn | Exact attempt status revalidate approval rồi mint link mới, không rerun/lấy latest |
| Attempt result hết retention / approval cần reverify | `410` / `409`, không trả artifact attempt khác |
| Status thiếu/sai auth | `401` |
| Download signature tamper/path escape/expired | `403/400/410`, không đọc object ngoài prefix |
| Download Range/resume + final SHA | `206`/resume pass, SHA khớp |
| Cache hit đồng thời nhận callback cũ | Một notification |
| Cancel một trong hai request của cùng chat | Chỉ detach request đó; shared work và request còn lại tiếp tục |
| Admin global cancel | Winning attempt fenced; mọi subscription nhận terminal cancel đúng một lần |
| Cache artifact/report/approval corrupt hoặc policy cũ | Không cache-hit; reverify/rerun đúng lớp |
| Clean approval hoặc source-cue manifest thiếu/tamper/stale fence/wrong generation/policy/SHA | Dub work authorization fail; dub service không được đọc clean/evidence artifact |
| Cùng source/clean SHA nhưng swap/replay source-cue manifest hoặc clean verdict từ collection root khác | Machine/verdict cross-binding mismatch; không tạo/activate clean approval |
| Dub work item thiếu exact signed source-cue collection hoặc thử list/read raw source video | Fail authorization/IAM; không OCR/đoán text từ clean artifact |
| Release approval payload/signature/manifest generation/report/verdict hash bị tamper | Signature/evidence verification fail; không completed/cache/link |
| Approval replay sang artifact, cue ledger hoặc QA policy khác | Binding mismatch, fail-closed |
| Approval dùng wrong/disabled/destroyed/revoked KMS key version | Fail-closed; chạy lại affected clean authorization hoặc final release gate trước khi đọc clean/phát link |
| KMS/key-status lookup lỗi hoặc approval hết hạn | Không mint/refresh link; không dùng cached pass |
| KMS rotation khi approval cũ còn hạn | Chỉ accept old version khi vẫn `ENABLED` và allowlisted; current signer dùng version mới |
| Delayed completed callback attempt 1 khi attempt 2 active | Exact attempt 1 endpoint trả đúng SHA; subscription 1 vẫn nhận artifact 1, không lấy latest attempt 2 |
| Callback khác bound attempt hoặc state_version cũ trong cùng subscription | ACK nhưng không mutate/notify row đó |
| Redirect tới private/metadata IP | Reject |
| IPv6, DNS rebinding và multi-hop redirect SSRF | Reject mọi hop/IP |
| Input oversize/VFR/HDR/rotation/multi-audio | Reject bằng stable error code |
| Dialogue caption có track ngoài `y=0,55–0,90` | Preflight reject `UNSUPPORTED_SUBTITLE_LAYOUT` |
| Input không audio | Preflight reject `DUB_SOURCE_AUDIO_REQUIRED`; không chạy clean/dub |
| Clean encode xong nhưng clean QA fail | `clean_qa_failed`, không chạy dub và không gửi link |
| Clean pass nhưng translation/TTS/scheduling/subtitle/dub QA fail | Giữ clean artifact/approval bất biến để evidence/recovery, nhưng parent không completed và không gửi link |
| Agent timeout/disagreement/verdict sai artifact/candidate/cue-ledger SHA | Fail đúng clean/translation/dub gate, không completed |
| Clean QA fail lần đầu và repair pass | Tạo đúng một repair attempt/clean SHA mới, chạy lại toàn bộ clean QA rồi invalidate/rebuild mọi dub derivative trước khi completed |
| Clean repair attempt fail | Dừng `clean_qa_failed`, không automatic repair thứ hai, không gửi link |
| QA provider/model ID/quota/no-training/retention/Singapore-processing runtime chưa chứng minh | Gate A blocked, không deploy/fake pass; ngoại lệ user chấp nhận chỉ dành cho CapCut TTS |
| Final Dub verifier C không có endpoint hỗ trợ trực tiếp audio/video | Gate A blocked; không giả vờ Sol/Luna/Terra đã nghe/xem. Phải duyệt audio/video-capable verifier hoặc revision sang exact derived evidence với giới hạn perceptual rõ ràng |
| CapCut private endpoint hoặc `BV075_streaming` unavailable/quota/error | Whole job fail ở TTS với stable code; không provider/voice fallback và không mixed voice |
| CapCut trả empty/truncated/corrupt audio hoặc âm thanh không đúng `BV075_streaming` dù response metadata đúng | Media/decode/ASR + Dub verifier C fail; không fallback hoặc select clip đó |
| Clean, source-cue-manifest, cue-ledger hoặc dubbed SHA thay đổi | Hủy mọi verdict/approval phụ thuộc SHA đó; verify/rebuild đúng downstream layer từ đầu |
| Source audio vốn ngắn hơn video, clean output bảo toàn packet/PTS | Clean audio gate pass |
| Clean audio khác source packet/hash/PTS dù container duration gần đúng | Fail clean audio gate |
| Dubbed audio khác source packet/hash/PTS | Không dùng clean-audio equality; kiểm mixed-audio policy riêng vì thay đổi là chủ ý |
| `N` source cues → `N` translations → `N` TTS clips → `N` Vietsub events, index liên tục | Pass cue-ledger cardinality/order gate |
| Cue bị empty/merge/split/drop/duplicate/reorder ở bất kỳ lớp nào | Fail-closed; không tự sửa bằng cách đổi cardinality |
| Cue ledger có text/timing nhưng thiếu crop generation/SHA, source PCM-slice SHA hoặc extractor/aligner identity | Fail cue-provenance gate; không cho translation/TTS |
| Hai caption fragment giống chữ nhưng cùng một utterance / hai utterance lặp thật | Temporal/audio/visual evidence phải collapse đúng một cue / giữ đúng hai cue; cardinality deterministic |
| Speech alignment thiếu/không monotonic hoặc chữ Trung trên hình không khớp giọng Trung | Fail với stable alignment code; không đoán text/timing |
| Translation đổi tên/số/đơn vị/phủ định/quan hệ hoặc bịa thêm | Semantic verifier fail |
| Translation đúng nghĩa nhưng mất sarcasm/humor/punchline hoặc tiếng Việt gượng | Style verifier fail; compact lại chỉ trong giới hạn hai retry |
| Translator tự chấm bản dịch của chính mình | Reject provenance; semantic/style verifier phải độc lập |
| Initial không fit; compact `1` không fit; compact `2` fit | Đúng ba immutable candidates và hai compact retries; candidate `2` được hai preflight verifier pass rồi là selected duy nhất |
| Initial + đúng hai compact vẫn không fit | `DUB_CUE_UNFIT`; không candidate `3`, merge/drop/trim hoặc đổi rate |
| Có per-cue rate, bất kỳ post-`atempo`, `synthesize_to_fit` hoặc mixed provider/voice | Fail TTS policy |
| Một cue có actual pace khác fixture dù request metadata khai `rate=1.5000` | Dub verifier C/rate-consistency gate fail; request metadata không đủ để pass |
| Video có nhiều người Trung nói hoặc đổi giới tính/độ tuổi giọng | Vẫn dùng một `BV075_streaming`; không speaker classification/voice matching, cue/timing N→N không đổi |
| Decoded-PCM lag ngay dưới/bằng/trên `0` và `26460` samples; gap ngay dưới/bằng/trên `2205` samples | Negative lag (active speech sớm hơn anchor) fail; exact lower/upper bounds pass; scheduled metadata không thay actual samples |
| Hai clip Việt overlap hoặc voice tail vượt video | Fail scheduling/dub timing; không trim, merge, drop hoặc tăng tốc riêng cue |
| Dub mix đạt/vi phạm `-16 ±1 LUFS`, `≤-1 dBTP`, zero clip hoặc p05 voice-bed `≥8 dB` | Boundary behavior deterministic theo pinned BS.1770/active-window policy; vi phạm là `dub_qa_failed` |
| Bed bị channel swap, phase inversion, midstream dropout, wrong approved-clean PCM hoặc pre-duck gain khác `-20.000 dB` | Stem hash/no-TTS control/continuity gate fail dù final LUFS vẫn đạt |
| Source audio kết thúc sớm hơn video | Clean giữ exact source tail; dub bed được silence-pad và final AAC vẫn kết thúc trong `1024/44100s` của video end |
| Last Vietnamese clip kết thúc ≥1 giây trước clean-derived bed/video | Bed tiếp tục không dropout; last effective AAC packet/decoded end cách video end ≤1024 samples |
| Có dropout/truncation hoặc effective audio tail lệch quá `1024` samples sau priming/padding | Fail dub machine QA |
| Source mix còn giọng Trung nhỏ sau pre-duck `-20.000 dB` + sidechain | Được phép theo product contract nếu p05 voice-over-bed và mọi bed-integrity gate pass |
| Vietsub sai text/count/order/timing, quá hai dòng, có box/blur hoặc sai trắng-viền-tối | Fail Vietsub gate |
| Vietsub có dấu Việt/tên/số dài gây overflow, thiếu glyph/tofu, third line hoặc shrink dưới min | `DUB_SUBTITLE_UNFIT`; không truncate hoặc tự đổi style |
| Pixel thay đổi ngoài `(Vietsub alpha mask + 32 px halo)` so với no-Vietsub control | Fail dubbed-video-integrity gate |
| Midstream video PTS gap/duplicate nhưng first/last khớp | Fail full-sequence timeline gate |
| Color matrix/range/chroma/rotation khác source | Fail metadata gate |
| Engine detector bỏ sót caption, independent OCR thấy track 2+ frame | Fail residual gate |
| Independent OCR chỉ hit một frame | Bắt buộc agent triage; không auto-pass |
| False-positive/overwide mask đè chữ sản phẩm hợp lệ | Fail mask-precision/paired-review gate |
| MAE/p99/fraction/F_t đúng ngay dưới/bằng/trên threshold | Boundary behavior deterministic theo frozen policy |
| Flicker spike sát scene cut và transition không có active mask một phía | Scene-cut scalar exclusion đúng; transition ROI vẫn bắt buộc agent review |
| HMAC fixture dùng raw bytes production | Signature khớp, JSON reserialize không được dùng |
| Lifecycle xóa raw QA sau 2 ngày | Source/clean/source-cue manifest+objects/attempts/ledger/dubbed/control/stem manifests và approvals cần cho cache 7 ngày vẫn còn |

## 8. Definition of Done

1. Allowlisted user gửi một URL Douyin bằng `/desub <url>` trong private Telegram chat và nhận acknowledgement ngay; group/channel/user khác bị reject trước runner.
2. Job tạo immutable `clean.mp4` MP4/H.264 `yuv420p`, CRF 18; dialogue caption trong supported band đã được inpaint, không blur/Vietsub/TTS; caption ngoài band bị preflight reject, geometry/timeline và AAC nguồn nguyên vẹn.
3. Trước inpaint, Phase B tạo immutable source-cue collection; clean machine QA, ba clean verifier độc lập và clean Sol pass exact clean + source-cue-manifest SHA/policy. Controller/KMS tạo signed `clean_approval` + active `clean_gate_index` bind cả hai; sai signature/generation/fence/SHA/policy thì dub không được đọc.
4. Mỗi cue evidence bind source SHA, sample/frame range, crop + PCM-slice hashes, raw/NFC ZH text, aligner/extractor identity/confidence. Dub dùng `clean.mp4` làm video base và chỉ đọc exact signed evidence object URIs; không raw-source list/read, không OCR lại clean đã mất sub và không đoán khi evidence mismatch.
5. Final cue ledger chứng minh đúng `N` source cues → `N` selected translations → `N` selected TTS clips → `N` Vietsub events; retry history tách riêng và không làm tăng cardinality, không merge/split/drop/reorder. Mỗi selected cue bind exact signed translation preflight approval của selected TTS.
6. Bản dịch tự động giữ tên/số/đơn vị/phủ định/quan hệ, nghĩa và humor/punchline tự nhiên. Initial và tối đa hai compact candidates có immutable hashes; candidate artifact không chứa verdict sinh sau. Semantic + style verifier độc lập phải pass exact bindings, controller ký create-only preflight approval và TTS bind approval đó trước synthesize; hết budget thì `DUB_CUE_UNFIT`.
7. Job chỉ gọi CapCut `BV075_streaming` với request `prosody rate="1.5000"` cho mọi selected cue; không post-`atempo`, fallback/per-cue speed hoặc speaker matching. Request/response/media/decoded-PCM hashes được evidence-bound và actual wrong voice/rate không thể pass chỉ nhờ metadata.
8. Timing được đo từ decoded 44.1-kHz PCM bằng pinned detector: active voice không được sớm hơn speech anchor, gap ≥2205 samples, `0 ≤ onset lag ≤26460` samples, overlap 0 sample và final active voice không vượt video; scheduled delay metadata không phải bằng chứng duy nhất.
9. `dubbed.mp4` có Vietsub khớp selected cue/actual voice, trắng viền tối, tối đa hai dòng, font/style/glyph/margin/width policy pin bằng SHA; overflow hoặc thiếu glyph fail `DUB_SUBTITLE_UNFIT`, không truncate/tofu/third-line/excessive shrink.
10. Same-settings no-Vietsub control + alpha mask chứng minh pixel ngoài subtitle mask/codec halo nằm trong frozen thresholds; video geometry/timeline khớp clean.
11. Source-identical mix carried by approved `clean.mp4` là bed input duy nhất, được resample/pad tới video end, đặt pre-duck đúng `-20.000 dB`, sidechain/mix/two-pass normalization/limiter theo exact graph và stem hashes; Phase C không đọc raw source, không source separation và chấp nhận giọng Trung nhỏ nếu voice-over-bed gate pass.
12. Dub machine QA bằng pinned BS.1770-4/EBU R128 pass `-16 ±1 LUFS`, true peak `≤-1 dBTP`, zero decoded clipped samples, p05 active-window voice/bed `≥8 dB`, no channel/phase swap/dropout/truncation; effective AAC end sau priming/padding cách video end ≤1024 samples.
13. Ba final dub verifier A/B/C reverify exact final video/ledger/evidence: semantics, natural Vietnamese + humor, voice/rate/timing/Vietsub/video/bed/audio; dub Sol pass same bindings. Translation preflight không thay thế final verdicts.
14. Controller/KMS chỉ tạo `release_approval` và active release index sau signed clean approval + dub machine + final agents/Sol; release liệt kê mọi signed translation preflight approval mà TTS history/selected ledger dùng. Callback được ký/dedupe/recoverable, n8n chỉ edit một status message và chỉ gửi fresh `dubbed.mp4` link. `clean.mp4` không public.
15. File user tải decode đầy đủ, SHA khớp dub manifest + release approval; approval bind exact attempt/fence, source/clean approval/source-cue-manifest + cue evidence/translation candidates/preflight approvals/TTS attempts/ledger/control/stems/dubbed SHAs và link refresh đúng exact attempt.
16. Ba canary đã duyệt pass, bao phủ thoại dày, repeated cue, multi-speaker, humor/punchline, long Vietsub, high motion và early-ending audio/voice tail; Long/Short/Cover Visub không regression.
17. Images/dependencies qua license, SBOM và CVE gate; không cài package có CVE đã biết. QA provider thỏa no-training/retention và chứng minh processing jurisdiction Singapore bằng endpoint riêng của provider; CapCut TTS được ghi rõ là accepted risk exception.
18. Dashboard/log/trace và alerts cho stuck job, callback DLQ, clean/dub QA failure, retry budget, latency/cost/capacity hoạt động; dữ liệu nhạy cảm được redact; không automatic quality/reasoning downgrade vì cost.
19. Global concurrency 2, queue 10, integrated SLO 60 phút và hard timeout 90 phút pass; queue time đo riêng.
20. Chỉ sau mọi gate, nhiều agent và canary pass mới mời user kiểm hình ảnh, bản dịch/humor, Vietsub và lồng tiếng của video cuối; production rollout cần user phê duyệt rõ ràng.

## 9. Authorization state

Toàn bộ product decision trong §1.3 đã được user duyệt. Không còn câu hỏi sản phẩm nào đang chặn việc lập kế hoạch.

User đã ra chỉ thị rõ ràng “bắt đầu thực hiện Phase A” ngày 2026-07-23. Vì vậy Phase A được phép materialize contract, policy candidate, ADR, fixture inventory và test cục bộ. Quyền này **không** mở rộng sang Phase B, chạy/xử lý video production, tạo Cloud Run Execution/deploy, mutate n8n, provision KMS hoặc mint download link. Gate A hiện BLOCKED/fail-closed; Phase B chỉ bắt đầu sau khi mọi blocker Gate A được đóng và user tiếp tục cho phép theo tiến độ đã thống nhất.
