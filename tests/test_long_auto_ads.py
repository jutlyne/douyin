import unittest
from unittest.mock import Mock, patch

with patch("google.auth.default", return_value=(Mock(), None)):
    from container_long.coordinator_job import (
        _auto_remove_ads,
        _merge_ad_candidates,
    )


class LongAutoAdsTests(unittest.TestCase):
    def test_merges_overlapping_results_from_two_detectors(self):
        merged = _merge_ad_candidates(
            [
                {
                    "start": 10,
                    "end": 20,
                    "reason_vi": "Kêu gọi tải ứng dụng",
                    "confidence": 0.9,
                    "sources": ["subtitles"],
                },
                {
                    "start": 19.5,
                    "end": 25,
                    "reason_vi": "Có mã QR quảng cáo",
                    "confidence": 0.95,
                    "sources": ["audiovisual"],
                },
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["start"], merged[0]["end"]), (10, 25))
        self.assertEqual(merged[0]["sources"], ["audiovisual", "subtitles"])
        self.assertEqual(merged[0]["confidence"], 0.95)

    def test_clean_video_is_not_cut(self):
        with (
            patch(
                "container_long.coordinator_job._detect_ad_candidates",
                return_value=([], ""),
            ),
            patch("container_long.coordinator_job._run_review_job") as cut,
        ):
            metadata, audit = _auto_remove_ads(
                request_data={"auto_ad_min_confidence": 0.85},
                output_prefix="gs://bucket/batch",
                video_uri="gs://bucket/batch/final/final-long.mp4",
                metadata_uri="gs://bucket/batch/final/final-long.json",
                metadata={"duration": 800},
            )
        self.assertEqual(metadata, {"duration": 800})
        self.assertTrue(audit["clean"])
        self.assertEqual(audit["applied_cuts"], [])
        cut.assert_not_called()

    def test_high_confidence_ad_is_cut_then_reaudited(self):
        detected = {
            "start": 30.0,
            "end": 42.0,
            "confidence": 0.97,
            "reason_vi": "Quảng cáo ứng dụng",
        }
        updated = {"duration": 788}
        with (
            patch(
                "container_long.coordinator_job._detect_ad_candidates",
                side_effect=[([detected], ""), ([], "")],
            ),
            patch(
                "container_long.coordinator_job._run_review_job",
                return_value="operations/cut",
            ) as cut,
            patch("container_long.coordinator_job._wait_operation"),
            patch(
                "container_long.coordinator_job._load_json",
                return_value=updated,
            ),
        ):
            metadata, audit = _auto_remove_ads(
                request_data={
                    "auto_ad_min_confidence": 0.85,
                    "auto_ad_max_passes": 2,
                },
                output_prefix="gs://bucket/batch",
                video_uri="gs://bucket/batch/final/final-long.mp4",
                metadata_uri="gs://bucket/batch/final/final-long.json",
                metadata={"duration": 800},
            )
        self.assertEqual(metadata, updated)
        self.assertTrue(audit["clean"])
        self.assertEqual(audit["applied_cuts"], [[30.0, 42.0]])
        cut.assert_called_once_with(
            output_prefix="gs://bucket/batch",
            spans=[[30.0, 42.0]],
        )


if __name__ == "__main__":
    unittest.main()
