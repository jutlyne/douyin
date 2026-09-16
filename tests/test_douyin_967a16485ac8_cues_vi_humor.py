import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tmp" / "integrated_desub_probe_frames" / "visub_cues_final_v7.json"
FIXTURE = ROOT / "integrated_desub_runtime" / "fixtures" / "douyin_967a16485ac8_cues_vi_humor.json"
INVARIANT_FIELDS = ("start", "end", "text_zh", "center_x", "center_y", "line_count", "reviewed_index")


class DouyinVietnameseCueFixtureTest(unittest.TestCase):
    def test_fixture_invariants(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(77, len(fixture["cues"]))
        self.assertEqual(list(range(77)), [cue["reviewed_index"] for cue in fixture["cues"]])
        self.assertEqual("gpt-5.6-terra", fixture["translation_metadata"]["translator_model"])
        self.assertEqual(
            "tmp/integrated_desub_probe_frames/visub_cues_final_v7.json",
            fixture["translation_metadata"]["source_input"],
        )

    @unittest.skipUnless(
        SOURCE.exists(),
        "tmp/integrated_desub_probe_frames/visub_cues_final_v7.json là file scratch "
        "cục bộ (nằm trong .gitignore), không có trong repo đã clone",
    )
    def test_fixture_matches_local_source(self):
        source = json.loads(SOURCE.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(77, len(source["cues"]))
        for source_cue, translated_cue in zip(source["cues"], fixture["cues"]):
            for field in INVARIANT_FIELDS:
                self.assertEqual(source_cue[field], translated_cue[field], field)


if __name__ == "__main__":
    unittest.main()
