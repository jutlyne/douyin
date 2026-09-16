import os
import sys
import unittest
from unittest.mock import MagicMock, patch


class ShortJobCallbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, {"requests": MagicMock()}):
            from container_short.short_job import _defer_failure_callback

        cls.defer_failure_callback = staticmethod(_defer_failure_callback)

    def test_defers_failure_callback_before_final_retry(self):
        env = {
            "CLOUD_RUN_TASK_ATTEMPT": "0",
            "JOB_MAX_RETRIES": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(self.defer_failure_callback())

    def test_allows_failure_callback_on_final_attempt(self):
        env = {
            "CLOUD_RUN_TASK_ATTEMPT": "1",
            "JOB_MAX_RETRIES": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(self.defer_failure_callback())

    def test_allows_failure_callback_when_retries_disabled(self):
        env = {
            "CLOUD_RUN_TASK_ATTEMPT": "0",
            "JOB_MAX_RETRIES": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(self.defer_failure_callback())


if __name__ == "__main__":
    unittest.main()
