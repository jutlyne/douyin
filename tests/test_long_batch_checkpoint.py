import importlib
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch


def _install_google_stubs():
    requests_module = types.ModuleType("google.auth.transport.requests")
    requests_module.AuthorizedSession = Mock(return_value=Mock())
    sys.modules["google.auth.transport.requests"] = requests_module

    storage_module = types.ModuleType("google.cloud.storage")
    storage_module.Client = Mock(return_value=Mock())
    sys.modules["google.cloud.storage"] = storage_module
    cloud_module = sys.modules.get("google.cloud") or types.ModuleType(
        "google.cloud"
    )
    cloud_module.storage = storage_module
    sys.modules["google.cloud"] = cloud_module


def _fresh_coordinator():
    _install_google_stubs()

    sys.modules.pop("container_long.coordinator_job", None)
    with (
        patch("google.auth.default", return_value=(object(), None)),
    ):
        return importlib.import_module("container_long.coordinator_job")


class LongRunnerPayloadTest(unittest.TestCase):
    def test_preserves_global_and_item_force_refresh(self):
        from long_job_runner import payload

        videos, error = payload.parse_videos(
            [
                " https://v.douyin.com/one/ ",
                {
                    "douyin_url": "https://v.douyin.com/two/",
                    "force_refresh": "true",
                },
            ],
            force_refresh_all=False,
        )

        self.assertEqual(error, "")
        self.assertEqual(
            videos,
            [
                {"douyin_url": "https://v.douyin.com/one/"},
                {
                    "douyin_url": "https://v.douyin.com/two/",
                    "force_refresh": True,
                },
            ],
        )

        videos, error = payload.parse_videos(
            [
                "https://v.douyin.com/one/",
                {"douyin_url": "https://v.douyin.com/two/"},
            ],
            force_refresh_all=True,
        )

        self.assertEqual(error, "")
        self.assertTrue(all(item["force_refresh"] for item in videos))


