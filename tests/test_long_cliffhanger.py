import unittest
from unittest.mock import patch

from container_long.models import ChunkResult
from container_long.pipeline import (
    LongPipelineConfig,
    _bounded_source_range,
    _select_cliffhanger_chunks,
)


class LongCliffhangerTest(unittest.TestCase):
    def test_source_range_continues_from_previous_boundary(self):
        self.assertEqual(_bounded_source_range(5400, 0, 900), (0, 900))
        self.assertEqual(_bounded_source_range(5400, 720, 900), (720, 1620))
        self.assertEqual(_bounded_source_range(1200, 900, 900), (900, 1200))
        self.assertEqual(_bounded_source_range(1200, 0, 0), (0, 1200))
        with self.assertRaises(ValueError):
            _bounded_source_range(1200, 0, 30)
        with self.assertRaises(ValueError):
            _bounded_source_range(1200, 1200, 900)

    def test_selects_model_boundary_and_keeps_whole_chunks(self):
        chunks = [
            ChunkResult(
                index=index,
                source_start=index * 60,
                source_end=(index + 1) * 60,
                duration=60,
                output_start=index * 60,
                summary_vi=f"Doan {index}",
            )
            for index in range(15)
        ]
        cfg = LongPipelineConfig(
            cliffhanger_enabled=True,
            cliffhanger_min_seconds=600,
            cliffhanger_max_seconds=900,
        )
        with patch(
            "container_long.pipeline.select_cliffhanger_boundary",
            return_value={
                "boundary_index": 11,
                "title_vi": "Bi mat vua duoc he lo",
                "reason_vi": "Dung luc doi dau bat dau",
                "confidence": 0.94,
            },
        ):
            kept, metadata = _select_cliffhanger_chunks(chunks, cfg=cfg)

        self.assertEqual(len(kept), 12)
        self.assertEqual(kept[-1].index, 11)
        self.assertEqual(metadata["source_end"], 720)
        self.assertEqual(metadata["window_min_seconds"], 600)
        self.assertEqual(metadata["window_max_seconds"], 900)
        self.assertFalse(metadata["fallback_used"])

    def test_invalid_model_boundary_uses_end_of_window_fallback(self):
        chunks = [
            ChunkResult(
                index=index,
                source_start=300 + index * 60,
                source_end=300 + (index + 1) * 60,
                duration=60,
                output_start=index * 60,
            )
            for index in range(15)
        ]
        cfg = LongPipelineConfig(
            cliffhanger_enabled=True,
            cliffhanger_min_seconds=600,
            cliffhanger_max_seconds=900,
        )
        with patch(
            "container_long.pipeline.select_cliffhanger_boundary",
            return_value={"boundary_index": 99},
        ):
            kept, metadata = _select_cliffhanger_chunks(chunks, cfg=cfg)

        self.assertEqual(kept[-1].source_end, 1200)
        self.assertTrue(metadata["fallback_used"])


if __name__ == "__main__":
    unittest.main()
