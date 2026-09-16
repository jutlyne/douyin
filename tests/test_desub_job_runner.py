from __future__ import annotations

import unittest
from unittest import mock

from desub_job_runner import core

try:
    from desub_job_runner import app as runner
except ModuleNotFoundError as exc:
    if exc.name != "flask":
        raise
    runner = None


class _FakeBlob:
    def __init__(self, exists: bool = False):
        self._exists = exists

    def exists(self) -> bool:
        return self._exists


class DesubRunnerPureTests(unittest.TestCase):
    def test_normalize_and_artifacts_match_cover_job_contract(self) -> None:
        normalized = core.normalize_douyin_url(
            "http://www.douyin.com/video/7558369768110116134?foo=bar"
        )
        self.assertEqual(
            normalized,
            "https://douyin.com/video/7558369768110116134/",
        )
        artifacts = core.artifact_uris(
            normalized,
            result_root="gs://media/desub/cover-visub",
            pipeline_version="v9",
        )
        self.assertRegex(artifacts["desub_id"], r"^douyin-[0-9a-f]{12}$")
        self.assertEqual(
            artifacts["output_uri"],
            f"{artifacts['result_prefix']}/output.mp4",
        )
        self.assertEqual(
            artifacts["status_uri"],
            f"{artifacts['result_prefix']}/status.json",
        )

    def test_rejects_non_douyin_and_unsupported_paths(self) -> None:
        for value in (
            "https://evil.example/video/1",
            "https://douyin.com/user/1",
            "javascript:alert(1)",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    core.normalize_douyin_url(value)


@unittest.skipIf(runner is None, "Flask is available in the service image, not local dev")
class DesubRunnerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = runner.app.test_client()
        self.settings = mock.patch.multiple(
            runner,
            API_KEY="api-test",
            DOWNLOAD_SECRET="download-test",
            SERVICE_BASE_URL="https://desub.example",
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def test_run_requires_api_key(self) -> None:
        response = self.client.post(
            "/run",
            json={"douyin_url": "https://v.douyin.com/abc/"},
        )
        self.assertEqual(response.status_code, 401)

    def test_proxy_https_is_preserved_in_generated_links(self) -> None:
        with mock.patch.object(runner, "SERVICE_BASE_URL", ""):
            response = self.client.get(
                "/health",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "desub.example",
                },
            )
            self.assertEqual(response.status_code, 200)
            with runner.app.test_request_context(
                "/",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "desub.example",
                },
            ):
                self.assertEqual(
                    runner._external_base_url(),
                    "https://desub.example",
                )

    def test_all_cover_job_active_stages_are_deduplicated(self) -> None:
        self.assertTrue(
            {
                "validating",
                "downloading",
                "loading_source",
                "detecting",
                "loading_mask",
                "translating",
                "loading_cues",
                "synthesizing",
                "rendering",
                "verifying",
            }.issubset(runner.RUNNING_STATUSES)
        )

    def test_run_queues_cover_job_with_url_and_cleared_gcs_input(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(url, artifacts, force_refresh):
            captured.update(
                url=url,
                artifacts=artifacts,
                force_refresh=force_refresh,
            )
            return "operations/test"

        with (
            mock.patch.object(runner, "_read_json", return_value={}),
            mock.patch.object(runner, "_blob", return_value=_FakeBlob(False)),
            mock.patch.object(runner, "_write_status") as write_status,
            mock.patch.object(runner, "_run_job", side_effect=fake_run),
        ):
            response = self.client.post(
                "/run",
                headers={"X-API-Key": "api-test"},
                json={
                    "douyin_url": "https://v.douyin.com/omX_s0jMfi0/",
                    "force_refresh": True,
                },
            )
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["operation"], "operations/test")
        self.assertTrue(body["download_url"].startswith("https://desub.example/download?"))
        self.assertEqual(captured["url"], "https://v.douyin.com/omX_s0jMfi0/")
        self.assertTrue(captured["force_refresh"])
        write_status.assert_called_once()

    def test_completed_result_is_returned_from_cache(self) -> None:
        with (
            mock.patch.object(
                runner,
                "_read_json",
                return_value={"status": "completed"},
            ),
            mock.patch.object(runner, "_blob", return_value=_FakeBlob(True)),
            mock.patch.object(runner, "_run_job") as run_job,
        ):
            response = self.client.post(
                "/run",
                headers={"X-API-Key": "api-test"},
                json={"douyin_url": "https://v.douyin.com/omX_s0jMfi0/"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["cached"])
        run_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
