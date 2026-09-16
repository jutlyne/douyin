import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _install_google_stubs() -> None:
    google_module = types.ModuleType("google")
    google_module.__path__ = []
    genai_module = types.ModuleType("google.genai")
    genai_types_module = types.ModuleType("google.genai.types")
    genai_module.types = genai_types_module
    cloud_module = types.ModuleType("google.cloud")
    storage_module = types.ModuleType("google.cloud.storage")
    storage_module.Client = object
    storage_module.Blob = object
    cloud_module.storage = storage_module
    google_module.genai = genai_module
    google_module.cloud = cloud_module
    sys.modules["google"] = google_module
    sys.modules["google.genai"] = genai_module
    sys.modules["google.genai.types"] = genai_types_module
    sys.modules["google.cloud"] = cloud_module
    sys.modules["google.cloud.storage"] = storage_module


_install_google_stubs()
from PIL import Image  # noqa: E402

from container_long import thumbnail_generator_job as thumbnail  # noqa: E402


class ThumbnailGeneratorTests(unittest.TestCase):
    def test_prompt_uses_story_and_reserves_text_areas(self):
        prompt = thumbnail._generation_prompt(
            {
                "title_vi": "Hà Nhân đại náo Trường An",
                "description_vi": "Hà Nhân đối đầu một thế gia quyền lực.",
                "source_parts": [
                    {
                        "cliffhanger": {
                            "title_vi": "Kẻ thù xuất hiện",
                            "reason_vi": "Một trận chiến sắp bắt đầu.",
                        }
                    }
                ],
            },
            3,
        )

        self.assertIn("Part 3", prompt)
        self.assertIn("Hà Nhân đại náo Trường An", prompt)
        self.assertIn("Kẻ thù xuất hiện", prompt)
        self.assertIn("upper 22 percent", prompt)
        self.assertIn("artwork only", prompt)

    def test_overlay_is_youtube_sized_and_below_limit(self):
        source_path = (
            Path(__file__).parents[1]
            / "container_long"
            / "assets"
            / "ha-nhan-thumbnail-reference.png"
        )
        source_bytes = source_path.read_bytes()
        windows_font = r"C:\Windows\Fonts\arialbd.ttf"

        with patch.object(thumbnail, "FONT_CANDIDATES", (windows_font,)):
            encoded = thumbnail._overlay_text(source_bytes, part_number=12)

        self.assertLessEqual(len(encoded), thumbnail.MAX_YOUTUBE_BYTES)
        with Image.open(io.BytesIO(encoded)) as image:
            self.assertEqual(image.size, (1280, 720))
            self.assertEqual(image.format, "JPEG")


if __name__ == "__main__":
    unittest.main()
