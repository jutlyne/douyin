import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

storage_module = types.ModuleType("google.cloud.storage")
storage_module.Client = Mock
storage_module.Blob = Mock
cloud_module = types.ModuleType("google.cloud")
cloud_module.storage = storage_module
sys.modules.setdefault("google.cloud", cloud_module)
sys.modules.setdefault("google.cloud.storage", storage_module)

from container_long import youtube_uploader_job as uploader


class YouTubeScheduleTest(unittest.TestCase):
    def test_video_resource_adds_publish_at_and_forces_private(self):
        with patch.dict(
            os.environ,
            {
                "YOUTUBE_PRIVACY_STATUS": "public",
                "YOUTUBE_PUBLISH_AT": "2026-08-26T04:30:00Z",
            },
            clear=False,
        ):
            resource = uploader._video_resource({"title_vi": "Scheduled video"})

        self.assertEqual(resource["status"]["privacyStatus"], "private")
        self.assertEqual(resource["status"]["publishAt"], "2026-08-26T04:30:00Z")


if __name__ == "__main__":
    unittest.main()
