import json
import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "job_runner")
)

import douyin_channel as dc  # noqa: E402


class ChannelParseTest(unittest.TestCase):
    def test_iter_collects_only_valid_aweme_ids(self):
        node = {
            "loaderData": {
                "user": {
                    "post": [
                        {"aweme_id": "7300000000000000001", "desc": "a"},
                        {"aweme_id": "123", "desc": "too short → bỏ"},
                        {"awemeId": "7300000000000000002", "desc": "b"},
                    ]
                }
            }
        }
        ids = {str(i.get("aweme_id") or i.get("awemeId")) for i in dc._iter_aweme_items(node)}
        self.assertEqual(
            ids, {"7300000000000000001", "7300000000000000002"}
        )

    def test_parse_router_dedupes_and_prefers_with_desc(self):
        items = [
            {"aweme_id": "7300000000000000001"},
            {"aweme_id": "7300000000000000001", "desc": "đầy đủ"},
        ]
        html = (
            '<script>window._ROUTER_DATA = '
            + json.dumps({"loaderData": {"u": {"list": items}}})
            + ";</script>"
        )
        parsed = dc._parse_router_users_posts(html)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].get("desc"), "đầy đủ")

    def test_to_video_builds_canonical_share_url(self):
        v = dc._to_video(
            {"aweme_id": "7300000000000000009", "desc": " hi ", "create_time": 123}
        )
        self.assertEqual(v.share_url, "https://www.douyin.com/video/7300000000000000009")
        self.assertEqual(v.desc, "hi")
        self.assertEqual(v.create_time, 123)

    def test_missing_router_data_raises(self):
        with self.assertRaises(dc.DouyinChannelError):
            dc._parse_router_users_posts("<html>no router here</html>")


if __name__ == "__main__":
    unittest.main()
