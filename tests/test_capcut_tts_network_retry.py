from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from container_short.steps import capcut_tts


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DownloadResponse:
    def __init__(
        self,
        status_code: int,
        chunks: list[bytes],
        *,
        stream_error: Exception | None = None,
        exit_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.chunks = chunks
        self.stream_error = stream_error
        self.exit_error = exit_error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.closed = True
        if self.exit_error is not None:
            raise self.exit_error

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise requests.exceptions.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size: int):
        self.chunk_size = chunk_size
        yield from self.chunks
        if self.stream_error is not None:
            raise self.stream_error


class CapCutNetworkRetryTests(unittest.TestCase):
    def test_zero_audio_task_failure_is_narrowly_retryable(self) -> None:
        self.assertTrue(
            capcut_tts._looks_retryable_task_failure(
                {
                    "data": {
                        "tasks": [
                            {
                                "status": "failed",
                                "err_code": 23084,
                                "err_msg": "audio duration is zero",
                            }
                        ]
                    }
                }
            )
        )
        self.assertFalse(
            capcut_tts._looks_retryable_task_failure(
                {
                    "data": {
                        "tasks": [
                            {
                                "status": "failed",
                                "err_code": 999,
                                "err_msg": "invalid voice",
                            }
                        ]
                    }
                }
            )
        )

    def test_concurrent_limit_task_failure_is_narrowly_retryable(self) -> None:
        payload = {
            "data": {
                "tasks": [
                    {
                        "status": "failed",
                        "err_code": 50000011,
                        "err_msg": "ExceededConcurrentLimit",
                    }
                ]
            }
        }
        self.assertEqual(
            capcut_tts._retryable_task_failure_reason(payload),
            "concurrent-limit",
        )
        self.assertTrue(capcut_tts._looks_retryable_task_failure(payload))

    def test_concurrent_limit_retry_uses_longer_backoff(self) -> None:
        transient = capcut_tts.RetryableCapCutTaskError(
            "concurrent limit",
            reason="concurrent-limit",
        )
        with (
            patch.object(
                capcut_tts,
                "_synthesize",
                side_effect=[transient, None],
            ) as synthesize,
            patch.object(capcut_tts, "duration_seconds", return_value=1.25),
            patch.object(capcut_tts.time, "sleep") as sleep,
        ):
            duration = capcut_tts.synthesize_once(
                "Tầm 2.500 tệ nhé?",
                "voice.mp3",
                voice="BV075_streaming",
                resource_id="7102355803792740865",
                device={"device_id": "fixed"},
                rate=1.5,
                poll_timeout=300,
            )

        self.assertEqual(duration, 1.25)
        self.assertEqual(synthesize.call_count, 2)
        self.assertEqual(
            synthesize.call_args_list[0],
            synthesize.call_args_list[1],
        )
        sleep.assert_called_once_with(5.0)

    def test_synthesize_once_retries_zero_audio_with_same_identity(self) -> None:
        transient = capcut_tts.RetryableCapCutTaskError(
            "zero audio",
            reason="zero-audio",
        )
        with (
            patch.object(
                capcut_tts,
                "_synthesize",
                side_effect=[transient, None],
            ) as synthesize,
            patch.object(capcut_tts, "duration_seconds", return_value=1.25),
            patch.object(capcut_tts.time, "sleep") as sleep,
        ):
            duration = capcut_tts.synthesize_once(
                "Tầm 2.500 tệ nhé?",
                "voice.mp3",
                voice="BV075_streaming",
                resource_id="7102355803792740865",
                device={"device_id": "fixed"},
                rate=1.5,
                poll_timeout=300,
            )

        self.assertEqual(duration, 1.25)
        self.assertEqual(synthesize.call_count, 2)
        self.assertEqual(
            synthesize.call_args_list[0],
            synthesize.call_args_list[1],
        )
        sleep.assert_called_once_with(2.0)

    def test_zero_audio_task_retry_is_bounded(self) -> None:
        transient = capcut_tts.RetryableCapCutTaskError(
            "zero audio",
            reason="zero-audio",
        )
        with (
            patch.object(
                capcut_tts,
                "_synthesize",
                side_effect=transient,
            ) as synthesize,
            patch.object(capcut_tts.time, "sleep"),
        ):
            with self.assertRaises(capcut_tts.RetryableCapCutTaskError):
                capcut_tts.synthesize_once(
                    "Tầm 2.500 tệ nhé?",
                    "voice.mp3",
                    device={"device_id": "fixed"},
                )

        self.assertEqual(synthesize.call_count, 3)

    def test_tls_failure_retries_same_signed_request(self) -> None:
        response = _Response(200)
        with (
            patch(
                "requests.post",
                side_effect=[
                    requests.exceptions.SSLError("temporary TLS failure"),
                    response,
                ],
            ) as post,
            patch.object(capcut_tts.time, "sleep") as sleep,
        ):
            result = capcut_tts._post_with_transient_retries(
                "https://example.invalid/query",
                headers={"sign": "fixed"},
                body_text='{"task":"same"}',
                label="tts-query",
            )

        self.assertIs(result, response)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        sleep.assert_called_once_with(1.0)

    def test_audio_download_tls_failure_retries_same_url(self) -> None:
        success = _DownloadResponse(200, [b"audio", b"-bytes"])
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "voice.mp3"
            with (
                patch(
                    "requests.get",
                    side_effect=[
                        requests.exceptions.SSLError("temporary TLS failure"),
                        success,
                    ],
                ) as get,
                patch.object(capcut_tts.time, "sleep") as sleep,
            ):
                capcut_tts._download_url(
                    "https://example.invalid/audio.mp3",
                    str(destination),
                    timeout=120,
                )

            self.assertEqual(destination.read_bytes(), b"audio-bytes")

        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0], get.call_args_list[1])
        sleep.assert_called_once_with(1.0)

    def test_audio_download_retry_is_bounded(self) -> None:
        with (
            patch(
                "requests.get",
                side_effect=requests.exceptions.SSLError("persistent TLS failure"),
            ) as get,
            patch.object(capcut_tts.time, "sleep"),
        ):
            with self.assertRaises(requests.exceptions.SSLError):
                capcut_tts._download_url(
                    "https://example.invalid/audio.mp3",
                    "voice.mp3",
                    timeout=120,
                    attempts=3,
                )

        self.assertEqual(get.call_count, 3)

    def test_audio_download_terminal_midstream_failure_leaves_no_partial_file(self) -> None:
        first = _DownloadResponse(
            200,
            [b"partial-first"],
            stream_error=requests.exceptions.Timeout("stream interrupted"),
        )
        second = _DownloadResponse(
            200,
            [b"partial-second"],
            stream_error=requests.exceptions.ConnectionError("stream interrupted"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "voice.mp3"
            destination.write_bytes(b"stale-audio")
            with (
                patch("requests.get", side_effect=[first, second]) as get,
                patch.object(capcut_tts.time, "sleep"),
            ):
                with self.assertRaises(requests.exceptions.ConnectionError):
                    capcut_tts._download_url(
                        "https://example.invalid/audio.mp3",
                        str(destination),
                        timeout=120,
                        attempts=2,
                    )

            self.assertFalse(destination.exists())
            self.assertEqual([], list(Path(temp_dir).glob("*.part")))

        self.assertEqual(get.call_count, 2)

    def test_audio_download_does_not_retry_non_5xx_600_status(self) -> None:
        response = _DownloadResponse(600, [])
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "voice.mp3"
            with (
                patch("requests.get", return_value=response) as get,
                patch.object(capcut_tts.time, "sleep") as sleep,
            ):
                with self.assertRaises(requests.exceptions.HTTPError):
                    capcut_tts._download_url(
                        "https://example.invalid/audio.mp3",
                        str(destination),
                        timeout=120,
                    )

            self.assertFalse(destination.exists())

        get.assert_called_once()
        sleep.assert_not_called()

    def test_audio_download_terminal_close_failure_leaves_no_published_file(self) -> None:
        response = _DownloadResponse(
            200,
            [b"complete-audio"],
            exit_error=requests.exceptions.Timeout("response close failed"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "voice.mp3"
            with (
                patch("requests.get", return_value=response) as get,
                patch.object(capcut_tts.time, "sleep"),
            ):
                with self.assertRaises(requests.exceptions.Timeout):
                    capcut_tts._download_url(
                        "https://example.invalid/audio.mp3",
                        str(destination),
                        timeout=120,
                        attempts=1,
                    )

            self.assertFalse(destination.exists())
            self.assertEqual([], list(Path(temp_dir).glob("*.part")))

        get.assert_called_once()

    def test_retryable_http_response_is_closed_before_retry(self) -> None:
        unavailable = _Response(503)
        success = _Response(200)
        with (
            patch("requests.post", side_effect=[unavailable, success]) as post,
            patch.object(capcut_tts.time, "sleep") as sleep,
        ):
            result = capcut_tts._post_with_transient_retries(
                "https://example.invalid/new",
                headers={},
                body_text="{}",
                label="tts-new",
            )

        self.assertIs(result, success)
        self.assertTrue(unavailable.closed)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_nonretryable_http_response_returns_immediately(self) -> None:
        response = _Response(400)
        with (
            patch("requests.post", return_value=response) as post,
            patch.object(capcut_tts.time, "sleep") as sleep,
        ):
            result = capcut_tts._post_with_transient_retries(
                "https://example.invalid/new",
                headers={},
                body_text="{}",
                label="tts-new",
            )

        self.assertIs(result, response)
        post.assert_called_once()
        sleep.assert_not_called()

    def test_retry_count_is_strictly_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 8"):
            capcut_tts._post_with_transient_retries(
                "https://example.invalid/new",
                headers={},
                body_text="{}",
                label="tts-new",
                attempts=9,
            )


if __name__ == "__main__":
    unittest.main()
