import unittest
import sys
import types
from unittest.mock import Mock

storage_module = types.ModuleType("google.cloud.storage")
storage_module.Client = Mock
storage_module.Blob = Mock
cloud_module = types.ModuleType("google.cloud")
cloud_module.storage = storage_module
sys.modules.setdefault("google.cloud", cloud_module)
sys.modules.setdefault("google.cloud.storage", storage_module)

from container_long.youtube_uploader_job import _set_thumbnail


class YoutubeThumbnailTests(unittest.TestCase):
    def test_uploads_thumbnail_as_media(self):
        blob = Mock()
        blob.size = 238896
        blob.content_type = "image/jpeg"
        blob.download_as_bytes.return_value = b"jpeg"
        response = Mock(status_code=200)
        response.json.return_value = {"items": [{"default": {"url": "x"}}]}
        session = Mock()
        session.post.return_value = response

        result = _set_thumbnail(
            session,
            token="token",
            video_id="abc123",
            thumbnail_blob=blob,
        )

        self.assertIn("items", result)
        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["params"]["videoId"], "abc123")
        self.assertEqual(kwargs["headers"]["Content-Type"], "image/jpeg")

    def test_rejects_thumbnail_over_two_mb(self):
        blob = Mock()
        blob.size = 2 * 1024 * 1024 + 1
        blob.content_type = "image/jpeg"
        with self.assertRaisesRegex(RuntimeError, "2 MB"):
            _set_thumbnail(
                Mock(),
                token="token",
                video_id="abc123",
                thumbnail_blob=blob,
            )


if __name__ == "__main__":
    unittest.main()
