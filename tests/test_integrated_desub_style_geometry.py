from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (
    ROOT
    / "integrated_desub_contracts"
    / "schemas"
    / "v1"
    / "source_cue_record.schema.json"
)
STYLE = (
    ROOT
    / "integrated_desub_contracts"
    / "policies"
    / "v1"
    / "vietsub_style_policy.json"
)
PREIMAGES = (
    ROOT
    / "integrated_desub_contracts"
    / "policies"
    / "v1"
    / "hash_preimage_contracts.json"
)
LEDGER_SCHEMA = (
    ROOT
    / "integrated_desub_contracts"
    / "schemas"
    / "v1"
    / "cue_ledger.schema.json"
)


def _load(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, parse_float=Decimal)


class SupportedBandContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = _load(SCHEMA)
        self.style = _load(STYLE)

    def test_schema_and_policy_freeze_same_inclusive_baseline_band(self) -> None:
        baseline = self.schema["$defs"]["supportedBandCaptionBaseline"]
        band = self.style["supported_caption_band"]

        self.assertEqual(
            self.schema["properties"]["source_visual"]["properties"][
                "caption_baseline_y_normalized"
            ]["$ref"],
            "#/$defs/supportedBandCaptionBaseline",
        )
        self.assertEqual(baseline["minimum"], Decimal("0.55"))
        self.assertEqual(baseline["maximum"], Decimal("0.9"))
        self.assertEqual(band["y_min_inclusive"], baseline["minimum"])
        self.assertEqual(band["y_max_inclusive"], baseline["maximum"])
        self.assertIn("baseline", band["membership_rule"])
        self.assertFalse(band["bbox_intersection_is_membership"])
        self.assertFalse(band["bbox_containment_is_membership"])

    def test_supported_band_boundaries_below_equal_and_above(self) -> None:
        baseline = self.schema["$defs"]["supportedBandCaptionBaseline"]
        lower = baseline["minimum"]
        upper = baseline["maximum"]

        def is_supported(value: Decimal) -> bool:
            return lower <= value <= upper

        cases = (
            (Decimal("0.549999"), False),
            (Decimal("0.55"), True),
            (Decimal("0.90"), True),
            (Decimal("0.900001"), False),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertIs(is_supported(value), expected)


class DeterministicRenderContractTests(unittest.TestCase):
    def test_style_is_fail_closed_until_exact_renderer_is_pinned(self) -> None:
        style = _load(STYLE)
        renderer = style["renderer"]

        self.assertTrue(style["status"].startswith("blocked_"))
        self.assertEqual(renderer["status"], "blocked_unresolved_exact_runtime_bindings")
        for field in (
            "container_image_digest",
            "renderer_binding_sha256",
            "renderer_executable_sha256",
            "libass_sha256",
            "fontconfig_sha256",
            "harfbuzz_sha256",
            "freetype_sha256",
        ):
            self.assertIsNone(renderer[field], field)

        script_info = style["ass_script_info"]
        self.assertEqual(
            script_info["play_res_x_rule"],
            "PlayResX = post_rotation_output_frame_width_px",
        )
        self.assertEqual(
            script_info["play_res_y_rule"],
            "PlayResY = post_rotation_output_frame_height_px",
        )
        self.assertEqual(script_info["scaled_border_and_shadow"], "yes")
        self.assertEqual(script_info["wrap_style"], 2)

        geometry = style["deterministic_geometry"]
        self.assertEqual(
            geometry["line_advance_px_formula"],
            "floor((font_px*118+50)/100)",
        )
        self.assertIn(
            "round_half_up_nonnegative", geometry["seed_anchor_x_px_formula"]
        )
        self.assertIn(
            "round_half_up_nonnegative", geometry["seed_anchor_y_px_formula"]
        )
        self.assertIn("minimum_integer_dx", geometry["final_anchor_x_px_formula"])
        self.assertIn("minimum_integer_dy", geometry["final_anchor_y_px_formula"])

    def test_render_preimage_binds_geometry_lines_mask_renderer_and_style(self) -> None:
        preimages = _load(PREIMAGES)
        render = preimages["contracts"]["vietsub_render_event_sha256"]
        fields = set(render["ordered_semantic_fields"])
        required = {
            "chosen_font_px",
            "text_lines_nfc",
            "line_break_sequence",
            "anchor_x_px",
            "anchor_y_px",
            "rendered_alpha_bbox_px.x",
            "rendered_alpha_bbox_px.y",
            "rendered_alpha_bbox_px.width",
            "rendered_alpha_bbox_px.height",
            "vietsub_alpha_mask.uri",
            "vietsub_alpha_mask.generation",
            "vietsub_alpha_mask.size_bytes",
            "vietsub_alpha_mask.sha256",
            "vietsub_alpha_mask.content_type",
            "renderer_binding_sha256",
            "vietsub_style_policy_sha256",
        }
        self.assertTrue(required <= fields, sorted(required - fields))
        self.assertIn("text_lines_nfc", render["field_encodings"])
        self.assertIn("line_break_sequence", render["field_encodings"])
        self.assertIn("vietsub_alpha_mask", render["field_encodings"])

    def test_vietsub_event_persists_every_render_preimage_field(self) -> None:
        ledger = _load(LEDGER_SCHEMA)
        event = ledger["properties"]["cues"]["items"]["properties"]["vietsub_event"]
        required = set(event["required"])
        expected = {
            "chosen_font_px",
            "text_lines_nfc",
            "line_break_sequence",
            "anchor_x_px",
            "anchor_y_px",
            "rendered_alpha_bbox_px",
            "vietsub_alpha_mask",
            "font_file_sha256",
            "renderer_binding_sha256",
            "vietsub_style_policy_sha256",
            "render_event_sha256",
        }
        self.assertTrue(expected <= required, sorted(expected - required))
        self.assertFalse(event["additionalProperties"])
        self.assertEqual(event["properties"]["chosen_font_px"]["minimum"], 1)
        self.assertEqual(event["properties"]["anchor_x_px"]["type"], "integer")
        self.assertEqual(event["properties"]["anchor_y_px"]["type"], "integer")
        self.assertEqual(
            event["properties"]["rendered_alpha_bbox_px"]["$ref"],
            "#/$defs/pixelBbox",
        )
        self.assertEqual(
            event["properties"]["vietsub_alpha_mask"]["allOf"][0]["$ref"],
            "common.schema.json#/$defs/objectRef",
        )
        self.assertEqual(
            event["properties"]["vietsub_alpha_mask"]["allOf"][1]["properties"][
                "content_type"
            ]["const"],
            "image/png",
        )

    def test_vietsub_event_schema_binds_line_count_to_break_shape(self) -> None:
        ledger = _load(LEDGER_SCHEMA)
        event = ledger["properties"]["cues"]["items"]["properties"]["vietsub_event"]
        lines = event["properties"]["text_lines_nfc"]
        breaks = event["properties"]["line_break_sequence"]

        self.assertEqual((lines["minItems"], lines["maxItems"]), (1, 2))
        self.assertEqual((breaks["minItems"], breaks["maxItems"]), (0, 1))
        self.assertEqual(breaks["items"]["const"], "\\N")

        branches = {
            branch["properties"]["line_count"]["const"]: branch["properties"]
            for branch in event["oneOf"]
        }
        self.assertEqual(set(branches), {1, 2})
        for line_count in (1, 2):
            with self.subTest(line_count=line_count):
                properties = branches[line_count]
                self.assertEqual(
                    (
                        properties["text_lines_nfc"]["minItems"],
                        properties["text_lines_nfc"]["maxItems"],
                    ),
                    (line_count, line_count),
                )
                self.assertEqual(
                    (
                        properties["line_break_sequence"]["minItems"],
                        properties["line_break_sequence"]["maxItems"],
                    ),
                    (line_count - 1, line_count - 1),
                )


if __name__ == "__main__":
    unittest.main()
