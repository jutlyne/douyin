import unittest
import os
import tempfile
from unittest.mock import patch

from douyin_api.douyin_downloader import DouyinError, _parse_item_payload
from container_short.steps import download


class DouyinResolverFallbackTest(unittest.TestCase):
    def test_parses_legacy_item_list(self):
        item = _parse_item_payload(
            {"status_code": 0, "item_list": [{"aweme_id": "123"}]},
            "123",
        )
        self.assertEqual(item["aweme_id"], "123")

    def test_parses_web_aweme_detail(self):
        item = _parse_item_payload(
            {"aweme_detail": {"aweme_id": "456"}},
            "456",
        )
        self.assertEqual(item["aweme_id"], "456")

    def test_rejects_empty_payload(self):
        with self.assertRaises(DouyinError):
            _parse_item_payload({"item_list": []}, "789")

    def test_download_falls_back_to_ytdlp(self):
        work = tempfile.mkdtemp()
        destination = os.path.join(work, "source.mp4")
        expected = object()
        with (
            patch.object(download, "resolve", side_effect=DouyinError("blocked")),
            patch.object(
                download,
                "_download_with_ytdlp",
                return_value=expected,
            ) as fallback,
        ):
            actual = download.download_douyin(
                "https://v.douyin.com/example/",
                destination,
            )
        self.assertIs(actual, expected)
        fallback.assert_called_once_with(
            "https://v.douyin.com/example/",
            destination,
            cookie=None,
        )

    def test_ytdlp_uses_fresh_browser_when_cookie_missing(self):
        work = tempfile.mkdtemp()
        destination = os.path.join(work, "source.mp4")
        with (
            patch.object(download, "_fresh_douyin_browser_profile") as fresh,
            patch.object(download, "_ytdlp_extract") as extract,
        ):
            browser = download._BrowserProfile("/tmp/profile", "test-agent")
            fresh.return_value.__enter__.return_value = browser
            extract.return_value = ({
                "id": "7424910827729210643",
                "title": "test",
                "duration": 60,
            }, destination)
            with open(destination, "wb") as handle:
                handle.write(b"video")

            info = download._download_with_ytdlp(
                "https://v.douyin.com/example/",
                destination,
                cookie=None,
            )

        self.assertEqual(info.aweme_id, "7424910827729210643")
        extract.assert_called_once_with(
            "https://v.douyin.com/example/",
            dst_path=destination,
            cookie=None,
            browser=browser,
        )


if __name__ == "__main__":
    unittest.main()
