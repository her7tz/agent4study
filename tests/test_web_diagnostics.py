"""Failure logs retain diagnostics while hiding configured credentials."""

import unittest
from unittest.mock import Mock

from web_server import SecretSafeLogger


class FailureLoggingTests(unittest.TestCase):
    def test_exception_trace_is_present_and_secret_is_redacted(self):
        logger = Mock()
        safe_logger = SecretSafeLogger(logger, "test-private-credential")
        try:
            raise RuntimeError("failed request: test-private-credential")
        except RuntimeError:
            safe_logger.exception("任务执行失败")
        text = "\n".join(str(call.args[1]) for call in logger.log.call_args_list)
        self.assertIn("Traceback", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("[已隐藏]", text)
        self.assertNotIn("test-private-credential", text)


if __name__ == "__main__":
    unittest.main()
