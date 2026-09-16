import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).parents[1] / "n8n_cloudrun" / "flow_runtime.py"
SPEC = importlib.util.spec_from_file_location("flow_runtime", MODULE_PATH)
flow = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(flow)


class FlowRuntimeTests(unittest.TestCase):
    def test_validates_supported_douyin_urls(self):
        self.assertEqual(
            flow.validate_douyin_url("https://v.douyin.com/Ab_c-12/"),
            "https://v.douyin.com/Ab_c-12/",
        )
        self.assertEqual(
            flow.validate_douyin_url("https://www.douyin.com/video/12345?x=1"),
            "https://www.douyin.com/video/12345?x=1",
        )

    def test_rejects_non_douyin_and_invalid_paths(self):
        with self.assertRaises(ValueError):
            flow.validate_douyin_url("https://example.com/video/123")
        with self.assertRaises(ValueError):
            flow.validate_douyin_url("https://douyin.com/user/123")

    def test_parses_long_refresh_flags(self):
        parsed = flow.parse_message(
            {
                "message_id": 10,
                "chat": {"id": 20},
                "text": "/long --refresh https://v.douyin.com/aaa/ !https://v.douyin.com/bbb/",
            }
        )
        self.assertEqual(parsed["route"], "long")
        self.assertTrue(parsed["force_refresh"])
        self.assertEqual(len(parsed["videos"]), 2)
        self.assertNotIn("force_refresh", parsed["videos"][0])
        self.assertTrue(parsed["videos"][1]["force_refresh"])

    def test_plain_link_defaults_to_short(self):
        parsed = flow.parse_message(
            {"message_id": 1, "chat": {"id": 2}, "text": "https://v.douyin.com/abc/"}
        )
        self.assertEqual(parsed["route"], "short")

    def test_part_starts_new_series(self):
        parsed = flow.parse_message(
            {
                "message_id": 11,
                "chat": {"id": 22},
                "text": "/part https://v.douyin.com/abc/",
            }
        )
        self.assertEqual(parsed["route"], "series_start")
        self.assertEqual(parsed["douyin_url"], "https://v.douyin.com/abc/")

    def test_next_requires_no_url(self):
        parsed = flow.parse_message(
            {"message_id": 12, "chat": {"id": 22}, "text": "/next"}
        )
        self.assertEqual(parsed["route"], "series_next")

    def test_duplicate_telegram_update_is_acknowledged_without_processing(self):
        send_json = Mock()
        handler = SimpleNamespace(
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            _read_json=lambda: {"update_id": 123, "message": {}},
            _send_json=send_json,
        )
        with (
            patch.object(flow, "_env", return_value="secret"),
            patch.object(flow, "_claim_telegram_update", return_value=False),
            patch.object(flow, "parse_message") as parse,
        ):
            flow.FlowHandler._handle_telegram(handler)

        parse.assert_not_called()
        send_json.assert_called_once_with(
            flow.HTTPStatus.OK,
            {"ok": True, "ignored": True, "reason": "duplicate_update"},
        )

    def test_busy_next_is_reported_once_and_webhook_returns_ok(self):
        send_json = Mock()
        handler = SimpleNamespace(
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            _read_json=lambda: {
                "update_id": 124,
                "message": {"chat": {"id": 22}, "text": "/next"},
            },
            _send_json=send_json,
        )
        with (
            patch.object(flow, "_env", return_value="secret"),
            patch.object(flow, "_claim_telegram_update", return_value=True),
            patch.object(
                flow,
                "parse_message",
                return_value={"route": "series_next", "chat_id": 22, "id": "next"},
            ),
            patch.object(
                flow,
                "start_series",
                side_effect=ValueError("Part hiện tại vẫn đang xử lý"),
            ),
            patch.object(flow, "telegram_send") as telegram_send,
        ):
            flow.FlowHandler._handle_telegram(handler)

        telegram_send.assert_called_once()
        status, payload = send_json.call_args.args
        self.assertEqual(status, flow.HTTPStatus.OK)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["handled"])

    def test_claim_telegram_update_treats_gcs_precondition_as_duplicate(self):
        error = urllib.error.HTTPError(
            "https://storage.googleapis.com/upload",
            flow.HTTPStatus.PRECONDITION_FAILED,
            "exists",
            hdrs=None,
            fp=io.BytesIO(b""),
        )
        with (
            patch.dict(os.environ, {"FLOW_STATE_BUCKET": "media-bucket"}),
            patch.object(flow, "_access_token", return_value="token"),
            patch.object(flow.urllib.request, "urlopen", side_effect=error),
        ):
            self.assertFalse(flow._claim_telegram_update({"update_id": 125}))

    def test_series_job_uploads_private_with_generated_thumbnail(self):
        captured = {}

        def fake_start(payload):
            captured.update(payload)
            return {"ok": True}

        original = flow.start_long_job
        flow.start_long_job = fake_start
        try:
            with patch.dict(
                os.environ,
                {"FLOW_STATE_BUCKET": "media-bucket"},
            ):
                flow._series_job(
                    {"id": "x", "chat_id": 22},
                    url="https://v.douyin.com/abc/",
                    start=720,
                    part_number=3,
                )
        finally:
            flow.start_long_job = original

        self.assertEqual(captured["source_start_seconds"], 720)
        self.assertEqual(captured["cliffhanger_min_seconds"], 600)
        self.assertEqual(captured["cliffhanger_max_seconds"], 900)
        self.assertNotIn("cliffhanger_target_seconds", captured)
        self.assertTrue(captured["youtube_upload_enabled"])
        self.assertEqual(captured["series_part_number"], 3)
        self.assertTrue(captured["thumbnail_generate_enabled"])
        self.assertTrue(captured["thumbnail_required"])
        self.assertEqual(
            captured["thumbnail_reference_uri"],
            "gs://media-bucket/long/_assets/ha-nhan-thumbnail-reference.png",
        )
        self.assertTrue(captured["auto_remove_ads"])
        self.assertEqual(captured["auto_ad_min_confidence"], 0.85)

    def test_series_schedule_uses_two_daily_slots_from_part_thirteen(self):
        schedule_env = {
            "SERIES_SCHEDULE_BASE_PART": "13",
            "SERIES_SCHEDULE_BASE_DATE": "2026-08-26",
            "SERIES_SCHEDULE_TIMES": "11:30,18:30",
            "SERIES_SCHEDULE_TIMEZONE_OFFSET": "+07:00",
        }
        with patch.dict(os.environ, schedule_env, clear=False):
            self.assertEqual(flow._series_publish_at(12), "")
            self.assertEqual(flow._series_publish_at(13), "2026-08-26T04:30:00Z")
            self.assertEqual(flow._series_publish_at(14), "2026-08-26T11:30:00Z")
            self.assertEqual(flow._series_publish_at(15), "2026-08-27T04:30:00Z")
            self.assertEqual(flow._series_publish_at(16), "2026-08-27T11:30:00Z")

    def test_next_uses_exact_saved_source_boundary(self):
        current = {
            "chat_id": 22,
            "douyin_url": "https://v.douyin.com/abc/",
            "part_number": 1,
            "next_start_seconds": 713.5,
            "source_duration": 5200,
            "status": "ready",
            "history": [],
        }
        parsed = {"id": "series-next-22", "chat_id": 22}
        with (
            patch.object(flow, "load_series_state", return_value=current),
            patch.object(flow, "save_series_state"),
            patch.object(
                flow,
                "_series_job",
                return_value={"batch_id": "series-next-22", "operation": "op"},
            ) as run,
        ):
            state, _ = flow.start_series(parsed, next_part=True)

        self.assertEqual(state["part_number"], 2)
        run.assert_called_once_with(
            parsed,
            url="https://v.douyin.com/abc/",
            start=713.5,
            part_number=2,
        )

    def test_completed_callback_persists_next_boundary(self):
        state = {
            "chat_id": 22,
            "douyin_url": "https://v.douyin.com/abc/",
            "part_number": 1,
            "next_start_seconds": 0,
            "source_duration": 0,
            "status": "processing",
            "active_batch_id": "series-part-001-22",
            "history": [],
        }
        body = {
            "event": "long.youtube.completed",
            "ok": True,
            "batch_id": "series-part-001-22",
            "output_uri": "gs://bucket/part1.mp4",
            "youtube_video_id": "abc123",
            "youtube_url": "https://www.youtube.com/watch?v=abc123",
            "youtube_title": "Series | Phần 1",
            "thumbnail_uri": "gs://bucket/part1.jpg",
            "source_parts": [
                {
                    "source_start": 0,
                    "source_processed_end": 713.5,
                    "source_duration": 5200,
                    "cliffhanger": {"reason_vi": "Đối đầu bắt đầu"},
                }
            ],
        }
        with (
            patch.object(flow, "save_series_state") as save,
            patch.object(flow, "telegram_send"),
        ):
            handled = flow._handle_series_callback(body, state)

        self.assertTrue(handled)
        self.assertEqual(state["next_start_seconds"], 713.5)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["history"][0]["source_end"], 713.5)
        self.assertEqual(
            state["history"][0]["youtube_url"],
            "https://www.youtube.com/watch?v=abc123",
        )
        save.assert_called_once_with(state)

    def test_final_source_callback_suggests_final_command(self):
        state = {
            "chat_id": 22,
            "douyin_url": "https://v.douyin.com/abc/",
            "part_number": 7,
            "next_start_seconds": 4800,
            "source_duration": 5200,
            "status": "processing",
            "active_batch_id": "series-part-007-22",
            "history": [],
        }
        body = {
            "event": "long.youtube.completed",
            "ok": True,
            "batch_id": "series-part-007-22",
            "output_uri": "gs://bucket/part7.mp4",
            "youtube_url": "https://www.youtube.com/watch?v=finalpart",
            "source_parts": [
                {
                    "source_start": 4800,
                    "source_processed_end": 5200,
                    "source_duration": 5200,
                    "cliffhanger": {},
                }
            ],
        }
        with (
            patch.object(flow, "save_series_state"),
            patch.object(flow, "telegram_send") as telegram_send,
        ):
            handled = flow._handle_series_callback(body, state)

        self.assertTrue(handled)
        self.assertEqual(state["status"], "complete")
        message = telegram_send.call_args.args[1]
        self.assertIn("Đã xử lý hết video nguồn", message)
        self.assertIn("<b>/final</b>", message)
        self.assertNotIn("<b>/next</b>", message)

    def test_callback_auto_starts_the_next_part_until_configured_limit(self):
        state = {
            "chat_id": 22,
            "douyin_url": "https://v.douyin.com/abc/",
            "part_number": 19,
            "next_start_seconds": 13980,
            "source_duration": 35695,
            "status": "processing",
            "active_batch_id": "series-next-19",
            "history": [],
        }
        body = {
            "event": "long.youtube.completed",
            "ok": True,
            "batch_id": "series-next-19",
            "youtube_url": "https://www.youtube.com/watch?v=part19",
            "source_parts": [
                {
                    "source_start": 13980,
                    "source_processed_end": 14700,
                    "source_duration": 35695,
                    "cliffhanger": {},
                }
            ],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "SERIES_AUTO_CHAT_ID": "22",
                    "SERIES_AUTO_UNTIL_PART": "34",
                },
                clear=False,
            ),
            patch.object(flow, "save_series_state") as save,
            patch.object(
                flow,
                "start_series",
                return_value=({"part_number": 20}, {"batch_id": "p20"}),
            ) as start_next,
            patch.object(flow, "telegram_send") as telegram_send,
        ):
            handled = flow._handle_series_callback(body, state)

        self.assertTrue(handled)
        start_next.assert_called_once()
        self.assertTrue(start_next.call_args.kwargs["next_part"])
        self.assertEqual(start_next.call_args.args[0]["chat_id"], 22)
        self.assertIn("Part 20", telegram_send.call_args.args[1])
        save.assert_called_once_with(state)

    def test_auto_queue_stops_at_its_configured_final_part(self):
        with patch.dict(
            os.environ,
            {
                "SERIES_AUTO_CHAT_ID": "22",
                "SERIES_AUTO_UNTIL_PART": "34",
            },
            clear=False,
        ):
            self.assertEqual(flow._series_auto_until_part(22), 34)
            self.assertEqual(flow._series_auto_until_part(23), 0)

    def test_next_retries_same_part_after_failure(self):
        current = {
            "chat_id": 22,
            "douyin_url": "https://v.douyin.com/abc/",
            "part_number": 3,
            "next_start_seconds": 1400.0,
            "source_duration": 5200,
            "status": "failed",
            "history": [],
        }
        parsed = {"id": "series-next-retry-22", "chat_id": 22}
        with (
            patch.object(flow, "load_series_state", return_value=current),
            patch.object(flow, "save_series_state"),
            patch.object(
                flow,
                "_series_job",
                return_value={"batch_id": "retry", "operation": "op"},
            ) as run,
        ):
            state, _ = flow.start_series(parsed, next_part=True)

        self.assertEqual(state["part_number"], 3)
        run.assert_called_once_with(
            parsed,
            url="https://v.douyin.com/abc/",
            start=1400.0,
            part_number=3,
        )

    def test_limits_long_to_thirty_urls(self):
        links = " ".join(f"https://v.douyin.com/a{i}/" for i in range(31))
        with self.assertRaisesRegex(ValueError, "30"):
            flow.parse_message({"message_id": 1, "chat": {"id": 2}, "text": f"/long {links}"})


if __name__ == "__main__":
    unittest.main()
