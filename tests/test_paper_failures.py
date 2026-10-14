"""Regression checks for the IEEE link failure and error propagation."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from paper_summary import PaperError, _fetch_paper, read_paper
from runtime_control import CancellationToken
from subagents import run_business_subagent
from web_research import PublicFetchError

URL = "https://ieeexplore.ieee.org/document/11029963"


class PaperFailureTests(unittest.TestCase):
    def test_http_202_preserves_reason_and_alternative_inputs(self):
        with patch("paper_summary.fetch_public", side_effect=PublicFetchError(202)):
            with self.assertRaises(PaperError) as raised:
                _fetch_paper(URL, CancellationToken())
        self.assertIn("HTTP 202", str(raised.exception))
        self.assertIn("PDF", str(raised.exception))
        self.assertFalse(raised.exception.retryable)

    def test_challenge_page_is_not_paper_material(self):
        page = b"<html><title>JavaScript is disabled</title><body>In order to continue, we need to verify that you're not a robot. Enable JavaScript and then reload the page.</body></html>"
        with patch("paper_summary._fetch_paper", return_value=(URL, page, "text/html")):
            with self.assertRaisesRegex(PaperError, "访问验证页面"):
                read_paper({"url": URL}, None, CancellationToken(), lambda _: None)

    def harness(self, errors, rounds):
        shared = SimpleNamespace(settings=SimpleNamespace(api_key=""), cancellation=CancellationToken(),
                                 model=Mock(), trace=Mock(), execute_tool=Mock(side_effect=errors))
        calls = ["produce_result", "inspect_result"][:rounds]
        shared.model.chat.side_effect = [{"role": "assistant", "tool_calls": [
            {"id": "call_" + str(i), "function": {"name": name, "arguments": "{}"}}]}
            for i, name in enumerate(calls)]
        harness = SimpleNamespace(shared=shared, title="论文总结", max_rounds=rounds, time_budget=30,
            tools=SimpleNamespace(metadata=lambda: []), instructions=lambda: "处理论文", task_description=lambda: {}, finished=False)
        return harness

    def test_terminal_failure_stops_after_one_model_round(self):
        harness = self.harness([PaperError("HTTP 202，请上传 PDF")], 2)
        with patch.dict("subagents.BUSINESS_HARNESSES", {"paper_summary": lambda *_: harness}):
            with self.assertRaisesRegex(PaperError, "HTTP 202"):
                run_business_subagent("paper_summary", {}, None)
        self.assertEqual(harness.shared.model.chat.call_count, 1)
        self.assertEqual(harness.shared.execute_tool.call_count, 1)

    def test_followup_inspection_error_does_not_hide_production_error(self):
        harness = self.harness([PublicFetchError(502), ValueError("还没有成果")], 2)
        with patch.dict("subagents.BUSINESS_HARNESSES", {"paper_summary": lambda *_: harness}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                run_business_subagent("paper_summary", {}, None)


if __name__ == "__main__":
    unittest.main()
