import io
import unittest

from container_long.subtitles import write_vietnamese_srt
from container_long.text_cleanup import normalize_long_vi_text


class LongTextCleanupTest(unittest.TestCase):
    def test_normalizes_protagonist_name_variants(self):
        self.assertEqual(
            normalize_long_vi_text(
                "Tôm Trường Sinh gặp Hạ tiên sinh, rồi Hạ mỗ rời đi."
            ),
            "Hà Trường Sinh gặp Hà tiên sinh, rồi Hà mỗ rời đi.",
        )

    def test_vietnamese_srt_excludes_chinese_debug_line(self):
        handle = io.StringIO()

        count = write_vietnamese_srt(
            handle,
            [(1.2, 3.4, "Tôm tiên sinh đã đồng ý rồi.")],
        )

        text = handle.getvalue()
        self.assertEqual(count, 1)
        self.assertIn("Hà tiên sinh đã đồng ý rồi.", text)
        self.assertNotIn("[ZH]", text)


if __name__ == "__main__":
    unittest.main()