class LongCoordinatorCacheTest(unittest.TestCase):
    def test_url_normalize_and_cache_key_are_stable(self):
        coordinator = _fresh_coordinator()

        normalized = coordinator._normalize_url(
            " HTTPS://V.DOUYIN.COM/AbC/?foo=1#bar "
        )
        self.assertEqual(normalized, "https://v.douyin.com/AbC/")
        self.assertEqual(
            coordinator._source_cache_key(normalized),
            coordinator._source_cache_key("https://v.douyin.com/AbC/"),
        )

    def test_cache_hit_does_not_start_worker(self):
        coordinator = _fresh_coordinator()

        with (
            patch.object(
                coordinator,
                "_source_artifacts_exist",
                return_value=True,
            ),
            patch.object(coordinator, "_copy_source_artifacts") as copy_mock,
            patch.object(coordinator, "_run_job") as run_job_mock,
        ):
            state = coordinator._run_worker_sources(
                videos=[{"douyin_url": "https://v.douyin.com/one/"}],
                batch_id="batch",
                output_prefix="gs://media/long/batch",
                work_prefix="gs://scratch/long/batch",
                force_refresh_all=False,
            )

        run_job_mock.assert_not_called()
        copy_mock.assert_called_once()
        self.assertEqual(state["failed_sources"], [])
        self.assertEqual(state["cached_sources"][0]["status"], "cached")

    def test_worker_retry_success_counts_second_attempt(self):
        coordinator = _fresh_coordinator()

        with (
            patch.object(
                coordinator,
                "_source_artifacts_exist",
                side_effect=[False, True],
            ),
            patch.object(
                coordinator,
                "_run_job",
                side_effect=["operations/first", "operations/second"],
            ) as run_job_mock,
            patch.object(
                coordinator,
                "_read_operation",
                side_effect=[
                    {"done": True, "error": {"message": "download failed"}},
                    {"done": True},
                ],
            ),
            patch.object(coordinator, "_copy_source_artifacts") as copy_mock,
        ):
            state = coordinator._run_worker_sources(
                videos=[{"douyin_url": "https://v.douyin.com/one/"}],
                batch_id="batch",
                output_prefix="gs://media/long/batch",
                work_prefix="gs://scratch/long/batch",
                force_refresh_all=False,
            )

        self.assertEqual(run_job_mock.call_count, 2)
        copy_mock.assert_called_once()
        self.assertEqual(state["failed_sources"], [])
        self.assertEqual(state["completed_sources"][0]["attempts"], 2)

    def test_transient_operation_poll_error_keeps_worker_active(self):
        coordinator = _fresh_coordinator()

        with (
            patch.object(
                coordinator,
                "_source_artifacts_exist",
                side_effect=[False, True],
            ),
            patch.object(
                coordinator,
                "_run_job",
                return_value="operations/worker",
            ),
            patch.object(
                coordinator,
                "_read_operation",
                side_effect=[
                    coordinator.TransientOperationReadError("503 unavailable"),
                    {"done": True},
                ],
            ) as read_operation_mock,
            patch.object(coordinator, "_copy_source_artifacts"),
            patch.object(coordinator.time, "sleep") as sleep_mock,
        ):
            state = coordinator._run_worker_sources(
                videos=[{"douyin_url": "https://v.douyin.com/one/"}],
                batch_id="batch",
                output_prefix="gs://media/long/batch",
                work_prefix="gs://scratch/long/batch",
                force_refresh_all=False,
            )

        self.assertEqual(read_operation_mock.call_count, 2)
        sleep_mock.assert_called_once()
        self.assertEqual(state["failed_sources"], [])
        self.assertEqual(state["completed_sources"][0]["status"], "completed")

    def test_run_writes_status_and_skips_assemble_when_sources_fail(self):
        coordinator = _fresh_coordinator()
        request_data = {
            "batch_id": "batch",
            "videos": [{"douyin_url": "https://v.douyin.com/one/"}],
            "chat_id": "chat",
            "callback_enabled": True,
            "callback_url": "https://callback.test/",
        }
        failed_source = {
            "index": 0,
            "douyin_url": "https://v.douyin.com/one/",
            "attempts": 2,
            "error": "download failed",
        }

        with (
            patch.dict(
                os.environ,
                {
                    "REQUEST_URI": "gs://media/long/batch/request.json",
                    "OUTPUT_PREFIX": "gs://media/long/batch",
                    "WORK_PREFIX": "gs://scratch/long/batch",
                    "BATCH_ID": "batch",
                },
            ),
            patch.object(coordinator, "_load_json", return_value=request_data),
            patch.object(
                coordinator,
                "_run_worker_sources",
                return_value={
                    "completed_sources": [],
                    "cached_sources": [],
                    "failed_sources": [failed_source],
                },
            ),
            patch.object(coordinator, "_upload_json") as upload_json_mock,
            patch.object(coordinator, "_build_manifest") as build_manifest_mock,
            patch.object(coordinator, "_run_job") as run_job_mock,
        ):
            with self.assertRaises(coordinator.BatchSourceFailure) as caught:
                coordinator._run()

        build_manifest_mock.assert_not_called()
        run_job_mock.assert_not_called()
        upload_json_mock.assert_called_once()
        payload, status_uri = upload_json_mock.call_args.args
        self.assertEqual(status_uri, "gs://media/long/batch/status.json")
        self.assertEqual(payload["failed_sources"], [failed_source])
        self.assertEqual(caught.exception.payload["failed_sources"], [failed_source])

    def test_youtube_upload_env_uses_n8n_upload_defaults(self):
        coordinator = _fresh_coordinator()

        env = coordinator._youtube_upload_env(
            request_data={
                "youtube_privacy_status": "private",
                "youtube_publish_at": "2026-08-26T04:30:00Z",
                "youtube_category_id": "24",
                "youtube_made_for_kids": False,
            },
            batch_id="batch",
            chat_id="chat",
            final_uri="gs://media/long/batch/final/final-long.mp4",
            metadata_uri="gs://media/long/batch/final/final-long.json",
            upload_result_uri="gs://media/long/batch/final/youtube-upload.json",
            youtube_fields={
                "youtube_title": "Ha Nhan Sa Dieu",
                "youtube_description": "desc",
                "hashtags": ["hanhan", "reviewphim"],
            },
        )

        self.assertEqual(env["YOUTUBE_TITLE"], "Ha Nhan Sa Dieu")
        self.assertEqual(env["YOUTUBE_DESCRIPTION"], "desc")
        self.assertEqual(env["YOUTUBE_PRIVACY_STATUS"], "private")
        self.assertEqual(env["YOUTUBE_PUBLISH_AT"], "2026-08-26T04:30:00Z")
        self.assertEqual(env["YOUTUBE_CATEGORY_ID"], "24")
        self.assertEqual(env["YOUTUBE_MADE_FOR_KIDS"], "False")
        self.assertEqual(env["YOUTUBE_TAGS"], "hanhan,reviewphim")

    def test_youtube_fields_uses_fixed_public_hashtag_line(self):
        coordinator = _fresh_coordinator()

        fields = coordinator._youtube_fields(
            {
                "title_vi": "Long title",
                "description_vi": "Long description",
                "hashtags": ["truyen tranh review", "tóm tắt phim"],
            }
        )

        self.assertIn(
            "#truyentranhreview #tutien #truongsinh #tomtatphim #manhua #huyenhuyen #douyin #china #hanhan",
            fields["youtube_description"],
        )
        self.assertNotIn("#truyen tranh review", fields["youtube_description"])

    def test_series_youtube_title_has_part_marker_within_limit(self):
        coordinator = _fresh_coordinator()

        fields = coordinator._series_youtube_fields(
            {"youtube_title": "A" * 100, "title_vi": "A" * 100},
            {"series_part_number": 12},
        )

        self.assertTrue(fields["youtube_title"].endswith(" | Phần 12"))
        self.assertLessEqual(len(fields["youtube_title"]), 100)
        self.assertEqual(fields["series_part_number"], 12)

    def test_run_uses_youtube_uploader_when_enabled(self):
        coordinator = _fresh_coordinator()
        request_data = {
            "batch_id": "batch",
            "videos": [{"douyin_url": "https://v.douyin.com/one/"}],
            "chat_id": "chat",
            "callback_enabled": True,
            "callback_url": "https://callback.test/",
            "youtube_upload_enabled": True,
            "series_part_number": 3,
            "thumbnail_generate_enabled": True,
            "thumbnail_required": True,
            "thumbnail_reference_uri": "gs://media/assets/ha-nhan.png",
        }
        long_meta = {
            "title_vi": "Long title",
            "description_vi": "Long description",
            "hashtags": ["hanhan"],
        }
        upload_result = {
            "ok": True,
            "youtube_video_id": "abc123",
            "youtube_url": "https://www.youtube.com/watch?v=abc123",
        }

        with (
            patch.dict(
                os.environ,
                {
                    "REQUEST_URI": "gs://media/long/batch/request.json",
                    "OUTPUT_PREFIX": "gs://media/long/batch",
                    "WORK_PREFIX": "gs://scratch/long/batch",
                    "BATCH_ID": "batch",
                },
            ),
            patch.object(
                coordinator,
                "_load_json",
                side_effect=[request_data, long_meta],
            ),
            patch.object(
                coordinator,
                "_run_worker_sources",
                return_value={
                    "completed_sources": [],
                    "cached_sources": [{"index": 0}],
                    "failed_sources": [],
                },
            ),
            patch.object(coordinator, "_build_manifest", return_value={"items": []}),
            patch.object(coordinator, "_run_job", return_value="operations/assembler"),
            patch.object(coordinator, "_wait_operation"),
            patch.object(
                coordinator,
                "_run_thumbnail_generation",
                return_value={
                    "ok": True,
                    "thumbnail_uri": "gs://media/long/batch/final/youtube-thumbnail.jpg",
                },
            ) as thumbnail_mock,
            patch.object(
                coordinator,
                "_run_youtube_upload",
                return_value=upload_result,
            ) as youtube_upload_mock,
            patch.object(coordinator, "_upload_json") as upload_json_mock,
            patch.object(coordinator, "_callback") as callback_mock,
        ):
            result = coordinator._run()

        youtube_upload_mock.assert_called_once()
        thumbnail_mock.assert_called_once()
        self.assertEqual(
            youtube_upload_mock.call_args.kwargs["thumbnail_uri"],
            "gs://media/long/batch/final/youtube-thumbnail.jpg",
        )
        self.assertEqual(result["event"], "long.youtube.completed")
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["youtube_video_id"], "abc123")
        self.assertEqual(result["series_part_number"], 3)
        self.assertNotIn("download_url", result)
        upload_json_mock.assert_any_call(result, "gs://media/long/batch/status.json")
        callback_mock.assert_called_once_with(
            result,
            enabled=True,
            url="https://callback.test/",
        )

if __name__ == "__main__":
    unittest.main()
